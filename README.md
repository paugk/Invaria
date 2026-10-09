# Invaria (core, initial)

Invaria is an operational-integrity and reproducible-evidence layer for tokenized operations, for Stellar. Its goal is to state, for a concrete operation such as a fund subscription, whether institutional records (orders, bank, transfer agent) and on-chain observations are coherent:
- `MATCH`: every mandatory check passes with sufficient evidence.
- `BREAK`: a contradiction is demonstrated.
- `UNKNOWN`: evidence, coverage or interpretation is insufficient.

Invaria keeps the evidence needed to explain that conclusion.

**This branch contains the implemented building blocks, offline verticals for a synthetic fund subscription and a synthetic redemption, and a demo subscription evaluated with real Stellar testnet evidence.** It is not the MVP and not production-ready. The Stellar adapter covers a declared subset, and the local console is a read-only demo.

**[CAPABILITIES.md](CAPABILITIES.md)** lists what each component can and cannot conclude, with its declared limits: absence of settlement, completeness of what was observed, `valid_at`, and historical replay.

## What is in this branch

| Component | Path | Status |
|---|---|---|
| Canonical contracts (typed, strict, immutable) | `src/invaria/contracts/` | Implemented and tested |
| JSON Schemas (2020-12) generated from the contracts | `schemas/` | Implemented; a test fails on drift |
| Deterministic CSV ingestion with versioned declarative mappings | `src/invaria/ingest/` | Implemented and tested |
| Pure evaluators (subscription and redemption), explanations and minimal export | `src/invaria/engine/` | Implemented; versioned engines (current, retired for replay only, blocked) |
| Offline CLI (`invaria`) | `src/invaria/cli.py` | Implemented |
| Directory evidence bundles: offline R1/R2 replay, detached Ed25519 signatures, hardened verifier | `src/invaria/bundle/` | Implemented; trust comes only from the verifier's own trust store |
| Synthetic corpora: a demo fund subscription (9 scenarios) and a redemption (46 scenarios), one directory per profile version | `tests/fixtures/corpus/` (index in its README) | Synthetic data; expected results written by hand, reproduced by the evaluators |
| Golden bundles for scenario K3 (one per subscription engine) and frozen redemption bundles | `tests/fixtures/bundles/` | Rebuilt byte for byte by their engines (retired ones through compatibility implementations) |
| Golden verifier corpus: 22 cases covering every verifier status | `tests/fixtures/bundles/corpus/` | Public keys and signatures only; no private key |
| Read-only Stellar adapter (Horizon + RPC) | `src/invaria/stellar/` | Payments and SAC movements; path payments, DEX fills, clawbacks, typed counterparties, muxed accounts and memos recorded explicitly (see CAPABILITIES.md) |
| Recorded real Stellar testnet samples | `tests/fixtures/stellar/` | Third-party USDC samples and Invaria's own DEMOA issuance, replayed offline |
| Ledger verification from history archives: anchored inclusion, review of a kept replay, bounded completeness of Classic payments, trustline reconciliation | `src/invaria/stellar/ledger_proof.py`, `src/invaria/stellar/ledger_completeness.py`, `src/invaria/stellar/trustline_reconciliation.py`, `tests/fixtures/stellar/ledger-archive/` | Implemented, tested offline on one testnet checkpoint; needs the pinned Stellar CLI. No effect on the engines (see [LEDGER_VERIFICATION.md](LEDGER_VERIFICATION.md)) |
| Requirement diagnosis and obligations views: the checks behind each stored control result, and the obligations the current profiles state | `src/invaria/query/diagnostics.py`, `src/invaria/query/obligations.py`, `src/invaria/engine/trace.py` | Implemented; offline CLI (`invaria diagnose`, `invaria obligations`) and query service; explanation only, no new result (see [QUERY_VIEWS.md](QUERY_VIEWS.md)) |
| Demo subscription evaluated with real testnet evidence | `src/invaria/vertical_testnet.py`, `tests/fixtures/corpus/subscription-testnet-1.6.0/` (current profile) | 4 scenarios; on-chain evidence replayed through the adapter |
| Append-only PostgreSQL persistence (optional `db` extra) | `src/invaria/persistence/` | Implemented; tests need a real PostgreSQL (opt-in) |
| Read-only consultative MCP server (optional `mcp` extra) | `src/invaria/query/`, `src/invaria/mcp_server.py` | Implemented; stdio only; tests need a real PostgreSQL (opt-in) |
| Invalidation, revision epochs and transactional outbox | `src/invaria/engine/dependencies.py`, `src/invaria/persistence/` | Implemented; tests need a real PostgreSQL (opt-in) |
| Logical backup and verified restore (optional `db` extra) | `src/invaria/persistence/backup.py` | Implemented; tests need PostgreSQL and the pinned container (opt-in) |
| Local read-only evidence console: REST and HTML, no JavaScript | `src/invaria/console/` | Implemented; listens on 127.0.0.1 only; tests need PostgreSQL (opt-in) |

