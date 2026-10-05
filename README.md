# Invaria (core, initial)

Invaria is an operational-integrity and reproducible-evidence layer for tokenized operations, starting with Stellar. Its goal is to state, for a concrete operation such as a fund subscription, whether institutional records (orders, bank, transfer agent) and on-chain observations are coherent:
- `MATCH`: every mandatory check passes with sufficient evidence.
- `BREAK`: a contradiction is demonstrated.
- `UNKNOWN`: evidence, coverage or interpretation is insufficient.

Invaria keeps the evidence needed to explain that conclusion.

**This branch contains the first implemented building blocks and an offline vertical for one synthetic fund subscription.** It is not the MVP: there is no signing, no API and no invalidation workflow, and the Stellar adapter only covers a first subset.

## What is in this branch

| Component | Path | Status |
|---|---|---|
| Canonical contracts (typed, strict, immutable) | `src/invaria/contracts/` | Implemented and tested |
| JSON Schemas (2020-12) generated from the contracts | `schemas/` | Implemented; a test fails on drift |
| Deterministic CSV ingestion with versioned declarative mappings | `src/invaria/ingest/` | Implemented and tested |
| Pure evaluator, explanations and minimal export | `src/invaria/engine/` | Implemented; reproduces all 9 corpus scenarios |
| Offline CLI (`invaria`) | `src/invaria/cli.py` | Implemented |
| Directory evidence bundles with offline R1 replay | `src/invaria/bundle/` | Implemented; unsigned |
| Synthetic corpus: one demo fund subscription, 9 scenarios | `tests/fixtures/corpus/subscription-synthetic/` | Synthetic data; expected results written before the evaluator, now reproduced |
| Golden bundle for scenario K3 | `tests/fixtures/bundles/K3/` | Must stay byte-identical to a fresh build |
| Read-only Stellar adapter (Horizon + RPC) | `src/invaria/stellar/` | Classic payments implemented; SAC subset implemented, other SAC cases explicitly excluded |
| Recorded real Stellar testnet samples | `tests/fixtures/stellar/` | Third-party USDC samples and Invaria's own DEMOA issuance, replayed offline |
| Demo subscription evaluated with real testnet evidence | `src/invaria/vertical_testnet.py`, `tests/fixtures/corpus/subscription-testnet/` | 4 scenarios; on-chain evidence replayed through the adapter |
| Append-only PostgreSQL persistence (optional `db` extra) | `src/invaria/persistence/` | Implemented; tests need a real PostgreSQL (opt-in) |

### Contracts
- **Exact quantities.** Integer atoms as a string, plus an explicit scale (0–38) and unit. Floats, exponents, NaN and duplicate JSON keys are rejected before validation.
- **Instrument and Stellar Classic representation.** Identified by network, asset code and issuer, never by ticker alone. Includes StrKey checksum validation (standard library only, no SDK).
- **Observation with provenance.** Each observation carries the raw sha256, a locator, the parser and mapping versions, and source record + revision. A correction is a new revision with `supersedes`; nothing is edited in place.
- **Coverage certificate.** Interval, ledger range, level, gaps and quarantined records.
- **Operation profile.** Authority per fact type, required coverage, exact pricing with no tolerance, controls, result precedence and declared unsupported capabilities.
- **Closed snapshot reference.** Holds the economic time (`valid_at`), knowledge time (`known_at`) and an explicit evaluation clock.
- **Control and evaluation results.** Precedence over mandatory controls is BREAK > UNKNOWN > MATCH. A technical error is UNKNOWN, never PASS.
- **Evidence-bundle manifest, frozen evidence and verification report.** The manifest validates safe relative, sorted, unique paths and declares `replay: R1` and `export_format: directory_v1`. Signing stays explicitly `not_implemented`.
- **CSV mapping.** Exact header, key, revision and time columns, status meanings, and quantity scale, unit and decimals. Declarative only: no expressions or code.

