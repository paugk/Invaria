"""Creating new files and directories without overwriting or following links.

Standard library only; no database, permissions or records: those belong to the callers
(``export_writer`` adds the export semantics on top of ``NewFile``).

The destination's directory is opened component by component from ``/`` (absolute path) or
from the current directory (relative path) with ``O_DIRECTORY | O_NOFOLLOW``: a symbolic
link in any component is refused, system ones included. Everything afterwards is relative
to that directory descriptor, so replacing a component of the path after it was opened never
redirects the write: the result is published in the directory that was opened. That is all
it guarantees: the textual path may afterwards resolve somewhere else (if a directory of it
was renamed or replaced). Checking the final name before writing (``prepare``) is only an
early refusal; the guarantee is the publication without replacement.

``NewFile``: an unpredictable temporary in the same directory (``O_CREAT | O_EXCL |
O_NOFOLLOW``, mode 0600), synced, published with ``linkat`` (fails on any existing name,
dangling links included), then the temporary removed and the directory synced. A remaining
second name is never a normal result (``Unconfirmed``).

``NewDirectory``: a private temporary directory (mode 0700) built with exclusive files (0600)
and subdirectories (0700), every one synced, published with ``renameat2(RENAME_NOREPLACE)``
(fails on any existing name, an empty directory included: a plain ``rename`` would replace
it), then the parent synced. Only entries this run created are ever removed.

Verified on Linux only; other platforms are not claimed. ``NewFile`` needs ``dir_fd`` support
for ``os.open``, ``os.link``, ``os.unlink`` and ``os.stat``; ``NewDirectory`` also needs
Linux's ``renameat2`` (through ``ctypes``). Without them, ``prepare`` refuses before anything
is written. A file system without hard links (files) or without ``RENAME_NOREPLACE``
(directories) gives ``publish_error``.
"""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | _CLOEXEC
RENAME_NOREPLACE = 1


