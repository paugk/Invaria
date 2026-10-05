"""Safe file access inside a bundle directory, shared by the verifier and the signer.

Every path is opened component by component relative to the root's file descriptor with
``O_NOFOLLOW``, so a symlink anywhere (including a parent directory swapped during the
operation) is refused instead of followed. Files must be regular with a single link.
Writes create a fresh, exclusive temporary file and rename it in place.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from invaria.contracts.bundle import VerifierStatus

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
NOCTTY = getattr(os, "O_NOCTTY", 0)


class Stop(Exception):
    """A bundle problem with the verifier status it implies."""

    def __init__(self, status: VerifierStatus, reason: str) -> None:
        super().__init__(reason)
        self.status: VerifierStatus = status
        self.reason = reason


@contextmanager
def open_root(root: Path) -> Iterator[int]:
    try:
        fd = os.open(root, os.O_RDONLY | DIRECTORY | NOFOLLOW)
    except OSError as error:
        raise Stop("REJECTED", "bundle path is not a real directory") from error
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _parent(root_fd: int, relative: str) -> Iterator[tuple[int, str]]:
    *parents, name = relative.split("/")
    opened: list[int] = []
    try:
        current = root_fd
        for segment in parents:
            current = os.open(segment, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=current)
            opened.append(current)
        yield current, name
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            raise Stop("REJECTED", f"{relative}: symlink or non-directory in path") from error
        raise
    finally:
        for descriptor in opened:
            os.close(descriptor)


def read_at(root_fd: int, relative: str, limit: int) -> bytes | None:
    """Read a regular, single-link file below the root; None when it does not exist."""
    try:
        with _parent(root_fd, relative) as (directory, name):
            fd = os.open(name, os.O_RDONLY | NOFOLLOW | NONBLOCK | NOCTTY, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK):
            raise Stop("REJECTED", f"{relative} is a symlink") from error
        raise
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Stop("REJECTED", f"{relative} is not a regular file")
        if info.st_nlink != 1:
            raise Stop("REJECTED", f"{relative} has {info.st_nlink} hard links")
        if info.st_size > limit:
            raise Stop("REJECTED", f"{relative} exceeds {limit} bytes")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise Stop("REJECTED", f"{relative} exceeds {limit} bytes")
    return data


def replace_at(root_fd: int, name: str, data: bytes) -> None:
    """Write ``name`` (top level) atomically through an exclusive, unpredictable temp file."""
    temporary = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | NOCTTY,
        mode=0o644,
        dir_fd=root_fd,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        raise


def _raise(error: OSError) -> None:
    raise Stop("REJECTED", f"cannot list bundle: {type(error).__name__}") from error


def inventory(
    root_fd: int, *, max_entries: int, max_bytes: int, max_depth: int
) -> tuple[set[str], set[str]]:
    """(files, directories) below the already opened root; anything else is REJECTED."""
    files: set[str] = set()
    directories: set[str] = set()
    total = 0
    for current, dirs, names, dir_fd in os.fwalk(
        ".", dir_fd=root_fd, follow_symlinks=False, onerror=_raise
    ):
        base = Path(current)
        for name in [*dirs, *names]:
            if len(files) + len(directories) >= max_entries:
                raise Stop("REJECTED", "bundle exceeds file count or size limits")
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            relative = (base / name).as_posix()
            if relative.count("/") >= max_depth:
                raise Stop("REJECTED", f"path too deep: {relative}")
            if stat.S_ISLNK(info.st_mode):
                raise Stop("REJECTED", f"symlink in bundle: {relative}")
            if stat.S_ISDIR(info.st_mode):
                directories.add(relative)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise Stop("REJECTED", f"not a regular file: {relative}")
            if info.st_nlink != 1:
                raise Stop("REJECTED", f"hard-linked file: {relative}")
            total += info.st_size
            if total > max_bytes:
                raise Stop("REJECTED", "bundle exceeds file count or size limits")
            files.add(relative)
    folded = [p.casefold() for p in files | directories]
    if len(set(folded)) != len(folded):
        raise Stop("REJECTED", "paths collide when case is ignored")
    return files, directories


def json_depth(data: bytes) -> int:
    """Maximum nesting of arrays/objects, ignoring brackets inside strings."""
    depth = deepest = 0
    in_string = escaped = False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            deepest = max(deepest, depth)
        elif byte in (0x5D, 0x7D):
            depth -= 1
    return deepest
