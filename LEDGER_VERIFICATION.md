# Ledger verification (Stellar testnet history archives)

This guide reproduces, offline, what the ledger verification module (`src/invaria/stellar/ledger_proof.py`, `src/invaria/stellar/ledger_completeness.py`, `src/invaria/stellar/trustline_reconciliation.py`) establishes for the demo DEMOA issuance. It uses only artifacts kept in this repository (`tests/fixtures/stellar/ledger-archive/`, `tests/fixtures/stellar/recordings/demoa-own/`). Nothing here downloads ledger state or re-runs Stellar Core.

## Scope

Scope of the module: anchored verification of inclusion, contrast of a replay, a completeness report of Classic `payment` operations in a declared ledger interval, and a reconciliation of one trustline's changes in a declared time interval.

The module does four separate things. None of them changes how the engines evaluate:
- certificates, profiles and coverage levels are unchanged, and the engines keep `provider_claimed` on-chain coverage;
- the limits of [CAPABILITIES.md](CAPABILITIES.md) still apply, including the absence-of-settlement limit and the completeness rules of the current profiles.

| Result | What it establishes | What it does not |
|---|---|---|
| **Anchored inclusion** | That a Classic operation is in a testnet checkpoint whose headers, transaction sets and result sets hash and chain to an anchor: the hash of the checkpoint ledger | Completeness, absences, contract events or balance changes. Including a failed transaction never shows a transfer |
| **Replay review** | That the kept meta of a replay of that checkpoint reproduces the anchored headers, transactions and results, and how its events and trustline changes compare with the observations | That the events are committed by consensus: no header commits them. They are derived from a replay. The review does not observe the original Stellar Core run |
| **Bounded completeness** | That, in a declared ledger interval, the provider's capture and the adapter's records hold exactly the Classic `payment` operations of the asset with the account as source or destination that the anchored history holds | All movements of the account (path payments, DEX fills, SAC, claimable balances, clawback), balance changes, or anything outside the interval |
| **Trustline reconciliation** | That, in a declared time interval, every change the replay's meta shows on the trustline of each approved address is explained by an anchored operation or an asset event of the same location, account and asset; with opening and closing state, net, and gross amounts where events show them | That deliveries are complete, or that the account received nothing else elsewhere. The trustline changes come from the replay's meta, which no header commits. It is a report: no control consumes it |

**Anchor and validators.** The anchor of checkpoint 5027711 comes from a recorded `stellar-core verify-checkpoints` run (`anchor-run/`). That run observed the consensus of the three SDF testnet validators `sdftest1` to `sdftest3`, with stellar-core 29.0.0 pinned by digest.
- **Imported, not observed.** Verifying with that record imports the anchor (`imported_with_provenance`); the verifier observes nothing itself.
- **Relative to the anchor.** Every result holds relative to the anchor. It depends on those validators' keys and on the archives they publish.
- **Same operator.** On testnet SDF also operates Horizon: the history archives are another system than the provider's database, not another operator.

## Requirements

- The locked environment: `uv sync --locked`.
- **Stellar CLI 28.1.0**, which the module uses to decode and re-encode XDR (every record must re-encode to its exact bytes). Official release tarball `stellar-cli-28.1.0-x86_64-unknown-linux-gnu.tar.gz`, sha256 `c1680deee94301d33ada7a17f98411e642a4248c727afbd2e43050d345746462`. `stellar --version` must start with `stellar 28.1.0`.
- No network and no Docker for sections 1 to 6: they are the offline verification.
- Network, and only for the commands that need it:
  - `invaria stellar ledger-fetch` downloads a checkpoint's files from a history archive over HTTPS, with size and time bounds;
  - `invaria stellar ledger-anchor` (and `ledger-verify --observe-anchor`) also needs Docker, to run the pinned stellar-core image (section 7);
  - repeating the replay (section 8) needs both, plus the ledger state.

  None of them is needed for, or run by, the offline verification, the tests or CI.

Run the module's tests with the CLI required, so that a missing CLI fails instead of skipping:

```bash
INVARIA_REQUIRE_STELLAR_CLI=1 uv run --locked pytest -q tests/stellar/test_ledger_proof.py
```