### CSV ingestion
`import_csv(raw_bytes, mapping, context, journal)`:
- **Parses deterministically.** No clock (the recorded time is passed in), no network, no float.
- **Quarantines a whole file** on schema drift (header name, order, extra column, BOM, delimiter), encoding errors, malformed CSV or size limits. A quarantined file yields no coverage.
- **Quarantines single rows** with a reason code: wrong scale or decimals, unit mismatch, negative amount, unknown status, non-UTC time, invalid identifier or revision. Values are never guessed or rounded.
- **Integrates into an append-only journal:**
  - `NEW` and `REVISION` (links `supersedes`) are appended;
  - `DUPLICATE` (idempotent re-import) is not appended;
  - `SOURCE_CONFLICT` (same key and revision, different content) is kept without choosing either;
  - `ORPHAN_RETRACTION` and `OUT_OF_ORDER` are rejected.
- **Produces a coverage certificate** with records received and quarantined.

### Evaluator
`evaluate(inputs)` is a pure function of a closed snapshot, an operation profile and the snapshot's member artifacts. It uses no clock, network or file access, and ignores anything outside the snapshot.
- **Precedence:** BREAK > UNKNOWN > MATCH over the mandatory controls. A BREAK keeps its UNKNOWN controls visible.
- **No guessing:** missing data is never treated as zero. Each undecided control is UNKNOWN with a reason: source conflict, withdrawn support, ambiguous identity, unsupported capability, quarantined input, missing evidence or insufficient coverage.
- **Exact comparisons:** in atoms, with the delta reported. Anything that would need rounding is UNKNOWN.
- **Technical errors are UNKNOWN, never PASS:** a snapshot member missing from the store, a member recorded after `known_at`, a control the engine does not implement, or an unexpected exception.
- **Explanations:** `explain_change` lists the controls and evidence that changed between two evaluations (e.g. K2 MATCH to K3 BREAK: the bank correction replaced the original confirmation). It does not claim a unique root cause.

### Evidence bundles (R1)
`invaria bundle` writes a directory with `manifest.json`, plus `snapshot.json`, `profile.json`, `evidence.json` (only snapshot members) and `evaluation.json`, all as canonical JSON with a SHA-256 per artifact. `invaria verify` re-runs the locally installed engine on the frozen artifacts, without network and without executing anything from the bundle. Verifier statuses:

| Status | Meaning |
|---|---|
| `REPRODUCED` | The recorded evaluation is reproduced byte for byte. The financial result (e.g. BREAK) is reported in a separate field. |
| `MISMATCH` | The artifacts are consistent but replay gives a different evaluation. |
| `INCOMPLETE` | A required artifact is missing, R2 was requested, or the bundle names an engine that is not installed locally. |
| `UNTRUSTED` | The manifest sha256 differs from an expected value received out of band. |
| `REJECTED` | Hash mismatch, undeclared files, symlinks, unsafe paths, duplicate JSON keys or floats, invalid contracts, inconsistent membership or versions, or size limits exceeded. |

Bundles are **not signed**. A hash proves integrity relative to the manifest, not authenticity: an internally consistent forgery verifies as `REPRODUCED` unless you pass `--expect-manifest-sha256` with a digest obtained through another channel. A test demonstrates exactly this.

