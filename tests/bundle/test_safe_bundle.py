"""Safe writing of bundles and single outputs.

Synthetic, offline: the shared writer's directory publication without replacement, the
failure model, modes, and the three commands (``evaluate --out``, ``bundle``) keeping their
bytes. ``ledger-verify --out`` is in tests/stellar/test_ledger_proof.py (it needs the CLI).
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from invaria import safe_write
from invaria.bundle import build_bundle, verify_bundle
from invaria.cli import main
from invaria.corpus_loader import Corpus, load_corpus
from invaria.engine.evaluate import evaluate
from invaria.engine.explain import export_evaluation
from invaria.safe_write import DestinationError, NewDirectory, Unconfirmed, WriteFailure

CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"
FILES = {"a.json": b"{}\n", "raw/b.csv": b"x,y\n", "raw/deep/c.txt": b"c", "manifest.json": b"m"}


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return load_corpus(CORPUS_DIR)


def temporaries(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith(".invaria-"))


def publish(path: Path, files: dict[str, bytes] = FILES) -> NewDirectory:
    target = NewDirectory(path)
    target.prepare()
    try:
        target.publish(files)
    finally:
        target.close()
    return target


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ----------------------------------------------------------------- the directory


def test_a_new_directory_is_complete_private_and_leaves_no_temporary(tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    assert publish(out).published
    assert {str(p.relative_to(out)): p.read_bytes() for p in out.rglob("*") if p.is_file()} == (
        FILES
    )
    assert [mode(d) for d in (out, out / "raw", out / "raw/deep")] == [0o700] * 3
    assert {mode(p) for p in out.rglob("*") if p.is_file()} == {0o600}
    assert temporaries(tmp_path) == []


def test_the_existing_parent_keeps_its_mode(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o755)
    os.chmod(parent, 0o755)
    publish(parent / "bundle")
    assert mode(parent) == 0o755


def test_existing_destinations_are_refused_even_an_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    (tmp_path / "full").mkdir()
    (tmp_path / "full/keep.txt").write_bytes(b"theirs")
    (tmp_path / "file").write_bytes(b"theirs")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "dangling").symlink_to(tmp_path / "elsewhere/target")
    for name, reason in (
        ("empty", "exists"),
        ("full", "exists"),
        ("file", "exists"),
        ("dangling", "symlink"),
    ):
        target = NewDirectory(tmp_path / name)
        with pytest.raises(DestinationError) as caught:
            target.prepare()
        assert caught.value.reason == reason, name
    assert list((tmp_path / "empty").iterdir()) == []
    assert (tmp_path / "full/keep.txt").read_bytes() == b"theirs"
    assert not (tmp_path / "elsewhere/target").exists()
    assert temporaries(tmp_path) == []


def test_links_in_path_components_are_refused_and_parents_created_safely(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "via").symlink_to(tmp_path / "real")
    with pytest.raises(DestinationError) as caught:
        NewDirectory(tmp_path / "via/bundle", create_parents=True).prepare()
    assert caught.value.reason == "symlink" and list((tmp_path / "real").iterdir()) == []
    with pytest.raises(DestinationError):
        NewDirectory(tmp_path / "missing/bundle").prepare()  # parents only when asked
    target = NewDirectory(tmp_path / "a/b/bundle", create_parents=True)
    target.prepare()
    target.publish(FILES)
    target.close()
    assert mode(tmp_path / "a") == 0o700 and mode(tmp_path / "a/b") == 0o700


def test_a_destination_created_concurrently_is_never_replaced(tmp_path: Path) -> None:
    """A plain rename would silently replace an empty directory: RENAME_NOREPLACE does not."""
    out = tmp_path / "bundle"
    target = NewDirectory(out)
    target.prepare()
    out.mkdir()  # another process, between the check and the publication
    with pytest.raises(WriteFailure) as caught:
        target.publish(FILES)
    target.close()
    assert caught.value.failure == "destination_exists" and not caught.value.leftover
    assert list(out.iterdir()) == [] and temporaries(tmp_path) == []


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (OSError(errno.ENOSPC, "No space left on device"), "write_error"),
        (KeyboardInterrupt(), "interrupted"),
    ],
)
def test_a_failure_during_construction_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException, failure: str
) -> None:
    out = tmp_path / "bundle"
    target = NewDirectory(out)
    target.prepare()
    real_write, calls = os.write, []

    def write(fd: int, data: Any) -> int:
        calls.append(fd)
        if len(calls) == 3:  # in the middle of the bundle
            raise error
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", write)
    with pytest.raises(WriteFailure) as caught:
        target.publish(FILES)
    monkeypatch.undo()
    target.close()
    assert caught.value.failure == failure and not caught.value.leftover
    assert not out.exists() and list(tmp_path.iterdir()) == []


def test_a_publication_error_cleans_only_this_runs_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "bundle"
    target = NewDirectory(out)
    target.prepare()

    def refuse(*args: Any) -> None:
        raise OSError(errno.EINVAL, "RENAME_NOREPLACE not supported here")

    monkeypatch.setattr(safe_write, "rename_noreplace", refuse)
    with pytest.raises(WriteFailure) as caught:
        target.publish(FILES)
    target.close()
    assert caught.value.failure == "publish_error"
    assert list(tmp_path.iterdir()) == []


def test_something_foreign_in_the_temporary_is_never_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "bundle"
    target = NewDirectory(out)
    target.prepare()

    def refuse(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
        (tmp_path / src / "foreign.txt").write_bytes(b"not ours")
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(safe_write, "rename_noreplace", refuse)
    with pytest.raises(WriteFailure) as caught:
        target.publish(FILES)
    target.close()
    assert caught.value.leftover and not out.exists()
    (left,) = temporaries(tmp_path)
    assert [p.name for p in (tmp_path / left).iterdir()] == ["foreign.txt"]


def test_an_interruption_just_after_publishing_is_reported_as_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "bundle"
    target = NewDirectory(out)
    target.prepare()
    real = safe_write.rename_noreplace

    def rename_then_interrupt(*args: Any) -> None:
        real(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr(safe_write, "rename_noreplace", rename_then_interrupt)
    with pytest.raises(Unconfirmed):
        target.publish(FILES)
    target.close()
    assert target.published and (out / "manifest.json").read_bytes() == b"m"
    assert temporaries(tmp_path) == []


def test_a_failed_parent_sync_after_publishing_is_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "bundle"
    target = NewDirectory(out)
    target.prepare()
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if fd == target.dir_fd:
            raise OSError(errno.EIO, "I/O error")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(Unconfirmed):
        target.publish(FILES)
    monkeypatch.undo()
    target.close()
    assert target.published and (out / "a.json").read_bytes() == b"{}\n"


def test_a_renamed_path_component_does_not_redirect_the_publication(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    (tmp_path / "attacker").mkdir()
    target = NewDirectory(tmp_path / "dir/bundle")
    target.prepare()
    (tmp_path / "dir").rename(tmp_path / "moved")
    (tmp_path / "dir").symlink_to(tmp_path / "attacker")
    target.publish(FILES)
    target.close()
    # Published in the opened directory; the textual path now leads elsewhere.
    assert (tmp_path / "moved/bundle/manifest.json").exists()
    assert list((tmp_path / "attacker").iterdir()) == []


def test_without_renameat2_directories_are_refused_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(safe_write, "_RENAMEAT2", None)
    with pytest.raises(DestinationError) as caught:
        NewDirectory(tmp_path / "bundle").prepare()
    assert caught.value.reason == "unsupported" and list(tmp_path.iterdir()) == []


def test_unsafe_relative_paths_are_refused_before_creating_anything(tmp_path: Path) -> None:
    target = NewDirectory(tmp_path / "bundle")
    target.prepare()
    for bad in ("../x", "a//b", "/abs", "a/./b"):
        with pytest.raises(ValueError):
            target.publish({bad: b"x"})
    target.close()
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------------ the commands


def test_bundle_keeps_its_bytes_and_verifies(corpus: Corpus, tmp_path: Path) -> None:
    inputs = corpus.inputs_for("K3")
    out = tmp_path / "new/k3"  # a missing parent is created, as before
    manifest = build_bundle(out, inputs, evaluate(inputs), mode="as_known_now")
    golden = Path(__file__).resolve().parents[1] / "fixtures/bundles/K3-engine-0.10.0"
    assert {p.name: p.read_bytes() for p in out.iterdir()} == {
        p.name: p.read_bytes() for p in golden.iterdir() if p.is_file()
    }
    assert manifest.bundle_id and verify_bundle(out).status == "REPRODUCED"
    assert mode(out) == 0o700 and {mode(p) for p in out.iterdir()} == {0o600}


def test_cli_refuses_existing_destinations_and_explains_how_to_repeat(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = str(CORPUS_DIR)
    (tmp_path / "empty").mkdir()
    assert main(["bundle", corpus, "K3", str(tmp_path / "empty")]) == 2
    assert "repeat with a new destination" in capsys.readouterr().err
    assert list((tmp_path / "empty").iterdir()) == []
    assert main(["bundle", corpus, "K3", str(tmp_path / "fresh")]) == 0
    assert "new directory 0700" in capsys.readouterr().out
    assert main(["verify", str(tmp_path / "fresh")]) == 0


def test_evaluate_out_never_overwrites_and_keeps_its_bytes(
    corpus: Corpus, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "k3.json"
    assert main(["evaluate", str(CORPUS_DIR), "K3", "--out", str(out)]) == 0
    inputs = corpus.inputs_for("K3")
    expected = export_evaluation(corpus.scenarios["K3"].snapshot, evaluate(inputs))
    assert out.read_bytes() == expected.encode("utf-8") and mode(out) == 0o600
    capsys.readouterr()
    assert main(["evaluate", str(CORPUS_DIR), "K3", "--out", str(out)]) == 2
    assert "never overwritten" in capsys.readouterr().err
    assert out.read_bytes() == expected.encode("utf-8")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "link.json").symlink_to(tmp_path / "elsewhere/new.json")
    assert main(["evaluate", str(CORPUS_DIR), "K3", "--out", str(tmp_path / "link.json")]) == 2
    assert not (tmp_path / "elsewhere/new.json").exists()
