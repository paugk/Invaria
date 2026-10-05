# Invaria (core, initial)

Invaria is an operational-integrity and reproducible-evidence layer for tokenized operations, starting with Stellar. Its goal is to state, for a concrete operation such as a fund subscription, whether institutional records (orders, bank, transfer agent) and on-chain observations are coherent:
- `MATCH`: every mandatory check passes with sufficient evidence.
- `BREAK`: a contradiction is demonstrated.
- `UNKNOWN`: evidence, coverage or interpretation is insufficient.

Invaria keeps the evidence needed to explain that conclusion.

**This branch contains the first implemented building blocks and an offline vertical for one synthetic fund subscription.** It is not the MVP: there is no live chain ingestion, no signing, no persistence and no API.

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

## Synthetic data only

Everything under `tests/fixtures/` is synthetic:
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

In `demo`, institutional evidence is imported from the raw CSV files. The on-chain movement and its coverage are frozen synthetic fixtures, and the output says so, because Stellar ingestion is not implemented.

After changing a contract, regenerate the schemas (a test fails if they drift):

```bash
uv run python -m invaria.contracts.schema_export
```

Runtime dependency: `pydantic`. Development: `pytest`, `hypothesis`, `ruff`, `mypy`. Exact versions are pinned in `uv.lock`.

## Not implemented yet

- **Deadline rules and currency:** no control uses the evaluation clock yet, and there is no current/stale/superseded projection.
- **Stellar ingestion:** no RPC/Horizon access. Stellar Asset Contract (SAC) events and custom Soroban tokens are declared unsupported. On-chain observations in the corpus are frozen synthetic fixtures.
- **Evidence bundles:** signatures and trust policy, R2 replay from raw bytes, and compressed archives.
- **Infrastructure:** persistence, API, MCP server, UI.
- **Other operations:** redemptions, fees, partial fills, multiple payments, FX, omnibus accounts.
- **Other sources:** XLSX, SFTP, encodings other than UTF-8, locale-specific number formats.

## License

Copyright 2026 unapu.pau. Licensed under the [Apache License, Version 2.0](LICENSE); see also [NOTICE](NOTICE). Third-party dependencies are installed from PyPI and keep their own licenses.