### Contracts
- **Exact quantities.** Integer atoms as a string, plus an explicit scale (0–38) and unit. Floats, exponents, NaN and duplicate JSON keys are rejected before validation.
- **Instrument and Stellar Classic representation.** Identified by network, asset code and issuer, never by ticker alone. Includes StrKey checksum validation (standard library only, no SDK).
- **Observation with provenance.** Each observation carries the raw sha256, a locator, the parser and mapping versions, and source record + revision. A correction is a new revision with `supersedes`; nothing is edited in place.
- **Coverage certificate.** Interval, ledger range, level, gaps and quarantined records.
- **Operation profile.** Authority per fact type, required coverage, exact pricing with no tolerance, controls, result precedence and declared unsupported capabilities.
- **Closed snapshot reference.** Holds the economic time (`valid_at`), knowledge time (`known_at`) and an explicit evaluation clock.
- **Control and evaluation results.** Precedence over mandatory controls is BREAK > UNKNOWN > MATCH. A technical error is UNKNOWN, never PASS.
- **Evidence-bundle manifest, frozen evidence and verification report.** The manifest validates safe relative, sorted, unique, case-insensitively distinct paths and declares `replay` (`R1` or `R2`) and `export_format: directory_v1`. Signatures are detached (`signature.json`), so the manifest field `integrity.signature` stays `not_implemented`.
- **Bundle signatures and trust store.** Ed25519 signature records (key id, claimed `signed_at`, exact manifest sha256) and the verifier's trust store (keys, validity windows, purpose, revocation).
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

### Evidence bundles (R1, R2, signatures)
`invaria bundle` writes a directory with `manifest.json`, plus `snapshot.json`, `profile.json`, `evidence.json` (only snapshot members) and `evaluation.json`, all as canonical JSON with a SHA-256 per artifact. With `--r2` it also includes the raw CSV files and mappings the observations cite. `invaria verify` re-runs the locally installed engine on the frozen artifacts, without network, without writing and without executing anything from the bundle. Verifier statuses:

| Status | Meaning |
|---|---|
| `REPRODUCED` | The recorded evaluation is reproduced byte for byte (and, at R2, every observation is rebuilt from its raw bytes). The financial result (e.g. BREAK) is reported in a separate field. |
| `MISMATCH` | The artifacts are consistent but replay gives a different evaluation, or a raw row renormalises to different content. |
| `INCOMPLETE` | A required artifact is missing, the bundle names an engine or parser not installed locally, or an observation cannot be renormalised at R2. |
| `UNTRUSTED` | The manifest sha256 differs from an expected value received out of band, or the trust policy is not met. |
| `REJECTED` | Hash mismatch, undeclared files or directories, symlinks (also in parent directories), hard links, FIFOs, unsafe or colliding paths, duplicate JSON keys, floats or excessive nesting, invalid contracts, inconsistent membership or versions, or size limits exceeded. |

Integrity of every present artifact is judged before availability: a tampered file is `REJECTED` even when another one is missing. Hostile input yields a report, never an exception. Every file is opened relative to the bundle's root descriptor without following symlinks.

**R2.** Each CSV-derived observation is parsed again from the bundled raw bytes with its mapping and must match. Tenant and instrument come from the bundle's snapshot and profile, not from the observation. Each raw row backs one observation only. Observations without a local normaliser (on-chain evidence: the verifier never imports the Stellar adapter) make R2 `INCOMPLETE`, and the report lists what was renormalised. Coverage certificates and identity links are declarations, verified at R1 only.

