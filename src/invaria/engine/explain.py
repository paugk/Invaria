"""Deterministic explanations derived from evaluation results. No language model."""

from __future__ import annotations

import json
from dataclasses import dataclass

from invaria.contracts.evaluation import SnapshotRef
from invaria.engine.evaluate import Evaluation

EXPORT_SCHEMA = "invaria-evaluation-export@1.0.0"


def explain(evaluation: Evaluation) -> list[str]:
    result = evaluation.result
    lines = [
        f"operation {result.operation_ref} | snapshot {result.snapshot_id} | "
        f"result {result.result}",
        f"engine {result.versions.engine_ref} | profile {result.versions.profile_ref} | "
        f"clock {result.evaluation_clock.isoformat()}",
    ]
    for control in result.controls:
        refs = ", ".join(control.evidence_refs) or "none"
        lines.append(
            f"  {control.status:<7} {control.control_id} [{control.reason_code}] "
            f"{control.reason} (evidence: {refs})"
        )
    lines.append(
        "  effective observations: " + (", ".join(evaluation.effective_observation_ids) or "none")
    )
    return lines


@dataclass(frozen=True)
class ControlChange:
    control_id: str
    before: str
    after: str


@dataclass(frozen=True)
class ConclusionChange:
    from_evaluation: str
    to_evaluation: str
    from_result: str
    to_result: str
    changed_controls: tuple[ControlChange, ...]
    added_evidence: tuple[str, ...]
    removed_evidence: tuple[str, ...]

    def lines(self) -> list[str]:
        out = [
            f"{self.from_result} -> {self.to_result} "
            f"({self.from_evaluation} -> {self.to_evaluation})"
        ]
        out += [f"  {c.control_id}: {c.before} -> {c.after}" for c in self.changed_controls]
        out.append("  evidence added: " + (", ".join(self.added_evidence) or "none"))
        out.append("  evidence removed: " + (", ".join(self.removed_evidence) or "none"))
        return out


def explain_change(old: Evaluation, new: Evaluation) -> ConclusionChange:
    """Structural difference between two evaluations of the same operation.

    Reports what changed in inputs and per control; it does not claim a unique root cause.
    """
    if old.result.operation_ref != new.result.operation_ref:
        raise ValueError("evaluations refer to different operations")
    before = {c.control_id: f"{c.status}/{c.reason_code}" for c in old.result.controls}
    after = {c.control_id: f"{c.status}/{c.reason_code}" for c in new.result.controls}
    changed = tuple(
        ControlChange(cid, before.get(cid, "absent"), after.get(cid, "absent"))
        for cid in sorted(set(before) | set(after))
        if before.get(cid) != after.get(cid)
    )
    old_ids, new_ids = set(old.effective_observation_ids), set(new.effective_observation_ids)
    return ConclusionChange(
        from_evaluation=old.result.evaluation_id,
        to_evaluation=new.result.evaluation_id,
        from_result=old.result.result,
        to_result=new.result.result,
        changed_controls=changed,
        added_evidence=tuple(sorted(new_ids - old_ids)),
        removed_evidence=tuple(sorted(old_ids - new_ids)),
    )


def export_evaluation(snapshot: SnapshotRef, evaluation: Evaluation) -> str:
    """Canonical JSON (sorted keys, no insignificant whitespace) of snapshot + evaluation.

    Minimal export: not signed, not a bundle, not a replay artifact.
    """
    if snapshot.snapshot_id != evaluation.result.snapshot_id:
        raise ValueError("snapshot does not match evaluation")
    document = {
        "export_schema": EXPORT_SCHEMA,
        "snapshot": snapshot.model_dump(mode="json"),
        "evaluation": evaluation.result.model_dump(mode="json"),
        "effective_observation_ids": list(evaluation.effective_observation_ids),
        "signed": False,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
