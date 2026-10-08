"""Pure evaluation of a closed snapshot against an operation profile.

No clock, network or filesystem. The result depends only on the snapshot, the profile and
the member artifacts. Anything that cannot be decided is UNKNOWN with a reason code; a
technical error is UNKNOWN (EVALUATION_ERROR), never PASS.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from invaria.contracts.coverage import CoverageCertificate
from invaria.contracts.evaluation import (
    ControlResult,
    EvaluationResult,
    EvaluationVersions,
    ReasonCode,
    SnapshotRef,
    aggregate,
)
from invaria.contracts.identity import IdentityLink
from invaria.contracts.observation import (
    CashPayload,
    FactType,
    Observation,
    OrderPayload,
    TokenMovementPayload,
    UnitsPayload,
)
from invaria.contracts.profile import ControlSpec, OperationProfile
from invaria.contracts.quantity import Quantity

ENGINE_REF = "invaria-engine@0.1.0"
_LEVEL_RANK = {"provider_claimed": 0, "internally_checked": 1, "independently_verified": 2}
ASSUMPTIONS = (
    "Evaluation uses only the snapshot members; nothing is fetched or recomputed from sources.",
    "Per source record the highest revision wins; identical redeliveries count once.",
    "Per source and fact type the most recently recorded coverage certificate applies.",
    "Comparisons are exact in atoms; no tolerance, fees or rounding.",
    "The evaluation clock is recorded; no control in this profile uses deadlines.",
)


@dataclass(frozen=True)
class EvaluationInputs:
    snapshot: SnapshotRef
    profile: OperationProfile
    observations: Mapping[str, Observation]
    coverage: Mapping[str, CoverageCertificate]
    identity_links: Mapping[str, IdentityLink]


@dataclass(frozen=True)
class Operands:
    left: Quantity
    right: Quantity


@dataclass(frozen=True)
class Evaluation:
    result: EvaluationResult
    effective_observation_ids: tuple[str, ...]
    operands: Mapping[str, Operands] = field(default_factory=dict)


class _Undecided(Exception):
    def __init__(self, reason: ReasonCode, detail: str, refs: Sequence[str] = ()) -> None:
        super().__init__(detail)
        self.reason: ReasonCode = reason
        self.detail = detail
        self.refs = tuple(refs)


ViewStatus = Literal["asserted", "absent", "conflict", "withdrawn", "ambiguous", "unsupported"]


@dataclass(frozen=True)
class _FactView:
    fact_type: FactType
    status: ViewStatus
    observations: tuple[Observation, ...]
    refs: tuple[str, ...]
    detail: str


def _content(o: Observation) -> tuple[Any, ...]:
    return (
        o.kind,
        o.fact_type,
        o.instrument_id,
        o.representation_id,
        o.operation_ref,
        o.valid_time,
        o.payload,
    )


def _resolve_records(observations: Sequence[Observation]) -> list[tuple[str, list[Observation]]]:
    """Per source record: ('asserted'|'withdrawn'|'conflict', observations involved)."""
    records: dict[tuple[str, str], list[Observation]] = {}
    for o in observations:
        records.setdefault((o.source.source_id, o.source.record_key), []).append(o)
    resolved: list[tuple[str, list[Observation]]] = []
    for key in sorted(records):
        revisions = records[key]
        top = max(o.source.revision for o in revisions)
        latest = sorted(
            (o for o in revisions if o.source.revision == top),
            key=lambda o: (o.recorded_at, o.observation_id),
        )
        distinct: dict[tuple[Any, ...], Observation] = {}
        for o in latest:
            distinct.setdefault(_content(o), o)
        if len(distinct) > 1:
            resolved.append(("conflict", latest))
        else:
            representative = latest[0]
            state = "withdrawn" if representative.kind == "retraction" else "asserted"
            resolved.append((state, [representative]))
    return resolved


def _institutional_view(
    fact_type: FactType, members: Sequence[Observation], single: str | None
) -> _FactView:
    resolved = _resolve_records(members)
    conflicts = [o for state, obs in resolved if state == "conflict" for o in obs]
    if conflicts:
        ids = tuple(o.observation_id for o in conflicts)
        return _FactView(fact_type, "conflict", (), ids, "same record and revision differ")
    asserted = tuple(o for state, obs in resolved if state == "asserted" for o in obs)
    withdrawn = tuple(
        o.observation_id for state, obs in resolved if state == "withdrawn" for o in obs
    )
    if not asserted:
        if withdrawn:
            return _FactView(
                fact_type,
                "withdrawn",
                (),
                withdrawn,
                "the only supporting record was withdrawn by its source",
            )
        return _FactView(fact_type, "absent", (), (), "no observation in the snapshot")
    if len(asserted) > 1 and single is not None:
        ids = tuple(o.observation_id for o in asserted)
        return _FactView(fact_type, "unsupported", asserted, ids, f"unsupported: {single}")
    return _FactView(fact_type, "asserted", asserted, tuple(o.observation_id for o in asserted), "")


class _Evaluator:
    def __init__(self, inputs: EvaluationInputs) -> None:
        self.inputs = inputs
        self.snapshot = inputs.snapshot
        self.profile = inputs.profile
        self.members = [inputs.observations[i] for i in self.snapshot.observation_ids]
        self.certificates = [inputs.coverage[i] for i in self.snapshot.coverage_ids]
        self.links = [inputs.identity_links[i] for i in self.snapshot.identity_link_ids]
        self.views: dict[FactType, _FactView] = {}
        self.coverage_used: dict[FactType, str] = {}

    # ------------------------------------------------------------- evidence

    def _authoritative(self, fact_type: FactType) -> list[Observation]:
        source = self.profile.authority_for(fact_type)
        return [
            o
            for o in self.members
            if o.fact_type == fact_type
            and o.source.source_id == source
            and o.instrument_id == self.profile.instrument.instrument_id
        ]

    def view(self, fact_type: FactType) -> _FactView:
        if fact_type not in self.views:
            self.views[fact_type] = self._build_view(fact_type)
        return self.views[fact_type]

    def _build_view(self, fact_type: FactType) -> _FactView:
        operation = self.snapshot.operation_ref
        candidates = self._authoritative(fact_type)
        if fact_type != "token_movement":
            linked = [o for o in candidates if o.operation_ref == operation]
            single = {
                "cash_settled": "multiple_cash_payments",
                "units_registered": "partial_fill",
            }.get(fact_type, "multiple_orders")
            return _institutional_view(fact_type, linked, single)
        return self._token_view(candidates)

    def _token_view(self, candidates: Sequence[Observation]) -> _FactView:
        operation = self.snapshot.operation_ref
        representations = {r.representation_id: r for r in self.profile.representations}
        order = self.view("order_accepted")
        account = (
            order.observations[0].payload.account_ref
            if (
                order.status == "asserted"
                and isinstance(order.observations[0].payload, OrderPayload)
            )
            else None
        )
        relevant = [o for o in candidates if o.representation_id in representations]
        resolved = _resolve_records(relevant)
        conflicts = [o for state, obs in resolved if state == "conflict" for o in obs]
        if conflicts:
            return _FactView(
                "token_movement",
                "conflict",
                (),
                tuple(o.observation_id for o in conflicts),
                "same chain record delivered with different content",
            )
        asserted = [o for state, obs in resolved if state == "asserted" for o in obs]
        movements = [
            (o, o.payload) for o in asserted if isinstance(o.payload, TokenMovementPayload)
        ]
        successful = [(o, p) for o, p in movements if p.chain.tx_successful]

        def linked_address(o: Observation, p: TokenMovementPayload) -> bool:
            return any(
                link.account_ref == account
                and link.address == p.to_address
                and link.network == p.chain.network
                and link.valid_from <= o.valid_time
                and (link.valid_to is None or o.valid_time < link.valid_to)
                for link in self.links
            )

        explicit = [(o, p) for o, p in successful if o.operation_ref == operation]
        if not explicit:
            unlinked = [
                o.observation_id
                for o, p in successful
                if o.operation_ref is None and linked_address(o, p)
            ]
            if unlinked:
                return _FactView(
                    "token_movement",
                    "ambiguous",
                    (),
                    tuple(unlinked),
                    "movements to the investor address lack an execution link",
                )
            return _FactView(
                "token_movement", "absent", (), (), "no successful movement linked to the operation"
            )
        ids = tuple(o.observation_id for o, _ in explicit)
        for o, p in explicit:
            rep = representations[o.representation_id or ""]
            if p.from_address != rep.issuer:
                return _FactView(
                    "token_movement", "unsupported", (), ids, "unsupported: distributor_delivery"
                )
            if not linked_address(o, p):
                return _FactView(
                    "token_movement",
                    "ambiguous",
                    (),
                    ids,
                    "destination address has no approved identity link",
                )
        return _FactView("token_movement", "asserted", tuple(o for o, _ in explicit), ids, "")

    # ------------------------------------------------------------- coverage

    def _check(self, fact_type: FactType, order_time: datetime | None) -> None:
        view = self.view(fact_type)
        reason_by_status: dict[str, ReasonCode] = {
            "conflict": "SOURCE_CONFLICT",
            "withdrawn": "LOSS_OF_SUPPORT",
            "ambiguous": "AMBIGUOUS_MATCH",
            "unsupported": "UNSUPPORTED_CAPABILITY",
        }
        if view.status in reason_by_status:
            raise _Undecided(
                reason_by_status[view.status], f"{fact_type}: {view.detail}", view.refs
            )
        source = self.profile.authority_for(fact_type)
        requirement = next(
            r
            for r in self.profile.coverage_requirements
            if r.source_id == source and fact_type in r.fact_types
        )
        certs = [
            c
            for c in self.certificates
            if c.source_id == source
            and fact_type in c.fact_types
            and c.instrument_id == self.profile.instrument.instrument_id
        ]
        latest = max(certs, key=lambda c: (c.recorded_at, c.coverage_id)) if certs else None
        if latest is not None and latest.records_quarantined > 0:
            raise _Undecided(
                "QUARANTINED_INPUT",
                f"{fact_type}: {latest.records_quarantined} record(s) quarantined "
                f"in {latest.coverage_id}",
                (latest.coverage_id,),
            )
        if view.status == "absent":
            raise _Undecided("MISSING_EVIDENCE", f"{fact_type}: {view.detail}")
        if latest is None:
            raise _Undecided(
                "INSUFFICIENT_COVERAGE",
                f"{fact_type}: no coverage certificate from {source}",
                view.refs,
            )
        problems = []
        if latest.gaps:
            problems.append("declares gaps")
        if _LEVEL_RANK[latest.level] < _LEVEL_RANK[requirement.min_level]:
            problems.append(f"level {latest.level} < {requirement.min_level}")
        own_times = [o.valid_time for o in view.observations]
        start = order_time if order_time is not None else min(own_times)
        if latest.interval.start > start or latest.interval.end < self.snapshot.valid_at:
            problems.append("interval does not cover order time to valid_at")
        if problems:
            raise _Undecided(
                "INSUFFICIENT_COVERAGE",
                f"{fact_type}: {latest.coverage_id} " + "; ".join(problems),
                (*view.refs, latest.coverage_id),
            )
        self.coverage_used[fact_type] = latest.coverage_id

    # ------------------------------------------------------------- controls

    def order(self) -> OrderPayload:
        payload = self.view("order_accepted").observations[0].payload
        assert isinstance(payload, OrderPayload)
        return payload

    def operands(self, control_id: str) -> Operands:
        order = self.order()
        if control_id == "subscription.order_terms":
            return Operands(_price_times_units(order, self.profile), order.cash_amount)
        if control_id == "subscription.cash_vs_order":
            (cash,) = self.view("cash_settled").observations
            assert isinstance(cash.payload, CashPayload)
            return Operands(cash.payload.amount, order.cash_amount)
        if control_id == "subscription.ta_units_vs_order":
            (ta,) = self.view("units_registered").observations
            assert isinstance(ta.payload, UnitsPayload)
            return Operands(ta.payload.units, order.units)
        if control_id == "subscription.token_units_vs_order":
            units = [
                o.payload.units
                for o in self.view("token_movement").observations
                if isinstance(o.payload, TokenMovementPayload)
            ]
            return Operands(_sum(units), order.units)
        raise _Undecided("EVALUATION_ERROR", f"control {control_id} is not implemented")

    def control(self, spec: ControlSpec) -> tuple[ControlResult, Operands | None]:
        order_time: datetime | None = None
        refs: list[str] = []
        try:
            for fact_type in spec.requires:
                self._check(fact_type, order_time)
                view = self.view(fact_type)
                refs.extend(view.refs)
                refs.append(self.coverage_used[fact_type])
                if fact_type == "order_accepted":
                    order_time = view.observations[0].valid_time
            ops = self.operands(spec.control_id)
            if (ops.left.unit, ops.left.scale) != (ops.right.unit, ops.right.scale):
                raise _Undecided(
                    "UNSUPPORTED_CAPABILITY",
                    f"operands differ in unit/scale: {ops.left.unit}/"
                    f"{ops.left.scale} vs {ops.right.unit}/{ops.right.scale}",
                    refs,
                )
        except _Undecided as undecided:
            return _result(
                spec, "UNKNOWN", undecided.reason, undecided.detail, [*refs, *undecided.refs]
            ), None
        except Exception as error:  # a technical failure must never become PASS
            return _result(
                spec, "UNKNOWN", "EVALUATION_ERROR", f"{type(error).__name__}: {error}", refs
            ), None
        left, right = int(ops.left.atoms), int(ops.right.atoms)
        text = f"{ops.left.to_decimal_text()} vs {ops.right.to_decimal_text()} {ops.left.unit}"
        if left == right:
            return _result(spec, "PASS", "EXACT_MATCH", f"equal: {text}", refs), ops
        delta = Quantity(atoms=str(left - right), scale=ops.left.scale, unit=ops.left.unit)
        return _result(
            spec,
            "FAIL",
            spec.failure_code,
            f"{text}; delta {delta.to_decimal_text()} {delta.unit}",
            refs,
            delta,
        ), ops


def _price_times_units(order: OrderPayload, profile: OperationProfile) -> Quantity:
    units, price = order.units, order.price_per_unit
    instrument = profile.instrument
    if (units.unit, units.scale) != (instrument.unit, instrument.scale):
        raise _Undecided("UNSUPPORTED_CAPABILITY", "order units not in instrument unit/scale")
    if (price.unit, price.scale) != (profile.pricing.cash_unit, profile.pricing.cash_scale):
        raise _Undecided("UNSUPPORTED_CAPABILITY", "price not in profile cash unit/scale")
    product = int(units.atoms) * int(price.atoms)
    divisor = 10**units.scale
    if product % divisor:
        raise _Undecided("UNSUPPORTED_CAPABILITY", "units x price needs rounding (unsupported)")
    return Quantity(atoms=str(product // divisor), scale=price.scale, unit=price.unit)


def _sum(quantities: Sequence[Quantity]) -> Quantity:
    kinds = {(q.unit, q.scale) for q in quantities}
    if len(kinds) != 1:
        raise _Undecided("UNSUPPORTED_CAPABILITY", "movements in different units/scales")
    ((unit, scale),) = kinds
    return Quantity(atoms=str(sum(int(q.atoms) for q in quantities)), scale=scale, unit=unit)


def _result(
    spec: ControlSpec,
    status: Literal["PASS", "FAIL", "UNKNOWN"],
    reason: ReasonCode,
    detail: str,
    refs: Sequence[str],
    delta: Quantity | None = None,
) -> ControlResult:
    return ControlResult(
        control_id=spec.control_id,
        mandatory=spec.mandatory,
        status=status,
        reason_code=reason,
        delta=delta,
        reason=detail,
        evidence_refs=list(dict.fromkeys(refs)),
    )


def _snapshot_problem(inputs: EvaluationInputs) -> str | None:
    snapshot, profile = inputs.snapshot, inputs.profile
    if snapshot.profile_ref != profile.profile_ref:
        return f"snapshot profile {snapshot.profile_ref} != {profile.profile_ref}"
    if snapshot.rules_ref != profile.rules_ref:
        return f"snapshot rules {snapshot.rules_ref} != {profile.rules_ref}"
    if set(snapshot.mapping_refs) != {s.mapping_ref for s in profile.sources}:
        return "snapshot mapping_refs differ from profile sources"
    stores: list[tuple[list[str], Mapping[str, Any], str]] = [
        (snapshot.observation_ids, inputs.observations, "observation"),
        (snapshot.coverage_ids, inputs.coverage, "coverage"),
        (snapshot.identity_link_ids, inputs.identity_links, "identity link"),
    ]
    for ids, store, kind in stores:
        for member in ids:
            if member not in store:
                return f"{kind} {member} is a snapshot member but missing from the store"
            if store[member].recorded_at > snapshot.known_at:
                return f"{kind} {member} was recorded after known_at"
    return None


def evaluation_id(snapshot: SnapshotRef, profile_ref: str, engine_ref: str) -> str:
    material = json.dumps(
        {
            "snapshot": snapshot.model_dump(mode="json"),
            "profile": profile_ref,
            "engine": engine_ref,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "eval-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def evaluate(inputs: EvaluationInputs, *, engine_ref: str = ENGINE_REF) -> Evaluation:
    snapshot, profile = inputs.snapshot, inputs.profile
    problem = _snapshot_problem(inputs)
    controls: list[ControlResult] = []
    operands: dict[str, Operands] = {}
    effective: tuple[str, ...] = ()
    if problem is not None:
        controls = [
            _result(spec, "UNKNOWN", "EVALUATION_ERROR", problem, []) for spec in profile.controls
        ]
    else:
        evaluator = _Evaluator(inputs)
        for spec in profile.controls:
            control_result, ops = evaluator.control(spec)
            controls.append(control_result)
            if ops is not None:
                operands[spec.control_id] = ops
        for fact_type in ("order_accepted", "cash_settled", "units_registered", "token_movement"):
            view = evaluator.view(fact_type)
            if view.status == "asserted":
                effective += tuple(o.observation_id for o in view.observations)
    result = EvaluationResult(
        schema_version="1.0",
        evaluation_id=evaluation_id(snapshot, profile.profile_ref, engine_ref),
        snapshot_id=snapshot.snapshot_id,
        operation_ref=snapshot.operation_ref,
        result=aggregate(controls),
        controls=controls,
        versions=EvaluationVersions(
            casm="1.0",
            profile_ref=profile.profile_ref,
            rules_ref=profile.rules_ref,
            mapping_refs=list(snapshot.mapping_refs),
            engine_ref=engine_ref,
        ),
        evaluation_clock=snapshot.evaluation_clock,
        assumptions=list(ASSUMPTIONS),
    )
    return Evaluation(result, tuple(sorted(set(effective))), operands)