**Signatures and trust.** `invaria sign <bundle> --key-file K.pem --key-id ID --signed-at T` adds a detached Ed25519 signature. The key file must be mode 600. Signing never changes the manifest, and the command refuses to sign a bundle that does not reproduce. The signed preimage is the domain `INVARIA-OEB-v1\0` plus the canonical signature record, which binds the key id, `signed_at` and the exact manifest sha256.

`invaria verify --trust-store S.json [--trust-at T] [--min-signatures N]` decides trust **only** from the trust store you supply; nothing inside the bundle can extend it. A signature counts only if all of these hold:
- the key is known and trusted for bundle signing;
- it covers this manifest and verifies cryptographically;
- the key is valid at the reference time (`signed_at` by default, or `--trust-at`);
- the key was not revoked before that time.

A `key_compromise` revocation voids all of a key's signatures, since `signed_at` is claimed by the signer. Without a trust store, a present signature is not verified and the report says so.

Without a signature or an expected manifest sha256, a hash proves integrity relative to the manifest, not authenticity: an internally consistent forgery verifies as `REPRODUCED`. A test demonstrates exactly this.

**Golden corpus.** `tests/fixtures/bundles/corpus/` holds 22 cases, each a base bundle plus an overlay and an expected outcome, covering all five statuses. Its signatures were made with ephemeral keys that were then discarded; only public keys are stored.

The report also carries the digest of the local replay code (`engine_source_sha256`).

### Stellar adapter (read-only)
`invaria stellar check-network` and `invaria stellar ingest` read Stellar testnet over HTTPS and turn asset movements into canonical `token_movement` observations. They never sign, fund or submit anything.
- **Network identity.** The Horizon and RPC network passphrases must both equal the expected network before any data is read.
- **Asset identity.** The asset is identified by network, code and issuer. The Stellar Asset Contract (SAC) id is derived locally. Horizon must publish the same `contract_id`, or none while the SAC is not deployed: testnet still emits unified events under the derived id.
- **Classic path (Horizon).** Reads an account's payments, including failed transactions, which are observed without economic effect. Provenance goes down to ledger, transaction, operation index and the sha256 of the exact raw page. Pagination is bounded; coverage certificates are `provider_claimed`.
- **SAC path (RPC `getEvents`).** Reads `transfer`, `mint` and `burn` events between accounts. Testnet emits unified events (CAP-67): a Classic payment also appears as a SAC event. Both paths share one economic-effect key (`<tx>:<op>:<ordinal>`), so the same effect counts once; differing content is kept as a visible conflict.
- **Effects that are not deliveries.** Path payments, DEX fills and clawbacks are recorded as `chain_effect`, never as `token_movement`. Contract, claimable-balance and liquidity-pool counterparties are typed by StrKey. Muxed ids and memos are kept exactly; a muxed address is attributed only through an explicit `IdentityLink` to it. Anything the adapter cannot resolve is quarantined with its reason. Custom Soroban tokens stay out of scope. Details and limits: [CAPABILITIES.md](CAPABILITIES.md).
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
`invaria demo-testnet tests/fixtures/corpus/subscription-testnet-1.6.0` evaluates the demo subscription `SUB-0001` with:
- **Synthetic institutional records:** order, bank and transfer agent.
- **Real on-chain evidence:** our own DEMOA issuance on Stellar testnet. The issuer `GCGGXYAKBIOKOIMVSUTXPEHRLJJU7VB7ZIYYKUFXYIVZTTFIWKEKTBEP` paid 1,000 DEMOA to the investor `GC6XNJNZOULFUZJBYORCTVNWI6UX5AVOEDC7EXDQM77BPFMQA3VWOYLK` (tx `87835238…`). The same issuance also includes a deliberately failed payment and an unlinked duplicate with the same memo.

The adapter replays the recorded responses; nothing is fetched.

| Scenario | Execution links | Result |
|---|---|---|
| TN-LINKED | the delivery, explicitly approved | MATCH, resting only on that delivery |
| TN-NO-LINK | none | UNKNOWN (`AMBIGUOUS_MATCH`): memo and amount never link |
| TN-OVER-LINKED | the delivery and, wrongly, the duplicate | BREAK, +1,000 shares |
| TN-LINKED-FAILED | only the failed transaction | UNKNOWN: a failed transaction has no effect |