Without that variable the tests are skipped when the CLI is absent, and a skip is not a verification.

## 1. Build the observations to contrast (offline)

```bash
export F=tests/fixtures/stellar/ledger-archive/checkpoint-5027711
export STORE=$(mktemp -d)/store
uv run --locked invaria stellar ingest --target tests/fixtures/stellar/targets/demoa-own.json \
  --start-ledger 5027650 --end-ledger 5027672 --store "$STORE" \
  --links tests/fixtures/stellar/links/demoa-own.json --page-limit 2 \
  --recorded-at 2026-10-05T00:59:44Z --replay tests/fixtures/stellar/recordings/demoa-own
```

The adapter replays the recorded Horizon responses (`--page-limit 2` matches the recorded requests) and writes three Classic observations: T1, T2 (a failed transaction) and T3.

## 2. Anchored inclusion

```bash
uv run --locked invaria stellar ledger-verify --archive-dir "$F/archive" --ledger 5027711 \
  --anchor-run "$F/anchor-run/anchor-run.json" \
  --target tests/fixtures/stellar/targets/demoa-own.json --store "$STORE" --out inclusion.json
```

Expected: `evidence VERIFIED (checkpoint 5027711, anchor mode imported_with_provenance)`, T1, T2 and T3 `SUPPORTED`, and `overall CONSISTENT` (exit code 0).
- T1 and T3: `EXECUTED_PER_RESULT`.
- T2: `NOT_EXECUTED`; its inclusion shows the attempt and its fee, never a transfer.
- An anchor with another hash gives `ANCHOR_MISMATCH`, which is not proof of tampering. Changed files give `INCONSISTENT`.

## 3. Review of the imported replay

```bash
uv run --locked invaria stellar ledger-verify --archive-dir "$F/archive" --ledger 5027711 \
  --anchor-run "$F/anchor-run/anchor-run.json" \
  --target tests/fixtures/stellar/targets/demoa-own.json --store "$STORE" \
  --replay-dir "$F/replay" \
  --expect-replay-record-sha256 b7459be8c89d05d360e990cdf81bec4c7a5c7930deca6aa8f8d55c48deb9cdc2 \
  --expect-replay-revision-sha256 89b1b95f79f78710cd4ed89228e2947e6b4dea766b6bb0b427348110cab66ddd \
  --out replay.json
```

Every kept file is checked against `replay-run.json` (and the files added after the run, against its revision) **before** anything is decoded. A missing, altered, undeclared or symlinked file fails closed.

The report's `replay` section has four parts:
- `history_against_anchor`;
- `core_execution`: declared by the record, `observed_by_this_process: false`;
- `meta_integrity`;
- `correspondence`: `REPLAY_CONSISTENT`, with the exact list of what it checks and does not check, and the events.

Expected events:
- T1 and T3: `EVENT_MATCH`. A `mint`, since a Classic payment from the issuer emits `mint` under CAP-67, with the matching trustline change and the XLM fee reported apart. The `to_muxed_id` of the event carries the transaction memo `SUB-0001`; it is never read as a sub-account.
- T2: `NO_MOVEMENT_IN_TX`, which says nothing about the account's other transactions.

**The two records.** The kept replay has two separate records:
- **`replay-run.json`.** Written right after the run, from its facts: the run finished at 2026-10-08T23:54:36Z and the file was written at 23:58Z. Its sha256, `b7459be8…deb9cdc2`, was first computed on 2026-10-09, not during the run. It declares the meta, the log, the initial history state, the config and the download script.
- **`replay-run.revision-1.json`.** Created on 2026-10-09. It names the original record by its sha256 and declares the two files the original did not, `download.log` and `resources.log`, as `added_after_run`, with `hashes_contemporaneous: false`. The run wrote those files but did not hash them. Their bytes were compared that day with the run's working copies, and that comparison is all the revision attests.

Neither record was rebuilt or edited afterwards.