### Stellar adapter (read-only)
`invaria stellar check-network` and `invaria stellar ingest` read Stellar testnet over HTTPS and turn asset movements into canonical `token_movement` observations. They never sign, fund or submit anything.
- **Network identity.** The Horizon and RPC network passphrases must both equal the expected network before any data is read.
- **Asset identity.** The asset is identified by network, code and issuer. The Stellar Asset Contract (SAC) id is derived locally. Horizon must publish the same `contract_id`, or none while the SAC is not deployed: testnet still emits unified events under the derived id.
- **Classic path (Horizon).** Reads an account's payments, including failed transactions, which are observed without economic effect. Provenance goes down to ledger, transaction, operation index and the sha256 of the exact raw page. Pagination is bounded; coverage certificates are `provider_claimed`.
- **SAC path (RPC `getEvents`).** Reads `transfer`, `mint` and `burn` events between accounts. Testnet emits unified events (CAP-67): a Classic payment also appears as a SAC event. Both paths share one economic-effect key (`<tx>:<op>:<ordinal>`), so the same effect counts once; differing content is kept as a visible conflict.
- **Explicit exclusions**, each with a reason:
  - path payments in the Classic path;
  - contract, claimable-balance or liquidity-pool counterparties;
  - u64 `to_muxed_id` values (a muxed destination or a memo id, indistinguishable from the event);
  - clawback;
  - custom Soroban tokens.
- **Checkpoints and availability.** Checkpoints advance only after the corresponding page is written and fsync'ed. A range outside Horizon history or RPC retention is reported as data unavailable (exit code 3), never as "no activity".
- **No implicit links.** An on-chain effect is linked to an operation only through an explicit, approved `ExecutionLink`. Memo text, amount or timing never link.

Endpoints come from `--horizon`/`--rpc`, then `INVARIA_STELLAR_HORIZON_URL`/`INVARIA_STELLAR_RPC_URL`, then the public testnet URLs. `--record DIR` stores every HTTP exchange; `--replay DIR` serves them offline.

```bash
uv run --locked invaria stellar check-network
uv run --locked invaria stellar ingest --target tests/fixtures/stellar/targets/usdc-issuer.json \
  --start-ledger 5015930 --end-ledger 5015945 --store /tmp/stellar-store --sac \
  --replay tests/fixtures/stellar/recordings/usdc-issuer
```

`tests/fixtures/stellar/` holds **real, public, third-party testnet data** (USDC, captured 2026-10-05; see its README). It is unrelated to the synthetic demo fund. The offline tests replay it with sockets blocked. An opt-in live test runs with `INVARIA_LIVE_TESTNET=1 uv run --locked pytest -m live_testnet tests/stellar`. Testnet is reset periodically, so after a reset that test skips.

### Demo subscription with real testnet evidence
`invaria demo-testnet tests/fixtures/corpus/subscription-testnet` evaluates the demo subscription `SUB-0001` with:
- **Synthetic institutional records:** order, bank and transfer agent.
- **Real on-chain evidence:** our own DEMOA issuance on Stellar testnet. The issuer `GCGGXYAKBIOKOIMVSUTXPEHRLJJU7VB7ZIYYKUFXYIVZTTFIWKEKTBEP` paid 1,000 DEMOA to the investor `GC6XNJNZOULFUZJBYORCTVNWI6UX5AVOEDC7EXDQM77BPFMQA3VWOYLK` (tx `87835238…`). The same issuance also includes a deliberately failed payment and an unlinked duplicate with the same memo.

The adapter replays the recorded responses; nothing is fetched.

| Scenario | Execution links | Result |
|---|---|---|
| TN-LINKED | the delivery, explicitly approved | MATCH, resting only on that delivery |
| TN-NO-LINK | none | UNKNOWN (`AMBIGUOUS_MATCH`): memo and amount never link |
| TN-OVER-LINKED | the delivery and, wrongly, the duplicate | BREAK, +1,000 shares |
| TN-LINKED-FAILED | only the failed transaction | UNKNOWN: a failed transaction has no effect |

The demo profile accepts `provider_claimed` coverage for the on-chain source. Horizon and RPC are operated by the same provider, so this is a declared project decision, not independent verification. Institutional sources still require `internally_checked`. This is not a real fund.

