# Invaria (core, initial)

Invaria is an operational-integrity and reproducible-evidence layer for tokenized operations, starting with Stellar. Its goal is to state, for a concrete operation such as a fund subscription, whether institutional records (orders, bank, transfer agent) and on-chain observations are coherent:
- `MATCH`: every mandatory check passes with sufficient evidence.
- `BREAK`: a contradiction is demonstrated.
- `UNKNOWN`: evidence, coverage or interpretation is insufficient.

Invaria keeps the evidence needed to explain that conclusion.

**This branch contains only the first, already implemented building blocks.** It is not the MVP. There is no evaluator yet: nothing in this repository computes MATCH/BREAK/UNKNOWN for an operation.

## What is in this branch

| Component | Path | Status |
|---|---|---|
| Canonical contracts (typed, strict, immutable) | `src/invaria/contracts/` | Implemented and tested |
| JSON Schemas (2020-12) generated from the contracts | `schemas/` | Implemented; a test fails on drift |
| Deterministic CSV ingestion with versioned declarative mappings | `src/invaria/ingest/` | Implemented and tested |
| Synthetic corpus: one demo fund subscription, 9 scenarios | `tests/fixtures/corpus/subscription-synthetic/` | Synthetic data; expected results are specifications |

### Contracts
- **Exact quantities.** Integer atoms as a string, plus an explicit scale (0–38) and unit. Floats, exponents, NaN and duplicate JSON keys are rejected before validation.
- **Instrument and Stellar Classic representation.** Identified by network, asset code and issuer, never by ticker alone. Includes StrKey checksum validation (standard library only, no SDK).
- **Observation with provenance.** Each observation carries the raw sha256, a locator, the parser and mapping versions, and source record + revision. A correction is a new revision with `supersedes`; nothing is edited in place.
- **Coverage certificate.** Interval, ledger range, level, gaps and quarantined records.
- **Operation profile.** Authority per fact type, required coverage, exact pricing with no tolerance, controls, result precedence and declared unsupported capabilities.
- **Closed snapshot reference.** Holds the economic time (`valid_at`), knowledge time (`known_at`) and an explicit evaluation clock.
- **Control and evaluation results.** Precedence over mandatory controls is BREAK > UNKNOWN > MATCH. A technical error is UNKNOWN, never PASS.
- **Initial evidence-bundle manifest.** Validates safe relative paths. Signing, export and replay are explicitly marked `not_implemented`.
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

After changing a contract, regenerate the schemas (a test fails if they drift):

```bash
uv run python -m invaria.contracts.schema_export
```

Runtime dependency: `pydantic`. Development: `pytest`, `hypothesis`, `ruff`, `mypy`. Exact versions are pinned in `uv.lock`.

## Not implemented yet

- **Evaluator:** computing control results and MATCH/BREAK/UNKNOWN from a snapshot. The scenario expectations in the corpus are specifications for it, not passing results.
- **Explanations** of conclusion changes.
- **Stellar ingestion:** no RPC/Horizon access. Stellar Asset Contract (SAC) events and custom Soroban tokens are declared unsupported. On-chain observations in the corpus are frozen synthetic fixtures.
- **Evidence bundles:** signing, safe archive export and offline replay.
- **Infrastructure:** persistence, API, MCP server, UI.
- **Other operations:** redemptions, fees, partial fills, multiple payments, FX, omnibus accounts.
- **Other sources:** XLSX, SFTP, encodings other than UTF-8, locale-specific number formats.

## License

Copyright 2026 unapu.pau. Licensed under the [Apache License, Version 2.0](LICENSE); see also [NOTICE](NOTICE). Third-party dependencies are installed from PyPI and keep their own licenses.
