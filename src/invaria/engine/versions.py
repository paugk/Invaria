"""Engine versions: which evaluate new conclusions and which only reproduce history.

- current: selected for new evaluations;
- retired: never selected for new evaluations, but replayed under its label (by a
  compatibility implementation, ``ENGINE_IMPLEMENTATION``) to reproduce a conclusion
  recorded with it. Reproducing an old conclusion does not
  validate it under the current semantics;
- blocked: never replayed (its code is not kept as a trusted runtime); a conclusion
  naming it is reported as such, and no other engine is substituted.

Pure constants: importing this module runs no engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

SUBSCRIPTION_ENGINE_REF = "invaria-engine@0.10.0"
REDEMPTION_ENGINE_REF = "invaria-redemption-engine@0.11.0"
SUBSCRIPTION_ENGINE_0_1_0 = "invaria-engine@0.1.0"
SUBSCRIPTION_ENGINE_0_2_0 = "invaria-engine@0.2.0"
SUBSCRIPTION_ENGINE_0_3_0 = "invaria-engine@0.3.0"
SUBSCRIPTION_ENGINE_0_4_0 = "invaria-engine@0.4.0"
SUBSCRIPTION_ENGINE_0_5_0 = "invaria-engine@0.5.0"
SUBSCRIPTION_ENGINE_0_6_0 = "invaria-engine@0.6.0"
SUBSCRIPTION_ENGINE_0_7_0 = "invaria-engine@0.7.0"
SUBSCRIPTION_ENGINE_0_8_0 = "invaria-engine@0.8.0"
SUBSCRIPTION_ENGINE_0_9_0 = "invaria-engine@0.9.0"
REDEMPTION_ENGINE_0_4_0 = "invaria-redemption-engine@0.4.0"
REDEMPTION_ENGINE_0_5_0 = "invaria-redemption-engine@0.5.0"
REDEMPTION_ENGINE_0_6_0 = "invaria-redemption-engine@0.6.0"
REDEMPTION_ENGINE_0_7_0 = "invaria-redemption-engine@0.7.0"
REDEMPTION_ENGINE_0_8_0 = "invaria-redemption-engine@0.8.0"
REDEMPTION_ENGINE_0_9_0 = "invaria-redemption-engine@0.9.0"
REDEMPTION_ENGINE_0_10_0 = "invaria-redemption-engine@0.10.0"

CURRENT_ENGINES: frozenset[str] = frozenset({SUBSCRIPTION_ENGINE_REF, REDEMPTION_ENGINE_REF})
# The operation type each engine evaluates: a bundle naming an engine of another type is
# never "reproduced" by it.
ENGINE_OPERATION: Mapping[str, str] = {
    SUBSCRIPTION_ENGINE_REF: "subscription",
    SUBSCRIPTION_ENGINE_0_1_0: "subscription",
    SUBSCRIPTION_ENGINE_0_2_0: "subscription",
    SUBSCRIPTION_ENGINE_0_3_0: "subscription",
    SUBSCRIPTION_ENGINE_0_4_0: "subscription",
    SUBSCRIPTION_ENGINE_0_5_0: "subscription",
    SUBSCRIPTION_ENGINE_0_6_0: "subscription",
    SUBSCRIPTION_ENGINE_0_7_0: "subscription",
    SUBSCRIPTION_ENGINE_0_8_0: "subscription",
    SUBSCRIPTION_ENGINE_0_9_0: "subscription",
    REDEMPTION_ENGINE_REF: "redemption",
    REDEMPTION_ENGINE_0_4_0: "redemption",
    REDEMPTION_ENGINE_0_5_0: "redemption",
    REDEMPTION_ENGINE_0_6_0: "redemption",
    REDEMPTION_ENGINE_0_7_0: "redemption",
    REDEMPTION_ENGINE_0_8_0: "redemption",
    REDEMPTION_ENGINE_0_9_0: "redemption",
    REDEMPTION_ENGINE_0_10_0: "redemption",
}

# Admission policy: why an engine is no longer selected for new evaluations.
_INV013 = (
    "retired on 2026-10-07 for new evaluations: it ignores chain "
    "effects bearing on the operation and counts every quarantined record even when the "
    "profile scopes them; kept only to reproduce the conclusions recorded with it"
)
_CLAWBACK = (
    "retired on 2026-10-07 for new evaluations: it lets a "
    "clawback of the investor turn an equal or short delivery comparison into UNKNOWN, "
    "although a clawback can neither complete nor undo a linked delivery; kept only to "
    "reproduce the conclusions recorded with it"
)
_UNRESOLVED = (
    "retired on 2026-10-07 for new evaluations: it treats a "
    "chain effect naming a participant the evidence does not identify (e.g. a claimable "
    "balance whose history could not be read) as foreign to the operation, although "
    "unknown participants can never be ruled out; kept only to reproduce the conclusions "
    "recorded with it"
)
_BOUNDED = (
    "retired on 2026-10-07 for new evaluations: a quarantined record bearing on "
    "the operation makes the whole delivery comparison UNKNOWN before it is made, so an "
    "identified clawback with incomplete correspondence loses a demonstrated short or "
    "excess delivery that it could not change, and an ExecutionLink reaches the "
    "evaluation only by erasing the record's known addresses; kept only to reproduce the "
    "conclusions recorded with it"
)
_PROFILE_PROMISE = (
    "retired on 2026-10-07 for new evaluations: it applies the per-control "
    "quarantine of 0.5.0 to any profile that scopes the on-chain quarantine, so under "
    "fund-subscription-testnet@1.1.0, which promised that a quarantined record bearing on "
    "the operation blocks the comparison, it silently changed that promise; it also takes "
    "the most recent certificate of the source, so a later Horizon certificate hides the "
    "quarantine of the SAC route, and it trusts a record's nature and ledger operation as "
    "written; kept only to reproduce the conclusions recorded with it"
)
_REDEMPTION_INTEGRITY = (
    "retired on 2026-10-07 for new evaluations: an equal burn passes as positive "
    "evidence whatever the quarantine of the chain coverage says about it, it takes the "
    "most recent certificate of the source, so a later Horizon certificate hides the "
    "quarantine of the SAC route, and it trusts a record's parties as written; kept only to "
    "reproduce the conclusions recorded with it"
)
_MUXED = (
    "retired on 2026-10-07 for new evaluations: it reads a "
    "token movement only by its base accounts, so a movement to or from a muxed sub-account "
    "would be attributed through an IdentityLink of the base account, which does not approve "
    "the sub-account; its adapter never produced one (it quarantined muxed payments), so no "
    "conclusion recorded with it contains one; kept only to reproduce those conclusions"
)
_PROVENANCE = (
    "retired on 2026-10-08 for new evaluations: "
    "it evaluates evidence produced by a mapping its profile does not admit and "
    "states the profile's mappings as if they had produced it; an unlinked movement to an "
    "unattributed muxed sub-account leaves an equal or short comparison UNKNOWN beside a "
    "linked delivery, although under explicit execution links it can never count; and a "
    "linked delivery to an address without an approved link blocks every comparison, losing "
    "an excess it cannot undo; kept only to reproduce the conclusions recorded with it"
)
_REDEMPTION_PROVENANCE = (
    "retired on 2026-10-08 for new evaluations: "
    "it evaluates evidence produced by a mapping its "
    "profile does not admit and states the profile's mappings as if they had produced it; an "
    "unlinked muxed movement leaves an equal or short burn comparison UNKNOWN beside a linked "
    "burn, although under explicit execution links it can never be one; a linked burn from an "
    "address without an approved link blocks every comparison, losing an excess it cannot "
    "undo; and it shows the absence of settlement with a chain certificate that declares it "
    "cannot see a claimable balance clawback or claim, which could be that settlement; kept "
    "only to reproduce the conclusions recorded with it"
)
_SCOPELESS = (
    "retired on 2026-10-08 for new evaluations: under a profile that declares "
    "absence_needs_chain_scope it would still let an on-chain certificate without a declared "
    "chain scope show that no further delivery reached the investor, so an equal or short "
    "token comparison could pass on coverage that says nothing about what it read; kept only "
    "to reproduce the conclusions recorded with it"
)
_REDEMPTION_SCOPELESS = (
    "retired on 2026-10-08 for new evaluations: under a profile that declares "
    "absence_needs_chain_scope it would still let an on-chain certificate without a declared "
    "chain scope (or with one that does not declare what it leaves out of sight) show the "
    "absence of settlement; kept only to reproduce the conclusions recorded with it"
)
_INCOHERENT_SCOPE = (
    "retired on 2026-10-08 for new evaluations: under a profile that "
    "declares completeness_needs_coverage it would accept a chain scope whose declaration is "
    "incompatible with what its route and role observe (an empty not_covered for a holder), "
    "or one of another representation of the profile than the movements evaluated; kept only "
    "to reproduce the conclusions recorded with it"
)
_REDEMPTION_INCOMPLETE = (
    "retired on 2026-10-08 for new evaluations: under a profile that "
    "declares completeness_needs_coverage it would conclude an equal or short burn comparison "
    "without coverage showing that the burns observed are all the burns, and accept an "
    "incoherent chain scope or one of another representation; kept only to reproduce the "
    "conclusions recorded with it"
)
RETIRED_ENGINES: Mapping[str, str] = {
    SUBSCRIPTION_ENGINE_0_1_0: f"{_INV013}; superseded by {SUBSCRIPTION_ENGINE_REF}",
    SUBSCRIPTION_ENGINE_0_2_0: f"{_CLAWBACK}; superseded by {SUBSCRIPTION_ENGINE_REF}",
    SUBSCRIPTION_ENGINE_0_3_0: f"{_UNRESOLVED}; superseded by {SUBSCRIPTION_ENGINE_REF}",
    SUBSCRIPTION_ENGINE_0_4_0: f"{_BOUNDED}; superseded by {SUBSCRIPTION_ENGINE_REF}",
    SUBSCRIPTION_ENGINE_0_5_0: f"{_PROFILE_PROMISE}; superseded by {SUBSCRIPTION_ENGINE_REF}",
    SUBSCRIPTION_ENGINE_0_6_0: f"{_MUXED}; superseded by {SUBSCRIPTION_ENGINE_REF}",
    SUBSCRIPTION_ENGINE_0_7_0: f"{_PROVENANCE}; superseded by {SUBSCRIPTION_ENGINE_REF}",
    SUBSCRIPTION_ENGINE_0_8_0: f"{_SCOPELESS}; superseded by {SUBSCRIPTION_ENGINE_REF}",
    SUBSCRIPTION_ENGINE_0_9_0: f"{_INCOHERENT_SCOPE}; superseded by {SUBSCRIPTION_ENGINE_REF}",
    REDEMPTION_ENGINE_0_4_0: f"{_INV013}; superseded by {REDEMPTION_ENGINE_REF}",
    REDEMPTION_ENGINE_0_5_0: f"{_UNRESOLVED}; superseded by {REDEMPTION_ENGINE_REF}",
    REDEMPTION_ENGINE_0_6_0: f"{_REDEMPTION_INTEGRITY}; superseded by {REDEMPTION_ENGINE_REF}",
    REDEMPTION_ENGINE_0_7_0: f"{_MUXED}; superseded by {REDEMPTION_ENGINE_REF}",
    REDEMPTION_ENGINE_0_8_0: (f"{_REDEMPTION_PROVENANCE}; superseded by {REDEMPTION_ENGINE_REF}"),
    REDEMPTION_ENGINE_0_9_0: (f"{_REDEMPTION_SCOPELESS}; superseded by {REDEMPTION_ENGINE_REF}"),
    REDEMPTION_ENGINE_0_10_0: (f"{_REDEMPTION_INCOMPLETE}; superseded by {REDEMPTION_ENGINE_REF}"),
}

# Implementation and provenance: what code actually runs for each engine label. Kept
# apart from admission and from result coincidence (a reproduced result does not prove
# that the code is the historical one).
# Every compatibility implementation: its engine never received a muxed movement.
_UNSUPPORTED_MUXED = (
    "; given a muxed movement, which none of its recorded conclusions contains, it "
    "answers UNSUPPORTED instead of attributing it through the base account"
)
# Every compatibility implementation: replay reproduces a recorded conclusion
# with its original evidence and provenance, so it never applies the mapping admission of
# new evaluations, and it states no evidence mappings, as its engine did not.
_REPLAY_PROVENANCE = (
    "; as a replay it does not refuse evidence whose mapping the profile does not admit (only "
    "new evaluations do) and states no evidence mappings, as its engine did not"
)
EngineImplementation = Literal["current", "compatibility"]
_IMPLEMENTATION: Mapping[str, tuple[EngineImplementation, str]] = {
    SUBSCRIPTION_ENGINE_REF: ("current", "this release's code"),
    REDEMPTION_ENGINE_REF: ("current", "this release's code"),
    SUBSCRIPTION_ENGINE_0_1_0: (
        "compatibility",
        "current code with the later rules off, not the historical code; the historical "
        "source was recovered from public commit c736292 (evaluate.py unchanged since "
        "cb6a299; tests/fixtures/engines/invaria-engine-0.1.0) and gives the same result "
        "documents on the 13 recorded inputs it can parse; identity is not claimed"
        + _UNSUPPORTED_MUXED,
    ),
    SUBSCRIPTION_ENGINE_0_3_0: (
        "compatibility",
        "current code with the unresolved-participant rule of 0.4.0 off; the 0.3.0 code is "
        "this repository's own (never published, no Git history), anchored by the golden "
        "bundle tests/fixtures/bundles/K3-engine-0.3.0; identity is not claimed"
        + _UNSUPPORTED_MUXED,
    ),
    SUBSCRIPTION_ENGINE_0_4_0: (
        "compatibility",
        "current code with the per-control quarantine of 0.5.0 off, reading an ExecutionLink "
        "on a quarantined record as unknown parties (what the 0.4.0 adapter wrote); the "
        "0.4.0 code is this repository's own (never published, no Git history), anchored by "
        "the golden bundle tests/fixtures/bundles/K3-engine-0.4.0; identity is not claimed, "
        "and reproducing the golden does not demonstrate equivalence on every input"
        + _UNSUPPORTED_MUXED,
    ),
    SUBSCRIPTION_ENGINE_0_5_0: (
        "compatibility",
        "current code with the rules of 0.6.0 off (profile-declared quarantine policy, active "
        "certificates by chain scope, classification supported by the snapshot); the 0.5.0 "
        "code is this repository's own (never published, no Git history), anchored by the "
        "golden bundle tests/fixtures/bundles/K3-engine-0.5.0; identity is not claimed, and "
        "reproducing the golden does not demonstrate equivalence on every input"
        + _UNSUPPORTED_MUXED,
    ),
    SUBSCRIPTION_ENGINE_0_6_0: (
        "compatibility",
        "current code with the muxed-account rules of 0.7.0 off; given a muxed movement, which "
        "no recorded conclusion contains, it makes the token comparison UNKNOWN "
        "(UNSUPPORTED_CAPABILITY) instead of guessing; the 0.6.0 code is this repository's own "
        "(never published, no Git history), anchored by the golden bundle "
        "tests/fixtures/bundles/K3-engine-0.6.0; identity is not claimed, and reproducing the "
        "golden does not demonstrate equivalence on every input",
    ),
    SUBSCRIPTION_ENGINE_0_7_0: (
        "compatibility",
        "current code with the rules of 0.8.0 off (the weighing of an unattributed muxed "
        "movement and the weighing of a linked delivery to an address without an approved "
        "link); the 0.7.0 code is this "
        "repository's own (never published, no Git history), anchored by the golden bundle "
        "tests/fixtures/bundles/K3-engine-0.7.0; identity is not claimed, and reproducing the "
        "golden does not demonstrate equivalence on every input",
    ),
    SUBSCRIPTION_ENGINE_0_9_0: (
        "compatibility",
        "current code with the rules of 0.10.0 off (a coherent chain scope "
        "of the representation used); the 0.9.0 code is this repository's own (never "
        "published, no Git history), anchored by the golden bundle "
        "tests/fixtures/bundles/K3-engine-0.9.0; identity is not claimed, and reproducing the "
        "golden does not demonstrate equivalence on every input",
    ),
    SUBSCRIPTION_ENGINE_0_8_0: (
        "compatibility",
        "current code with the rule of 0.9.0 off (the declared chain scope an absence "
        "needs); the 0.8.0 code is this repository's own (never published, no Git history), "
        "anchored by the golden bundle tests/fixtures/bundles/K3-engine-0.8.0; identity is not "
        "claimed, and reproducing the golden does not demonstrate equivalence on every input",
    ),
    SUBSCRIPTION_ENGINE_0_2_0: (
        "compatibility",
        "current code with the clawback rule of 0.3.0 off; the 0.2.0 code is this "
        "repository's own (never published, no Git history), anchored by the golden bundle "
        "tests/fixtures/bundles/K3-engine-0.2.0; identity is not claimed" + _UNSUPPORTED_MUXED,
    ),
    REDEMPTION_ENGINE_0_4_0: (
        "compatibility",
        "current code with the later rules off; no historical source is recoverable "
        "(never published, no Git history); contrasted only against bundles frozen on "
        "2026-10-07, so full equivalence with the historical engine is not demonstrated"
        + _UNSUPPORTED_MUXED,
    ),
    REDEMPTION_ENGINE_0_5_0: (
        "compatibility",
        "current code with the unresolved-participant rule of 0.6.0 off; the 0.5.0 code is "
        "this repository's own (never published, no Git history), anchored by the bundles "
        "tests/fixtures/bundles/redemption-0.5.0 frozen with it on 2026-10-07 before the "
        "change; identity is not claimed" + _UNSUPPORTED_MUXED,
    ),
    REDEMPTION_ENGINE_0_6_0: (
        "compatibility",
        "current code with the rules of 0.7.0 off (profile-declared per-control quarantine, "
        "active certificates by chain scope, records checked against the snapshot); the "
        "0.6.0 code is this repository's own (never published, no Git history), anchored by "
        "the bundles tests/fixtures/bundles/redemption-0.6.0 frozen with it on 2026-10-07 "
        "before the change; identity is not claimed" + _UNSUPPORTED_MUXED,
    ),
    REDEMPTION_ENGINE_0_7_0: (
        "compatibility",
        "current code with the muxed-account rules of 0.8.0 off; given a muxed movement, which "
        "no recorded conclusion contains, it makes the burn view UNKNOWN "
        "(UNSUPPORTED_CAPABILITY) instead of guessing; the 0.7.0 code is this repository's own "
        "(never published, no Git history), anchored by the bundles "
        "tests/fixtures/bundles/redemption-0.7.0 frozen with it on 2026-10-07 before the "
        "change; identity is not claimed",
    ),
    REDEMPTION_ENGINE_0_8_0: (
        "compatibility",
        "current code with the rules of 0.9.0 off (the weighing of an unattributed muxed "
        "movement, the weighing of a linked burn from an address without an approved link, "
        "and the chain scope required "
        "to show an absence of settlement); the 0.8.0 code is this repository's own (never "
        "published, no Git history), anchored by the bundles "
        "tests/fixtures/bundles/redemption-0.8.0 frozen with it on 2026-10-08 before the "
        "change; identity is not claimed",
    ),
    REDEMPTION_ENGINE_0_10_0: (
        "compatibility",
        "current code with the rules of 0.11.0 off (coverage for a burn "
        "comparison, a coherent chain scope of the representation used); the 0.10.0 code is "
        "this repository's own (never published, no Git history), anchored by the bundles "
        "tests/fixtures/bundles/redemption-0.10.0 frozen with it on 2026-10-08 before the "
        "change; identity is not claimed",
    ),
    REDEMPTION_ENGINE_0_9_0: (
        "compatibility",
        "current code with the rule of 0.10.0 off (the declared chain scope an "
        "absence of settlement needs); the 0.9.0 code is this repository's own (never "
        "published, no Git history), anchored by the bundles "
        "tests/fixtures/bundles/redemption-0.9.0 frozen with it on 2026-10-08 before the "
        "change; identity is not claimed",
    ),
}

ENGINE_IMPLEMENTATION: Mapping[str, tuple[EngineImplementation, str]] = {
    ref: (kind, text + _REPLAY_PROVENANCE if kind == "compatibility" else text)
    for ref, (kind, text) in _IMPLEMENTATION.items()
}

_SUPERSEDED_SYNTHETIC = (
    "superseded synthetic redemption semantics; withdrawn before any evaluation "
    "was persisted or published, and its code is not kept as a trusted runtime"
)
BLOCKED_ENGINES: Mapping[str, str] = {
    "invaria-redemption-engine@0.1.0": _SUPERSEDED_SYNTHETIC,
    "invaria-redemption-engine@0.2.0": _SUPERSEDED_SYNTHETIC,
    "invaria-redemption-engine@0.3.0": _SUPERSEDED_SYNTHETIC,
}

# Profile/engine combinations. A profile that declares ``quarantine_policy``
# (fund-subscription-testnet@1.2.0, fund-redemption-synthetic@1.5.0) is evaluated only by
# the engines that implement it; any other engine rejects it (EVALUATION_ERROR) instead of
# ignoring a declared rule. A profile without it keeps its own promise under every engine:
# with the current ones, a quarantined record bearing on the operation blocks as it did
# before the per-control quarantine (subscription 0.4.0, redemption 0.6.0), with only the
# two integrity corrections of the declared policy, which never relax a result (active
# certificates by chain scope, parties contrasted with the snapshot). The retired
# subscription 0.5.0 relaxed it under fund-subscription-testnet@1.1.0;
# it only reproduces what was recorded with it.
POLICY_ENGINES: frozenset[str] = frozenset(
    {
        SUBSCRIPTION_ENGINE_REF,
        SUBSCRIPTION_ENGINE_0_6_0,
        SUBSCRIPTION_ENGINE_0_7_0,
        REDEMPTION_ENGINE_REF,
        REDEMPTION_ENGINE_0_7_0,
        REDEMPTION_ENGINE_0_8_0,
        SUBSCRIPTION_ENGINE_0_8_0,
        REDEMPTION_ENGINE_0_9_0,
        SUBSCRIPTION_ENGINE_0_9_0,
        REDEMPTION_ENGINE_0_10_0,
    }
)
# A profile that declares how a muxed sub-account is attributed (``muxed_account_link``,
# fund-subscription-testnet@1.3.0) is evaluated only by the engines implementing it.
MUXED_ENGINES: frozenset[str] = frozenset(
    {
        SUBSCRIPTION_ENGINE_REF,
        SUBSCRIPTION_ENGINE_0_7_0,
        SUBSCRIPTION_ENGINE_0_8_0,
        SUBSCRIPTION_ENGINE_0_9_0,
    }
)
# A profile that admits further mappings for a source (``additional_mapping_refs``,
# fund-subscription-testnet@1.4.0) is evaluated only by the engines that state
# the provenance of their evidence.
PROVENANCE_ENGINES: frozenset[str] = frozenset(
    {
        SUBSCRIPTION_ENGINE_REF,
        REDEMPTION_ENGINE_REF,
        SUBSCRIPTION_ENGINE_0_8_0,
        REDEMPTION_ENGINE_0_9_0,
        SUBSCRIPTION_ENGINE_0_9_0,
        REDEMPTION_ENGINE_0_10_0,
    }
)
# A profile that declares ``absence_needs_chain_scope`` (fund-subscription-synthetic@1.1.0,
# fund-subscription-testnet@1.5.0, fund-redemption-synthetic@1.6.0) is evaluated
# only by the engines that refuse to show an absence with a certificate whose chain scope is
# absent or insufficient.
SCOPE_ENGINES: frozenset[str] = frozenset(
    {
        SUBSCRIPTION_ENGINE_REF,
        REDEMPTION_ENGINE_REF,
        SUBSCRIPTION_ENGINE_0_9_0,
        REDEMPTION_ENGINE_0_10_0,
    }
)
# A profile that declares ``completeness_needs_coverage`` (fund-subscription-synthetic@1.2.0,
# fund-subscription-testnet@1.6.0, fund-redemption-synthetic@1.7.0) is
# evaluated only by the engines that require a coherent chain scope of the representation
# used to affirm that the observed records are complete.
COMPLETENESS_ENGINES: frozenset[str] = frozenset({SUBSCRIPTION_ENGINE_REF, REDEMPTION_ENGINE_REF})


def profile_problem(
    engine_ref: str,
    profile_ref: str,
    declares_policy: bool,
    declares_muxed: bool = False,
    admits_more_mappings: bool = False,
    needs_chain_scope: bool = False,
    needs_completeness: bool = False,
) -> str | None:
    """Why ``engine_ref`` cannot evaluate a profile, or None."""
    if declares_policy and engine_ref not in POLICY_ENGINES:
        return (
            f"profile {profile_ref} declares a quarantine policy that engine {engine_ref} does "
            f"not implement; it is evaluated only by {', '.join(sorted(POLICY_ENGINES))}"
        )
    if declares_muxed and engine_ref not in MUXED_ENGINES:
        return (
            f"profile {profile_ref} declares how a muxed sub-account is attributed, which "
            f"engine {engine_ref} does not implement; it is evaluated only by "
            f"{', '.join(sorted(MUXED_ENGINES))}"
        )
    if admits_more_mappings and engine_ref not in PROVENANCE_ENGINES:
        return (
            f"profile {profile_ref} admits further mappings for a source, which engine "
            f"{engine_ref} does not implement; it is evaluated only by "
            f"{', '.join(sorted(PROVENANCE_ENGINES))}"
        )
    if needs_chain_scope and engine_ref not in SCOPE_ENGINES:
        return (
            f"profile {profile_ref} declares that an absence needs a certificate with a "
            f"sufficient chain scope, which engine {engine_ref} does not implement; it is "
            f"evaluated only by {', '.join(sorted(SCOPE_ENGINES))}"
        )
    if needs_completeness and engine_ref not in COMPLETENESS_ENGINES:
        return (
            f"profile {profile_ref} declares that affirming the observed chain records "
            f"complete needs coverage with a coherent chain scope, which engine {engine_ref} "
            f"does not implement; it is evaluated only by "
            f"{', '.join(sorted(COMPLETENESS_ENGINES))}"
        )
    return None


EngineStatus = Literal["current", "retired", "blocked", "unknown"]


def engine_status(engine_ref: str) -> EngineStatus:
    if engine_ref in CURRENT_ENGINES:
        return "current"
    if engine_ref in RETIRED_ENGINES:
        return "retired"
    if engine_ref in BLOCKED_ENGINES:
        return "blocked"
    return "unknown"
