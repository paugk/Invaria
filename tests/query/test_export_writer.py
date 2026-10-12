"""Safe creation of an export file.

No database: the destination checks, the temporary, the publication without replacement and
every failure between them, with the operating system's own calls (and injected faults).
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from invaria import safe_write
from invaria.export_writer import (
    TEMP_PREFIX,
    DestinationError,
    ExportTarget,
    Unconfirmed,
    WriteFailure,
)

CONTENT = b'{\n  "report": "bytes"\n}\n'


def leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith(TEMP_PREFIX))


def export(path: Path, content: bytes = CONTENT) -> ExportTarget:
    target = ExportTarget(path)
    target.prepare()
    try:
        target.publish(content)
    finally:
        target.close()
    return target


def refused(path: Path) -> str:
    target = ExportTarget(path)
    with pytest.raises(DestinationError) as caught:
        target.prepare()
    assert target.dir_fd is None
    return caught.value.reason


def test_a_new_file_gets_exactly_the_bytes_mode_0600_and_no_temporary(tmp_path: Path) -> None:
    out = tmp_path / "sub" / "report.json"
    out.parent.mkdir()
    target = export(out)
    assert target.published
    assert out.read_bytes() == CONTENT
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert out.stat().st_nlink == 1  # the temporary name is gone
    assert leftovers(out.parent) == []


def test_a_relative_destination_starts_at_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "d").mkdir()
    monkeypatch.chdir(tmp_path)
    export(Path("d/report.json"))
    assert (tmp_path / "d/report.json").read_bytes() == CONTENT


def test_existing_destinations_of_any_kind_are_refused_untouched(tmp_path: Path) -> None:
    (tmp_path / "file.json").write_bytes(b"someone else's")
    (tmp_path / "dir.json").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "link.json").symlink_to(tmp_path / "file.json")
    (tmp_path / "dangling.json").symlink_to(tmp_path / "elsewhere/new.json")
    assert refused(tmp_path / "file.json") == "exists"
    assert refused(tmp_path / "dir.json") == "exists"
    assert refused(tmp_path / "link.json") == "symlink"
    assert refused(tmp_path / "dangling.json") == "symlink"
    assert (tmp_path / "file.json").read_bytes() == b"someone else's"
    assert not (tmp_path / "elsewhere/new.json").exists()  # never written through the link
    assert leftovers(tmp_path) == []


def test_path_components_are_never_followed(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "via").symlink_to(tmp_path / "real")
    (tmp_path / "plain").write_bytes(b"x")
    assert refused(tmp_path / "via/report.json") == "symlink"
    assert refused(tmp_path / "plain/report.json") == "not_a_directory"
    assert refused(tmp_path / "absent/report.json") == "missing"
    assert list((tmp_path / "real").iterdir()) == []


def test_names_that_are_not_files_are_refused(tmp_path: Path) -> None:
    assert refused(tmp_path / ".") in ("invalid_name", "exists")
    assert refused(Path("..")) == "invalid_name"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_directory_is_refused_before_anything(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert refused(locked / "report.json") == "unwritable"
    finally:
        locked.chmod(0o700)


def test_an_unsupported_platform_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(safe_write, "_SUPPORTED", False)
    assert refused(tmp_path / "report.json") == "unsupported"


def test_a_destination_created_after_the_check_is_never_replaced(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    target = ExportTarget(out)
    target.prepare()
    out.write_bytes(b"another process")  # the race: between the check and the link
    with pytest.raises(WriteFailure) as caught:
        target.publish(CONTENT)
    target.close()
    assert caught.value.failure == "destination_exists" and not target.published
    assert out.read_bytes() == b"another process"
    assert leftovers(tmp_path) == []


def test_a_dangling_link_created_after_the_check_is_never_followed(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    (tmp_path / "elsewhere").mkdir()
    target = ExportTarget(out)
    target.prepare()
    out.symlink_to(tmp_path / "elsewhere/new.json")
    with pytest.raises(WriteFailure) as caught:
        target.publish(CONTENT)
    target.close()
    assert caught.value.failure == "destination_exists"
    assert not (tmp_path / "elsewhere/new.json").exists()
    assert leftovers(tmp_path) == []


def test_replacing_a_path_component_after_opening_it_does_not_redirect_the_write(
    tmp_path: Path,
) -> None:
    (tmp_path / "dir").mkdir()
    (tmp_path / "attacker").mkdir()
    target = ExportTarget(tmp_path / "dir/report.json")
    target.prepare()
    (tmp_path / "dir").rename(tmp_path / "moved")
    (tmp_path / "dir").symlink_to(tmp_path / "attacker")
    target.publish(CONTENT)
    target.close()
    assert (tmp_path / "moved/report.json").read_bytes() == CONTENT  # the opened directory
    assert list((tmp_path / "attacker").iterdir()) == []


@pytest.mark.parametrize(
    ("call", "error", "failure"),
    [
        ("write", OSError(errno.ENOSPC, "No space left on device"), "write_error"),
        ("fsync", OSError(errno.EIO, "I/O error"), "write_error"),
        ("write", KeyboardInterrupt(), "interrupted"),
        ("link", PermissionError(errno.EPERM, "no hard links here"), "publish_error"),
        ("link", KeyboardInterrupt(), "interrupted"),
    ],
)
def test_failures_before_publishing_leave_no_file_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str, error: BaseException, failure: str
) -> None:
    out = tmp_path / "report.json"
    target = ExportTarget(out)
    target.prepare()

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(os, call, fail)
    with pytest.raises(WriteFailure) as caught:
        target.publish(CONTENT)
    monkeypatch.undo()
    target.close()
    assert caught.value.failure == failure and not target.published
    assert not out.exists() and leftovers(tmp_path) == []


def test_an_interruption_just_after_the_link_is_reported_as_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "report.json"
    target = ExportTarget(out)
    target.prepare()
    real_link = os.link

    def link_then_interrupt(*args: Any, **kwargs: Any) -> None:
        real_link(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "link", link_then_interrupt)
    with pytest.raises(Unconfirmed):
        target.publish(CONTENT)
    monkeypatch.undo()
    target.close()
    assert target.published and out.read_bytes() == CONTENT  # never claimed as not written


def test_a_failed_directory_sync_after_publishing_is_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "report.json"
    target = ExportTarget(out)
    target.prepare()
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if fd == target.dir_fd:
            raise OSError(errno.EIO, "I/O error")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(Unconfirmed):
        target.publish(CONTENT)
    monkeypatch.undo()
    target.close()
    assert target.published and out.read_bytes() == CONTENT and leftovers(tmp_path) == []


def test_publish_needs_prepare_and_happens_once(tmp_path: Path) -> None:
    target = ExportTarget(tmp_path / "report.json")
    with pytest.raises(RuntimeError):
        target.publish(CONTENT)
    target.prepare()
    target.publish(CONTENT)
    with pytest.raises(RuntimeError):
        target.publish(CONTENT)
    target.close()


def test_a_temporary_left_after_publishing_is_never_a_normal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "report.json"
    target = ExportTarget(out)
    target.prepare()

    def unlink(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(os, "unlink", unlink)
    with pytest.raises(Unconfirmed):
        target.publish(CONTENT)
    monkeypatch.undo()
    target.close()
    # Published, but with a second name: reported as unconfirmed, never as complete.
    assert target.published and out.read_bytes() == CONTENT and out.stat().st_nlink == 2


def test_out_with_register_is_refused_before_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from invaria.cli import main

    monkeypatch.delenv("INVARIA_DATABASE_URL", raising=False)  # any connection would fail
    out = tmp_path / "report.json"
    args = ["analytics", "report", "--access-profile", str(tmp_path / "absent.json")]
    assert main([*args, "--as-of", "2026-10-20T00:00:00Z", "--out", str(out), "--register"]) == 2
    err = capsys.readouterr().err
    assert "cannot be combined with --register in this version" in err
    assert "INVARIA_DATABASE_URL" not in err  # refused before any connection or profile read
    assert list(tmp_path.iterdir()) == []