Earlier demo corpora (`subscription-testnet`, `-1.1.0`, `-1.2.0`) admit an older payment mapping, so a new evaluation with today's adapter output refuses them (exit code 1); `-1.3.0` to `-1.5.0` are earlier profiles that still give 4/4. The demo profile accepts `provider_claimed` coverage for the on-chain source. Horizon and RPC are operated by the same provider, so this is a declared project decision, not independent verification. Institutional sources still require `internally_checked`. This is not a real fund.

### Ledger verification (offline review of testnet history)
`invaria stellar ledger-verify` checks the demo DEMOA transactions against a Stellar testnet history checkpoint (ledgers 5027648 to 5027711), without Horizon or RPC. The checkpoint's headers, transaction sets and result sets must hash and chain to an anchor: the checkpoint ledger's hash, recorded from a `stellar-core verify-checkpoints` run that observed the SDF testnet validators. It reports up to four separate results:
- **Inclusion** of each Classic operation, with its technical result. A failed transaction is included, but never a transfer.
- **Replay review.** A kept replay of the checkpoint is checked file by file against its record before decoding. Its events and trustline changes are then contrasted with the observations. Those events are derived from the replay: no header commits them.
- **Bounded completeness** of Classic `payment` operations of one asset and account in a declared ledger interval, comparing the anchored history with the provider's recorded capture and the adapter's records. It is not completeness of all movements.
- **Trustline reconciliation** of the approved addresses of an account in a declared time interval: each change of the trustline in the replay's meta is explained by an anchored operation or an asset event of the same location, account and asset. It is a report within that scope, dependent on the replay's provenance; it does not show that deliveries are complete.

It needs Stellar CLI 28.1.0 to decode XDR, and its tests fail rather than skip when `INVARIA_REQUIRE_STELLAR_CLI=1` and the CLI is missing. It changes no certificate, profile or coverage level: the engines keep `provider_claimed`. Step-by-step commands, expected results and trust limits, including a simulated omission that leaves the fixtures untouched: [LEDGER_VERIFICATION.md](LEDGER_VERIFICATION.md).

### Requirement diagnosis and obligations (read-only views)
`invaria diagnose CORPUS SCENARIO [--control ID]` shows, for each control of a stored evaluation, the checks the engine ran, the one that fixed its result, what it compared and cited, and the next step for what is undetermined. `invaria obligations CORPUS SCENARIO` groups the controls into the obligations the profile already states, through the explicit presentation catalogue `obligation-catalogue@1.0.0` (three exact profile versions). Both replay the evaluation once with its recorded engine and never change the stored result. Only the current engines are instrumented: historical ones give `unavailable_for_engine`. UNKNOWN never means unpaid, and not applicable never means fulfilled. Both also have `--json` and a query-service method. Details and expected outputs: [QUERY_VIEWS.md](QUERY_VIEWS.md).

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

Without `INVARIA_TEST_DATABASE_URL` these tests are skipped with an explicit reason. A migrator that is not a superuser (for example on a managed PostgreSQL) must run `invaria.persistence.provision.prepare_for_migrator` before `migrate`; the test fixtures do. Use a direct, session-preserving connection, not a transaction-mode pooler.

### Invalidation and idempotency (PostgreSQL, optional)
When evidence an evaluation depends on changes, that evaluation stops being current and is re-evaluated. Nothing is deleted.
- **Dependencies are predicates**, derived from the profile and the operation (`invaria.engine.dependencies`, pure). Absence is therefore invalidatable: an UNKNOWN for missing bank evidence goes stale when that evidence arrives. Evidence for another operation, another instrument or a non-authoritative source changes nothing.
- **Revision epochs:** each watched operation has an append-only `revision_epoch`. The change, the new epoch and an outbox event (`scope.invalidated`) commit in one transaction.
- **Compare-and-swap publication:** a worker publishes only if the epoch it read before building its snapshot is still current. Otherwise it gets `NOT_CURRENT`; the attempt and the evaluation are kept, and nothing computed on a superseded revision becomes current.
- **Currency projection:** the financial result never changes. Currency is a separate projection: `current`, `stale` (re-evaluation pending) or `superseded`.
- **Outbox:** at-least-once delivery with idempotent, per-consumer acknowledgements. A redelivered event yields the same evaluation (`ALREADY_PUBLISHED`).
- **Knowledge cuts:** building a snapshot "as known at" a time closes that cut. A new record claiming an earlier `recorded_at` is refused (`LateRecord`), so the same cut always rebuilds the same membership.
- **Concurrency:** writes, snapshot builds and publication serialize per tenant with an advisory lock. The tests use two real connections.
- **Append-only:** new tables are also append-only for the application role.