### Persistence (PostgreSQL, optional)
`invaria.persistence` stores observations, coverage certificates, identity links, profiles, closed snapshots and evaluations append-only. Install it with the `db` extra (`psycopg` 3).
- **Migrations:** SQL files whose sha256 is recorded. A migration changed after being applied is rejected.
- **Application role:** `invaria_app` has SELECT and INSERT only. UPDATE, DELETE and TRUNCATE of history fail inside the database.
- **Idempotent writes:** the same id with different content raises `ImmutableConflict`.
- **Snapshots:** persisted with explicit membership. A snapshot rebuilt "as known at" a time excludes later corrections, and a commit that lands later never changes an existing snapshot.
- **Same results:** evaluating from the database gives exactly the same result as from files.

Run the database tests against a local container:

```bash
docker run -d --name invaria-pg -e POSTGRES_PASSWORD=<local> -p 127.0.0.1:55432:5432 \
  postgres@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f
INVARIA_TEST_DATABASE_URL=postgresql://postgres:<local>@127.0.0.1:55432/postgres \
  uv run --locked pytest -m postgres tests/persistence
```

Without `INVARIA_TEST_DATABASE_URL` these tests are skipped with an explicit reason.

## Synthetic data only

Everything under `tests/fixtures/` except `tests/fixtures/stellar/` (real testnet data) is synthetic. The testnet demo corpus mixes synthetic institutional files with that real on-chain evidence. The synthetic part:
- The demo fund `DEMO-A` and all institutional files are invented.
- Stellar accounts are valid StrKeys derived from `sha256("invaria:synthetic:account:...")`.
- Transaction hashes are `sha256("invaria:synthetic:tx:<label>")`.
- Ledger numbers are made up.

No value corresponds to a real institution, person or testnet/mainnet transaction. The profile's choices (exact match, no fees, no rounding, which source is authoritative) are fixture decisions, not policies of any real fund. The corpus has its own README (in Spanish) describing each scenario and why its expected result is what it is.

A `raw_sha256` proves byte integrity of what was received. It does not prove the content is true or complete.

## Install and test

Requires [uv](https://docs.astral.sh/uv/). Python 3.13 is selected through `.python-version` and installed by uv if missing.

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest -q
```

Run the offline CLI against the synthetic corpus:

```bash
uv run --locked invaria check tests/fixtures/corpus/subscription-synthetic        # 9/9 scenarios
uv run --locked invaria demo tests/fixtures/corpus/subscription-synthetic         # raw CSV -> K1/K2/K3
uv run --locked invaria explain-change tests/fixtures/corpus/subscription-synthetic K2 K3
uv run --locked invaria bundle tests/fixtures/corpus/subscription-synthetic K2 /tmp/k2-bundle
uv run --locked invaria verify /tmp/k2-bundle
```

In `demo`, institutional evidence is imported from the raw CSV files. The on-chain movement and its coverage are frozen synthetic fixtures, and the output says so: the demo issuer does not exist on any network.

After changing a contract, regenerate the schemas (a test fails if they drift):

```bash
uv run python -m invaria.contracts.schema_export
```

Runtime dependency: `pydantic`. Development: `pytest`, `hypothesis`, `ruff`, `mypy`. Exact versions are pinned in `uv.lock`.

## Not implemented yet

- **Deadline rules and currency:** no control uses the evaluation clock yet, and there is no current/stale/superseded projection.
- **Stellar beyond the first subset:** path payments, SAC contract and pool counterparties, muxed attribution, clawback, custom Soroban tokens, and independent verification of ledger checkpoints (on-chain coverage is `provider_claimed`).
- **Evidence bundles:** signatures and trust policy, R2 replay from raw bytes, and compressed archives.
- **Infrastructure:** invalidation and epochs, row-level security / multi-tenant, object storage for raw bytes, API, MCP server, UI.
- **Other operations:** redemptions, fees, partial fills, multiple payments, FX, omnibus accounts.
- **Other sources:** XLSX, SFTP, encodings other than UTF-8, locale-specific number formats.

## License

Copyright 2026 unapu.pau. Licensed under the [Apache License, Version 2.0](LICENSE); see also [NOTICE](NOTICE). Third-party dependencies are installed from PyPI and keep their own licenses.
