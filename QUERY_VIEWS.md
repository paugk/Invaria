# Requirement diagnosis and obligations views

Two read-only views explain a stored evaluation. Neither evaluates evidence again, produces a result of its own or changes anything recorded: the financial result, `evaluation_id`, profiles, certificates, `coverage_level` and bundles stay as they are.

| View | Module | Offline CLI | Query service (`operations:read`) | Schema |
|---|---|---|---|---|
| Requirement diagnosis of one control | `src/invaria/query/diagnostics.py`, `src/invaria/engine/trace.py` | `invaria diagnose CORPUS SCENARIO [--control ID] [--engine REF] [--json]` | `QueryService.diagnose_control` | `schemas/query-control-diagnosis.schema.json` |
| Obligations of an operation | `src/invaria/query/obligations.py` | `invaria obligations CORPUS SCENARIO [--engine REF] [--json]` | `QueryService.get_operation_obligations(operation_ref, evaluation_id=None)` | `schemas/query-operation-obligations.schema.json` |

They are not MCP tools, REST routes or console pages. `get_missing_evidence` is unchanged.

## Requirement diagnosis

**Where the checks come from.** While it evaluates, the engine records every check it runs (`engine/trace.py`) on a side channel of the evaluation, never in `EvaluationResult`. The view replays the evaluation with its recorded engine on its closed snapshot, and contrasts the replayed result with the stored one:
- `reconstructed`: they match, and the checks are shown;
- `reconstruction_mismatch`: they differ; the stored result stands and no check detail is given;
- `unavailable_for_engine`: the engine is not instrumented.

**Only instrumented engines explain.** Only `invaria-engine@0.10.0` and `invaria-redemption-engine@0.11.0` record their checks, named by exact label. Retired engines (`invaria-engine@0.1.0` to `0.9.0`, `invaria-redemption-engine@0.4.0` to `0.10.0`) run as compatibility implementations, whose checks are not the historical engine's reasoning. They give `unavailable_for_engine` with their stored result intact: no historical diagnosis is invented.

**What each check says.**
- Its status: `satisfied`, `contradicted`, `undetermined`, `not_applicable` or `not_evaluated`.
- Its cause, when undetermined: for example `insufficient_coverage`, `missing_evidence` or `technical_error`.
- What it compared (left, right, delta), and the evidence and coverage it cited.
- `*` marks the check that fixed the control's result. Checks after it were not run and are `not_evaluated`, never failed.

**Next steps** name the authoritative source, the mapping and the minimum coverage the profile asks for. A technical error never asks for documents. Next steps never suggest linking by amount, time or memo.

```bash
uv run --locked invaria diagnose tests/fixtures/corpus/subscription-synthetic-1.2.0 K2 \
  --control subscription.token_units_vs_order
```

Expected (excerpt):
```
K2 subscription.token_units_vs_order: UNKNOWN INSUFFICIENT_COVERAGE (not concluded); diagnosis reconstructed, invaria-engine@0.10.0 (current)
  [satisfied]  comparison: observed equal: 1000.0000000 vs 1000.0000000 FUND_SHARE; observed only: the control is not concluded
  [undetermined]* completeness (insufficient_coverage): token_movement: cov-chain-k2 declares no chain scope; ...
      next (obtain_coverage): Obtain a coverage certificate produced by reading stellar-testnet-frozen for token_movement, ...
  [not_evaluated]  chain_effects: not run: the engine fixed the result at completeness before reaching this check; ...
```

The observed amounts are equal, but the chain certificate cannot show that no further delivery reached the account, so the control stays UNKNOWN. A diagnosis never raises `coverage_level`. An auxiliary report (ledger verification, trustline reconciliation) never makes an insufficient certificate sufficient.

## Obligations

**Profiles name controls, not obligations.** The explicit, versioned presentation catalogue `obligation-catalogue@1.0.0` groups the controls of three exact profile versions into the obligations their rules already state. Every obligation cites the profile paths it rests on, such as `controls[redemption.cash_vs_expected].right` or `payment_deadline.hours`, read from the evaluation's own profile.