class DestinationError(Exception):
    """The destination cannot be used; nothing was created. ``reason``: ``exists``,
    ``symlink``, ``not_a_directory``, ``missing``, ``invalid_name``, ``unwritable`` or
    ``unsupported``."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class WriteFailure(Exception):
    """Nothing was published under the requested name and this run's temporaries were
    removed (``leftover``: a temporary could not be removed entirely because it holds
    something this run did not create). ``failure``: ``destination_exists``,
    ``write_error``, ``publish_error`` or ``interrupted``."""

    def __init__(self, failure: str, *, leftover: bool = False) -> None:
        super().__init__(failure)
        self.failure = failure
        self.leftover = leftover


class Unconfirmed(Exception):
    """Published under the requested name, but a later step (removing the temporary,
    syncing the directory, or an interruption) did not complete."""


_SUPPORTED = (
    all(f in os.supports_dir_fd for f in (os.open, os.link, os.unlink, os.stat, os.mkdir, os.rmdir))
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
)


def _load_renameat2() -> Callable[[int, bytes, int, bytes, int], int] | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError):
        return None
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p]
    function.argtypes += [ctypes.c_uint]
    function.restype = ctypes.c_int
    return function


_RENAMEAT2 = _load_renameat2()


def supported() -> bool:
    """Whether this platform can create files relative to a directory descriptor."""
    return _SUPPORTED


def directories_supported() -> bool:
    """Whether this platform can also publish a directory without replacement."""
    return _SUPPORTED and _RENAMEAT2 is not None


def rename_noreplace(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
    """``renameat2(..., RENAME_NOREPLACE)``: ``FileExistsError`` if ``dst`` exists."""
    if _RENAMEAT2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is not available")
    if _RENAMEAT2(src_dir_fd, os.fsencode(src), dst_dir_fd, os.fsencode(dst), RENAME_NOREPLACE):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _classify(parent: int, name: str, error: OSError) -> DestinationError:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError:
        return DestinationError("missing", "a directory of the destination does not exist")
    if stat.S_ISLNK(info.st_mode):
        return DestinationError(
            "symlink", "a directory of the destination is a symbolic link (refused)"
        )
    if error.errno == errno.EACCES:
        return DestinationError("unwritable", "a directory of the destination is not readable")
    return DestinationError("not_a_directory", "a component of the destination is not a directory")


def open_destination_parent(path: Path, *, create_parents: bool = False) -> int:
    """The destination's directory, opened without following links, after refusing an
    existing final name (of any kind) and an unwritable directory. Raises
    ``DestinationError``. Creates nothing, except missing parent directories when
    ``create_parents`` (mode 0700, each created exclusively and then opened without
    following links, so a link put in its place is refused)."""
    if path.name in ("", ".", ".."):
        raise DestinationError("invalid_name", "the destination must be named")
    fd = os.open("/" if path.is_absolute() else ".", _DIR_FLAGS | _CLOEXEC)
    try:
        for part in path.parent.parts:
            if part in ("/", "", "."):
                continue
            try:
                child = os.open(part, _DIR_FLAGS | _CLOEXEC, dir_fd=fd)
            except FileNotFoundError as error:
                if not create_parents:
                    raise _classify(fd, part, error) from error
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass  # created meanwhile: opened below without following links
                except OSError as created:
                    raise _classify(fd, part, created) from created
                try:
                    child = os.open(part, _DIR_FLAGS | _CLOEXEC, dir_fd=fd)
                except OSError as reopened:
                    raise _classify(fd, part, reopened) from reopened
            except OSError as error:
                raise _classify(fd, part, error) from error
            os.close(fd)
            fd = child
        try:
            info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            kind = "symlink" if stat.S_ISLNK(info.st_mode) else "exists"
            raise DestinationError(
                kind,
                "the destination exists"
                + (" (a symbolic link)" if kind == "symlink" else "")
                + "; it is never overwritten: use a new name",
            )
        if os.access in os.supports_dir_fd and not os.access(".", os.W_OK | os.X_OK, dir_fd=fd):
            raise DestinationError("unwritable", "the destination directory is not writable")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        view = view[os.write(fd, view) :]
    os.fsync(fd)


class NewFile:
    """One new file. ``prepare`` before computing the content; ``publish`` once."""

    def __init__(self, path: Path, *, temp_prefix: str = ".invaria-write-") -> None:
        self.path = path
        self.name = path.name
        self.temp_prefix = temp_prefix
        self.dir_fd: int | None = None
        self.published = False

    def prepare(self) -> None:
        if not supported():
            raise DestinationError(
                "unsupported", "this platform cannot create files relative to a directory"
            )
        self.dir_fd = open_destination_parent(self.path)

    def _same(self, temp: str) -> bool:
        """Whether the requested name now designates our temporary's file."""
        assert self.dir_fd is not None
        try:
            a = os.stat(temp, dir_fd=self.dir_fd, follow_symlinks=False)
            b = os.stat(self.name, dir_fd=self.dir_fd, follow_symlinks=False)
        except OSError:
            return False
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)

    def _discard(self, temp: str) -> None:
        assert self.dir_fd is not None
        try:
            os.unlink(temp, dir_fd=self.dir_fd)
        except FileNotFoundError:
            pass

    def publish(self, content: bytes) -> None:
        """Write, sync and publish without replacement. ``WriteFailure`` (not published,
        temporary removed) or ``Unconfirmed`` (published, a later step missing)."""
        if self.dir_fd is None or self.published:
            raise RuntimeError("prepare() once before publish()")
        temp = f"{self.temp_prefix}{secrets.token_hex(16)}.tmp"
        try:
            fd = os.open(temp, _FILE_FLAGS, 0o600, dir_fd=self.dir_fd)
        except KeyboardInterrupt as error:
            self._discard(temp)
            raise WriteFailure("interrupted") from error
        except OSError as error:
            raise WriteFailure("write_error") from error
        try:
            try:
                _write_all(fd, content)
            finally:
                os.close(fd)
        except BaseException as error:
            self._discard(temp)
            failure = "write_error" if isinstance(error, Exception) else "interrupted"
            raise WriteFailure(failure) from error
        try:
            os.link(temp, self.name, src_dir_fd=self.dir_fd, dst_dir_fd=self.dir_fd)
        except BaseException as error:
            # An interruption can arrive just after the link succeeded: look, never assume.
            if self._same(temp):
                self.published = True
                raise Unconfirmed("published; interrupted before it was confirmed") from error
            self._discard(temp)
            if isinstance(error, FileExistsError):
                raise WriteFailure("destination_exists") from error
            if isinstance(error, OSError):
                raise WriteFailure("publish_error") from error
            raise WriteFailure("interrupted") from error
        self.published = True
        try:
            os.unlink(temp, dir_fd=self.dir_fd)
            os.fsync(self.dir_fd)
        except BaseException as error:
            raise Unconfirmed("published; the temporary or the directory sync failed") from error

    def close(self) -> None:
        if self.dir_fd is not None:
            os.close(self.dir_fd)
            self.dir_fd = None


def _relative_parts(relative: str) -> list[str]:
    parts = relative.split("/")
    if not relative or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"not a safe relative path: {relative!r}")
    return parts