**What the expected hashes mean.**
- **Correspondence, not observation.** They show correspondence with the expected record, nothing more: not that the run happened, not that a declared provenance was observed, and not that whoever documented the run did so truthfully.
- **Same channel as the files.** Here they come from this repository, the same channel as the files. For an independent check, obtain them through a channel you trust.
- **Without them,** the files are only checked against the record that came with them (`supplier_declared`). The overall result is then `COHERENT_UNAUTHENTICATED`, exit code 1, never `CONSISTENT`.
- **The invariant.** `EventsAreConsistentWithEntryDiffs` checked the events while the original run produced them. It does not protect a copy modified afterwards.

## 4. Bounded completeness report

```bash
uv run --locked invaria stellar ledger-verify --archive-dir "$F/archive" --ledger 5027711 \
  --anchor-run "$F/anchor-run/anchor-run.json" \
  --target tests/fixtures/stellar/targets/demoa-own.json --store "$STORE" \
  --completeness-ledgers 5027650 5027672 \
  --horizon-recording tests/fixtures/stellar/recordings/demoa-own --out completeness.json
```

Expected: `classic payment completeness COMPLETE_IN_SCOPE in ledgers [5027650, 5027672]` and `overall CONSISTENT` (exit code 0).

**Scope:**
- Classic `payment` operations of `DEMOA:GCGGXYAKBIOKOIMVSUTXPEHRLJJU7VB7ZIYYKUFXYIVZTTFIWKEKTBEP` with `GC6XNJNZOULFUZJBYORCTVNWI6UX5AVOEDC7EXDQM77BPFMQA3VWOYLK` as source (explicit or the transaction's) or destination.
- Muxed addresses are decoded to their base only to enumerate; no identity is attributed.
- Inner transactions of fee bumps are included, and so are successful and failed transactions.
- Each operation is counted once per (transaction hash, operation index), never matched by amount.

The report keeps apart:
- the enumeration from the anchored transaction and result sets;
- the provider's capture: whether its pages chain over the interval, and whether its filters include failed transactions;
- the adapter's records;
- the differences.

A difference is a `PROVIDER_OMISSION` only when the capture is shown complete for the interval and its filters include the operation. Otherwise it is `CAPTURE_INCOMPLETE` or `SCOPE_INCOMPATIBLE`.

The `proven_absence` it states concerns those `payment` operations only, never all movements or balance changes.

## 5. Trustline reconciliation (report, replay required)

```bash
uv run --locked invaria stellar ledger-verify --archive-dir "$F/archive" --ledger 5027711 \
  --anchor-run "$F/anchor-run/anchor-run.json" \
  --target tests/fixtures/stellar/targets/demoa-own.json --store "$STORE" \
  --replay-dir "$F/replay" \
  --expect-replay-record-sha256 b7459be8c89d05d360e990cdf81bec4c7a5c7930deca6aa8f8d55c48deb9cdc2 \
  --expect-replay-revision-sha256 89b1b95f79f78710cd4ed89228e2947e6b4dea766b6bb0b427348110cab66ddd \
  --trustline-reconciliation 2026-10-05T00:57:20Z 2026-10-05T00:59:00Z \
  --approved-links tests/fixtures/corpus/subscription-testnet-1.6.0/identity_links.json \
  --account-ref acct-pseudo-0001 --out trustline.json
```

Expected: `trustline reconciliation RECONCILED_IN_SCOPE (authenticated_replay)` over ledgers 5027651 to 5027670, and `overall CONSISTENT` (exit code 0):
- the line of `GC6X…OYLK` for `DEMOA:GCGG…TBEP` is created in ledger 5027652 by a `change_trust`;
- T1 (ledger 5027658) and T3 (ledger 5027669) each add 10000000000, explained by the anchored payment and the asset events of the same operation;
- T2 (ledger 5027664) is a failed transaction: `NO_EXECUTED_MOVEMENT`; every fee (100 stroops of XLM) is reported apart;
- opening `ABSENT`, reconstructed from the complete sequence of changes in the window; closing 20000000000; net 20000000000; gross in 20000000000 and out 0.

**How it reads the evidence.**
- **Only an established replay.** The meta is read only when the artifacts match their record (`ARTIFACTS_MATCH`), the replay is `REPLAY_CONSISTENT`, and the decoded stream is the one the record names. Without the expected hashes the section is at most `RECONCILED_UNAUTHENTICATED` and the overall result `COHERENT_UNAUTHENTICATED` (exit code 1).
- **Interval.** Ledgers are chosen by the `close_time` of verified headers, `[start, end)` by default (`--interval-bounds closed` includes the end). If the interval starts before or ends after what the replay holds, time coverage is `INCOMPLETE`.
- **Approved addresses.** Each `IdentityLink` of `--account-ref` applies only within its own validity window. A muxed address (M) is `NOT_EXAMINABLE`: the trustline of its base G mixes all its sub-accounts. If an approved address that bears on the interval is not examined or not reconciled, the section is `INCOMPLETE`.
- **Opening state.** It is never observed: the replay keeps no initial state. It is reconstructed only from a complete sequence of changes in the window, never inferred from a creation alone; otherwise it is `NOT_ESTABLISHED`, never zero.
- **Correspondence.** By ledger, transaction, operation, account and asset identity (code and issuer), never by amount alone. Gross amounts come only from asset events; a change without events has gross `null` ("net only").
- **Outcomes.** A change without an explanation is `UNEXPLAINED` (`UNRESOLVED_CHANGES`, overall `NOT_ESTABLISHED`); an event without a matching change, a change in a failed transaction or a break in the chain of balances is `CONTRADICTED` (exit code 1).

**What it does not establish.** It reconstructs one trustline's changes within its scope, and depends on the replay's provenance. It does not show that deliveries are complete, that nothing reached the investor through another address, asset representation or route, or anything outside the interval. It changes no certificate, profile, coverage level or financial result, and the engines keep `provider_claimed`.

## 6. A simulated omission, without touching the fixtures

```bash
SIM=$(mktemp -d)/capture
python3 scripts/simulate_capture_omission.py tests/fixtures/stellar/recordings/demoa-own "$SIM" \
  00fd0148b4dc7d334169406bdc0af3829ffe53c72396c563b0077da4300d34bf
uv run --locked invaria stellar ledger-verify --archive-dir "$F/archive" --ledger 5027711 \
  --anchor-run "$F/anchor-run/anchor-run.json" \
  --target tests/fixtures/stellar/targets/demoa-own.json --store "$STORE" \
  --completeness-ledgers 5027650 5027672 --horizon-recording "$SIM"
git status --short tests/fixtures   # nothing changed
```

The script copies the recordings, removes T3 from the copied pages, and rewrites their sha256. Expected:
- `classic payment completeness GAPS_FOUND`;
- `PROVIDER_OMISSION` for T3 (ledger 5027669);
- `overall CONTRADICTED`, exit code 1.

This is a **simulated** omission, not a failure observed in Horizon. Deleting a page of the copy instead gives `CAPTURE_INCOMPLETE`, never an omission.

## 7. Observing the anchor again, or fetching a checkpoint (network; Docker for the anchor)

```bash
uv run --locked invaria stellar ledger-fetch --ledger 5027711 --out fetched-checkpoint
```

This downloads the checkpoint's `history`, `ledger`, `transactions` and `results` files from the SDF testnet archive. It writes their provenance (URL, sha256, size and time), with bounded size and time per file. The files are only evidence once `ledger-verify` checks them against an anchor.

```bash
uv run --locked invaria stellar ledger-anchor --config "$F/anchor-run/testnet.cfg" \
  --from-ledger 5027711 --out new-anchor-run
```

This runs `stellar-core verify-checkpoints` in the pinned image, as a non-root user. It reads only headers, and records the run with the sha256 of its config, anchors and filtered log. Passing `--observe-anchor CONFIG --anchor-dir DIR` to `ledger-verify` instead makes the anchor `observed_run`.

## 8. Repeating the replay is a different operation

Sections 3 to 6 review kept artifacts. **Repeating the replay** means running `stellar-core catchup 5027711/64 --trusted-checkpoint-hashes anchors.json` with the pinned image and the kept `replay.cfg`. That command re-obtains the initial state, about 1.46 GB of buckets referenced by `history-004cb73f.json`, and re-executes the 64 ledgers.
- **Not kept.** The buckets are not kept here, so the full replay cannot be repeated offline from these artifacts.
- **Recorded.** The original run's limits, consumption and command are in `replay-run.json`.
- **Not in CI.** It is not part of the tests or of CI.
