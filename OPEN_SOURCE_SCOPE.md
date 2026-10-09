# Invaria — Open source scope

Status: proposed boundary, effective only after maintainer adoption. It describes the branch `opensource/core-initial` at commit `92a9fa76d71a55f8b662a712ad0dd31f07fdc6d5`. Each component in that commit was approved by the maintainer for release. What each one can and cannot conclude is in [CAPABILITIES.md](CAPABILITIES.md), which governs where this document is shorter. Planned features are never represented as shipped.

## Purpose

Invaria's open core lets anyone check locally, without a paid service, whether the records of a tokenized operation and its on-chain observations support a conclusion, starting with Stellar. A developer can:
- ingest supported public chain data;
- import generic sample records;
- evaluate an explicit reference profile;
- inspect the evidence behind each conclusion;
- reproduce public evidence bundles.

Public reproducibility does not depend on private rules, a paid API, proprietary credentials or an unavailable commercial engine. Public schemas and generic adapters stay independent of private packages.

## What is published and what each part can show

| Component | What it can show | Nature |
|---|---|---|
| Canonical contracts and JSON Schemas | Exact quantities, identities, provenance and coverage, validated strictly | Implemented, tested |
| CSV ingestion | Deterministic import with versioned mappings; files and rows are quarantined with a reason, never guessed | Implemented, tested |
| Subscription and redemption evaluators | MATCH, BREAK or UNKNOWN per control, with exact deltas and the evidence cited; missing or ambiguous evidence gives UNKNOWN with its reason | Implemented. **Synthetic** profiles and corpora, plus one **testnet demo** profile |
| Engine versions | New evaluations use the current engine; retired engines only reproduce recorded conclusions, through compatibility implementations | Implemented |
| Evidence bundles and verifier | Whether a recorded evaluation is **reproduced** offline (R1/R2), with Ed25519 signatures judged only by the verifier's own trust store | Implemented. R2 covers CSV evidence only |
| Read-only Stellar adapter | Horizon payments and SAC events with network, asset and SAC identity checks. Path payments, DEX fills, clawbacks, typed counterparties, muxed accounts and memos recorded explicitly or quarantined | Implemented for a declared subset; testnet only |
| Ledger verification from history archives | **Anchored inclusion**, **replay contrast** and **bounded completeness** (below) for the demo DEMOA issuance | Implemented, tested offline on one testnet checkpoint |
| Persistence, backup and restore (optional) | Append-only PostgreSQL storage, closed snapshots, revision epochs; logical backup restored only to a new database and re-verified | Implemented; local environment only |
| Read-only MCP server and local evidence console (optional) | Consultation of stored conclusions, scoped by an operator access profile | Implemented; stdio and 127.0.0.1 only |

**Kinds of verification.** These are four different statements, and none implies the others:
- **Reproduction** (`invaria verify`): a recorded evaluation gives the same result again from its frozen artifacts. It says nothing about whether the operation complied: a reproduced evaluation can be a BREAK.
- **Anchored inclusion** (`invaria stellar ledger-verify`): a Classic operation is in a testnet checkpoint whose headers, transaction sets and result sets hash and chain to an anchor. That anchor comes from a recorded run that observed the SDF testnet validators. An included failed transaction never shows a transfer.
- **Replay contrast:** the kept meta of a replay of that checkpoint reproduces the anchored headers, transactions and results. Its events and trustline changes are then compared with the observations. The events are derived from the replay; no header commits them.
- **Bounded completeness:** in a declared ledger interval, the anchored history, the provider's recorded capture and the adapter's records hold the same Classic `payment` operations of one asset with one account as source or destination. It is not completeness of the account's movements or balance.

**Coverage the engines use.** The evaluators keep `provider_claimed` on-chain coverage. Ledger verification produces reports; it changes no certificate, profile, coverage level or financial result.

**Limits that stay in force** (detailed in [CAPABILITIES.md](CAPABILITIES.md)):
- **Absence of settlement.** The on-chain absence of settlement of a cancelled redemption is never shown with the current certificates.
- **Completeness of what was observed.** It needs coherent, scoped coverage of every representation.
- **Testnet only.** Mainnet, fees, partial fills, FX, omnibus accounts and custom Soroban tokens are not supported.
- **Contract addresses.** They cannot be approved as investor addresses.

"Bounded" describes the functional scope and verified limits of a component, not a restriction on its use.

## Synthetic and experimental parts

- **Institutional records.** Orders, bank statements and transfer-agent registers are synthetic everywhere. There is no institutional pilot.
- **Testnet demo.** The demo subscription uses real testnet data from Invaria's own DEMOA issuance, which is not a real fund. Its profile accepts `provider_claimed` on-chain coverage by a declared project decision.
- **Optional services.** The console, the MCP server and persistence are local demonstrations, not hardened services.
- **Nothing is production-ready.**

## Proposed or outside this repository

**Proposed, not implemented:**
- bounded, ledger-verified guarantees that a new profile could request explicitly. They would be tied to their scope, anchor and method, never a global coverage level;
- contract addresses as approved investor addresses, which needs an explicit identity decision first;
- an institutional pilot, only with authorized data;
- a research interoperability format with a permitted public corpus (candidate only; no mining implementations included).

**Outside this repository** unless separately approved for release:
- managed operation, customer onboarding and customer-specific profiles;
- proprietary connectors and mappings;
- private tooling and internal development material.

Customer data and secrets are never eligible. Basic access control for any shipped interface is never withheld.

## Changes to scope

- **Classification and approval.** A new module or capability needs explicit classification and maintainer approval before export. An existing public path does not approve new contents.
- **By responsibility, not by name.** Interfaces such as an API, persistence, UI or MCP are assessed by what they do; their technology names do not decide visibility.
- **Release selection.** Releases use a reviewed selection of files and commits; no private history is merged.
- **No inference.** Completing a task or passing tests does not grant permission to publish.

## License and status

The repository's LICENSE (Apache-2.0) and NOTICE govern the published material; this document adds no restriction. The project does not claim endorsement by the Stellar Development Foundation, acceptance by a funding program, or production certification.