class NewDirectory:
    """One new directory with its files. ``prepare`` first; ``publish`` once."""

    def __init__(
        self, path: Path, *, temp_prefix: str = ".invaria-dir-", create_parents: bool = False
    ) -> None:
        self.create_parents = create_parents
        self.path = path
        self.name = path.name
        self.temp_prefix = temp_prefix
        self.dir_fd: int | None = None
        self.published = False

    def prepare(self) -> None:
        if not directories_supported():
            raise DestinationError(
                "unsupported",
                "this platform cannot publish a directory without replacing an existing one "
                "(verified on Linux only)",
            )
        self.dir_fd = open_destination_parent(self.path, create_parents=self.create_parents)

    def publish(self, files: Mapping[str, bytes]) -> None:
        """Build ``files`` (relative path -> bytes, written in this order) in a private
        temporary directory and publish it without replacement. ``WriteFailure`` (nothing
        under the requested name; this run's entries removed) or ``Unconfirmed``."""
        if self.dir_fd is None or self.published:
            raise RuntimeError("prepare() once before publish()")
        parent = self.dir_fd
        for relative in files:
            _relative_parts(relative)
        temp = f"{self.temp_prefix}{secrets.token_hex(16)}.tmp"
        dirs: dict[str, int] = {}  # relative directory ("" is the root) -> open descriptor
        created_dirs: list[str] = []  # creation order
        created_files: list[str] = []
        try:
            try:
                os.mkdir(temp, 0o700, dir_fd=parent)
            except OSError as error:
                raise WriteFailure("write_error") from error
            created_dirs.append("")
            dirs[""] = os.open(temp, _DIR_FLAGS | _CLOEXEC, dir_fd=parent)
            for relative, content in files.items():
                *subdirs, name = _relative_parts(relative)
                here = ""
                for part in subdirs:
                    below = f"{here}/{part}".lstrip("/")
                    if below not in dirs:
                        os.mkdir(part, 0o700, dir_fd=dirs[here])
                        created_dirs.append(below)
                        dirs[below] = os.open(part, _DIR_FLAGS | _CLOEXEC, dir_fd=dirs[here])
                    here = below
                fd = os.open(name, _FILE_FLAGS, 0o600, dir_fd=dirs[here])
                created_files.append(relative)
                try:
                    _write_all(fd, content)
                finally:
                    os.close(fd)
            for relative in reversed(created_dirs):
                os.fsync(dirs[relative])
        except BaseException as error:
            leftover = self._clean(temp, dirs, created_dirs, created_files)
            if isinstance(error, WriteFailure):
                error.leftover = leftover
                raise
            failure = "write_error" if isinstance(error, Exception) else "interrupted"
            raise WriteFailure(failure, leftover=leftover) from error
        try:
            try:
                rename_noreplace(parent, temp, parent, self.name)
            except BaseException as error:
                if self._same(dirs[""]):  # an interruption just after the rename: look
                    self.published = True
                    raise Unconfirmed("published; interrupted before it was confirmed") from error
                leftover = self._clean(temp, dirs, created_dirs, created_files)
                if isinstance(error, FileExistsError):
                    raise WriteFailure("destination_exists", leftover=leftover) from error
                if isinstance(error, OSError):
                    raise WriteFailure("publish_error", leftover=leftover) from error
                raise WriteFailure("interrupted", leftover=leftover) from error
            self.published = True
            try:
                os.fsync(parent)
            except BaseException as error:
                raise Unconfirmed("published; the directory sync failed") from error
        finally:
            for fd in dirs.values():
                os.close(fd)
            dirs.clear()

    def _same(self, root_fd: int) -> bool:
        assert self.dir_fd is not None
        try:
            built = os.fstat(root_fd)
            named = os.stat(self.name, dir_fd=self.dir_fd, follow_symlinks=False)
        except OSError:
            return False
        return (built.st_dev, built.st_ino) == (named.st_dev, named.st_ino)

    def _clean(
        self, temp: str, dirs: dict[str, int], created_dirs: list[str], created_files: list[str]
    ) -> bool:
        """Remove only what this run created, deepest first. True if something else (or
        something that could not be removed) keeps the temporary in place."""
        assert self.dir_fd is not None
        leftover = False
        for relative in reversed(created_files):
            here, _, name = relative.rpartition("/")
            try:
                os.unlink(name, dir_fd=dirs[here])
            except FileNotFoundError:
                pass
            except OSError:
                leftover = True
        for relative in reversed(created_dirs):
            if relative in dirs:
                os.close(dirs.pop(relative))
            elif relative != "":
                leftover = True
                continue
            above, _, name = relative.rpartition("/")
            try:
                if relative == "":
                    os.rmdir(temp, dir_fd=self.dir_fd)
                else:
                    os.rmdir(name, dir_fd=dirs[above])
            except FileNotFoundError:
                pass
            except OSError:
                leftover = True
        return leftover

    def close(self) -> None:
        if self.dir_fd is not None:
            os.close(self.dir_fd)
            self.dir_fd = None