`invaria.persistence.worker` reads the epoch, builds the snapshot, evaluates, saves and publishes. Times are always explicit; there is no wall clock and no scheduler, leases or outbox retention yet.

### Consultative MCP server (read-only, optional)
`invaria mcp serve` exposes seven tools over stdio to a local MCP client such as an AI assistant. All are read-only: none writes, signs, sends, approves or reconciles anything.

| Tool | Answers |
|---|---|
| `trace_operation` | Current conclusion, legs (order, cash, units, token), history and revision epochs |
| `get_conclusion_as_known_at` | What was concluded for an economic time `valid_at` with the knowledge available at `known_at` |
| `get_missing_evidence` | What an UNKNOWN control lacks: authoritative source, mapping and minimum coverage |
| `explain_discrepancy` | Operands and exact delta of one control, replayed from the stored snapshot |
| `explain_conclusion_change` | Controls and effective evidence that changed between two evaluations |
| `get_evidence` | One observation or coverage certificate, labelled as untrusted source data |
| `get_coverage` | Coverage certificates applied to the conclusion and unmet requirements |

Every answer states its snapshot (`valid_at`, `known_at`), its currency (`current`, `stale`, `superseded` or `not_published`) and its coverage. Answers come only from stored snapshots and evaluations; no source is queried.

- **Access profile.** A JSON file chosen by the operator, outside the repository, sets the principal, the tenant and the scopes (`operations:read`, `evidence:read`). Every call is authorised on the server.
  - A missing scope gives `FORBIDDEN`.
  - Another tenant's data gives `NOT_FOUND`, so its existence is not revealed.
  - The tenant never comes from the client.
- **Read-only database role.** The server reads through `invaria_reader`, which has SELECT only (migration `0003`).
- **Bounded inputs.** Identifiers and UTC times only; no SQL, paths, URLs or free text. Lists are capped by the profile's `max_items`, with a `truncated` flag.
- **Access log.** An optional JSONL audit log records time, principal, tool, status and only a sha256 of the arguments.
- **Untrusted content.** The server instructions tell clients never to follow instructions found in source content.

```bash
uv sync --extra db --extra mcp
INVARIA_DATABASE_URL=postgresql://... uv run invaria mcp serve \
  --access-profile access.json --audit-log access.jsonl
```

With stdio, authentication is that of the operating-system user who starts the process. There is no network API, OAuth or token yet; the local evidence console serves REST and HTML on 127.0.0.1 only, with a per-process token. The typed answers are exported as JSON Schemas (`schemas/query-*.schema.json`, `schemas/access-profile.schema.json`).

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

Runtime dependencies: `pydantic`, `cryptography` (Ed25519 only). Optional: `psycopg` (`db` extra), `mcp` (`mcp` extra, the official MCP Python SDK). Development: `pytest`, `hypothesis`, `ruff`, `mypy`. Exact versions are pinned in `uv.lock`.

## Not implemented yet

- **Stellar:** independent verification of ledger checkpoints (on-chain coverage is `provider_claimed`); custom Soroban tokens; contracts as approved investor addresses; mainnet.
- **Evidence bundles:** compressed archives, R3 (source proofs), R2 for on-chain evidence, third-party timestamping of signatures, and Windows-specific path rules.
- **Infrastructure:** worker leases, scheduler and outbox retention; row-level security / multi-tenant; object storage for raw bytes; a network API or MCP transport with tokens; point-in-time recovery or high availability.
- **Other operations:** fees, partial fills, multiple payments, FX, omnibus accounts; business calendars for deadlines.
- **Other sources:** XLSX, SFTP, encodings other than UTF-8, locale-specific number formats.

## License

Copyright 2026 unapu.pau. Licensed under the [Apache License, Version 2.0](LICENSE); see also [NOTICE](NOTICE). Third-party dependencies are installed from PyPI and keep their own licenses.
