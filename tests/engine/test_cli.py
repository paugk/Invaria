from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from invaria.cli import main

CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures/corpus/subscription-synthetic"


def test_check_passes_on_corpus(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", str(CORPUS_DIR)]) == 0
    assert "9/9 scenarios reproduce their expected result" in capsys.readouterr().out


def test_check_fails_on_wrong_expectation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = tmp_path / "corpus"
    shutil.copytree(CORPUS_DIR, corpus)
    scenarios = json.loads((corpus / "scenarios.json").read_text("utf-8"))
    k2 = next(s for s in scenarios["scenarios"] if s["scenario_id"] == "K2")
    for control in k2["expected"]["controls"]:
        if control["control_id"] == "subscription.cash_vs_order":
            control.update(status="UNKNOWN", reason_code="MISSING_EVIDENCE")
    k2["expected"]["result"] = "UNKNOWN"
    (corpus / "scenarios.json").write_text(json.dumps(scenarios), "utf-8")
    assert main(["check", str(corpus)]) == 1
    assert "MISMATCH" in capsys.readouterr().out


def test_evaluate_exports_deterministically(tmp_path: Path) -> None:
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    assert main(["evaluate", str(CORPUS_DIR), "K3", "--out", str(first)]) == 0
    assert main(["evaluate", str(CORPUS_DIR), "K3", "--out", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()
    assert json.loads(first.read_text("utf-8"))["evaluation"]["result"] == "BREAK"


def test_unknown_scenario_and_missing_corpus(tmp_path: Path) -> None:
    assert main(["evaluate", str(CORPUS_DIR), "K99"]) == 2
    assert main(["check", str(tmp_path / "nothing")]) == 2


def test_demo_and_explain_change(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["demo", str(CORPUS_DIR)]) == 0
    out = capsys.readouterr().out
    assert "result UNKNOWN" in out and "result MATCH" in out and "result BREAK" in out
    assert main(["explain-change", str(CORPUS_DIR), "K2", "K3"]) == 0
    assert "evidence added: obs-B2" in capsys.readouterr().out