| Profile | Obligation | Controls (role) | Applies |
|---|---|---|---|
| `fund-subscription-synthetic@1.2.0`, `fund-subscription-testnet@1.6.0` | `subscription.cash_settlement` | `cash_vs_order` (performance), `order_terms` (term) | always |
| | `subscription.unit_registration` | `ta_units_vs_order` (performance), `order_terms` (term) | always |
| | `subscription.token_delivery` | `token_units_vs_order` (performance), `order_terms` (term) | always |
| `fund-redemption-synthetic@1.7.0` | `redemption.cash_payment` | `cash_vs_expected` (performance), `payment_deadline` (timeliness), `price_vs_approved`, `units_within_position` and `declared_due_consistency` (term), `cancellation_valid` (applicability condition) | unless cancelled |
| | `redemption.register_debit` | `ta_units_vs_request` (performance), `units_within_position` (term), `cancellation_valid` (applicability condition) | unless cancelled |
| | `redemption.token_retirement` | `burn_vs_request` (performance), `units_within_position` (term), `cancellation_valid` (applicability condition) | unless cancelled |
| | `redemption.no_settlement_after_cancellation` | `no_settlement_after_cancellation` (performance), `cancellation_valid` (applicability condition) | with a valid cancellation |

The grouping and the roles are presentation choices, not rules, and one control may serve several obligations. Rules the profiles do not declare are listed as `not_projected`, with their reason:
- deadlines for the subscription, the register debit and the burn;
- the order between cash and tokens, or between payment and burn;
- reversal after a cancellation;
- position reservation.

**Any other profile** gives `unavailable_for_profile`: no obligation is inferred from similar names, and the stored control results are listed as recorded. A catalogued profile whose controls or paths do not fit its entry gives `mismatch_with_profile`.

**What each obligation keeps apart.**
- What the profile requires: the basis paths and values.
- Whether it applies: `applies`, `not_applicable`, `mixed` or `not_determined`. This comes from the engine's own check of the condition in each measuring control. The readable output prints each such check as `condition check <check> in <control>: <status> (<meaning>)`: it is a check of the condition, not the obligation's applicability.
- What the controls conclude: their stored results, each with its role.
- What was observed: the comparisons the engine recorded. Each is `decided` when it fixed the result, and `observed_only` when it did not.
- What cannot be determined: the undetermined check that fixed a control's result, with its next step.
- The evidence and coverage cited.

There is no aggregate economic state and no second result.

**How to read it.**
- **UNKNOWN is not unpaid.** It means not determined from the snapshot. It never states that a payment, registration or delivery did not happen, is owed or failed.
- **Not applicable is not fulfilled.** An obligation extinguished by a valid cancellation is `not_applicable`, never shown as met.
- **Observed equality is not completeness.** An `observed_only` comparison next to an UNKNOWN control does not prove that nothing else exists.
- **A FAIL stays visible.** It does not disappear next to an UNKNOWN control. A cancellation after the due time keeps the missed deadline (`mixed`), and a settlement despite a cancellation stays a FAIL.
- **A technical error is never shown as a missing document.**
- **History.** A historical engine without diagnosis keeps its stored results, with no observed values or blocks. A historical view uses the evaluation's own profile and engine.
- **Cost.** The view replays the evaluation once per query, with no persistent cache.

```bash
uv run --locked invaria obligations tests/fixtures/corpus/subscription-synthetic-1.2.0 K2 [--json]
uv run --locked invaria obligations tests/fixtures/corpus/redemption-synthetic-1.7.0 RD-CANCELLED-AFTER-DUE
uv run --locked invaria obligations tests/fixtures/corpus/redemption-synthetic-1.7.0 RD-CANCELLED-PAID
uv run --locked invaria obligations tests/fixtures/corpus/subscription-synthetic-1.2.0 K2 --engine invaria-engine@0.9.0
uv run --locked invaria obligations tests/fixtures/corpus/subscription-synthetic K2
```

Expected:
- **K2:** stored result UNKNOWN. `subscription.token_delivery` applies. Its `token_units_vs_order` is UNKNOWN `INSUFFICIENT_COVERAGE`, the observed comparison is `observed_only` (1000.0000000 vs 1000.0000000 FUND_SHARE), and the block is the completeness check, with an `obtain_coverage` next step.
- **RD-CANCELLED-AFTER-DUE:** `redemption.cash_payment` is `mixed`. `cash_vs_expected` is NOT_APPLICABLE (extinguished by the cancellation), and `payment_deadline` is FAIL `PAYMENT_MISSED` (concluded):
  ```
  applicability unless_cancelled: mixed
    condition check applicability in redemption.cash_vs_expected: not_applicable (condition does not hold: this control does not apply)
    condition check applicability in redemption.payment_deadline: satisfied (condition holds: this control applies)
  ```
- **RD-CANCELLED-PAID:** the three obligations that apply unless cancelled are `not_applicable`. `redemption.no_settlement_after_cancellation` applies, with FAIL `SETTLED_DESPITE_CANCELLATION`.
- **Retired engine `invaria-engine@0.9.0`:** `diagnosis unavailable_for_engine`, with the stored results and no observed values or blocks.
- **Profile 1.0.0:** `catalogue unavailable_for_profile`, with the stored control results.
