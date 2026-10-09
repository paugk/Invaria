# Capabilities and limits

What this branch can and cannot conclude, and where its evidence stops. Everything here runs offline against synthetic data or recorded public Stellar **testnet** data. **Nothing here is production-ready, and it is not complete Stellar support.**

## Capability matrix

| Area | What it does | Status | Main limits |
|---|---|---|---|
| Contracts and schemas | Strict, immutable typed contracts with exact quantities. JSON Schemas are generated from them | Implemented, tested | — |
| CSV ingestion | Declarative, versioned mappings; quarantines files or rows; append-only journal | Implemented, tested | UTF-8 CSV only |
| Subscription evaluator | Order, cash, transfer-agent units and on-chain delivery compared exactly; MATCH / BREAK / UNKNOWN | Implemented, tested | Synthetic and testnet demo profiles only |
| Redemption evaluator | Price, position, cash, 48-hour deadline, register debit and burn; cancellation and reactivation | Implemented, tested | Synthetic profile only. Single synthetic transfer-agent journal; no business calendar |
| Engine versions | Current, retired (replay only) and blocked engines; a new evaluation always uses the current engine | Implemented, tested | See [Historical replay](#historical-replay) |
| Evidence bundles | R1 and R2 replay, detached Ed25519 signatures, hardened offline verifier | Implemented, tested | R2 covers CSV evidence only; no R3; directory format only |
| Stellar adapter (read-only) | Horizon payments and RPC SAC events with network, asset and SAC identity checks; recording and offline replay | Implemented for the cases below | Never signs, funds or submits. The coverage the engines use is `provider_claimed`; ledger verification (below) does not change it |
| Path payments, DEX fills, clawbacks | Recorded as `chain_effect`, never as delivery or retirement. Classic/SAC correspondence is checked; anything unresolved is quarantined | Implemented | A `chain_effect` never proves compliance. It only makes UNKNOWN what resolving it could change |
| Typed counterparties | Accounts, contracts, claimable balances and liquidity pools, by StrKey (G/C/B/L) | Implemented | Contracts cannot be approved as investor addresses (no `IdentityLink` for contracts) |
| Muxed accounts and memos | Muxed ids kept exactly as u64 text; memos kept exactly | Implemented | Decoding a muxed address never attributes it; only an explicit `IdentityLink` to that exact address does, under a profile that declares it. A memo never links anything |
| Mapping provenance | A new evaluation refuses evidence produced by a mapping its profile does not admit, and states the mappings of its evidence | Implemented | Older demo corpora are refused with today's adapter output |
| Ledger verification (history archives) | Anchored inclusion of Classic operations in a testnet checkpoint; offline review of a kept replay and contrast of its events; bounded completeness of Classic `payment` operations in a declared ledger interval. Guide: [LEDGER_VERIFICATION.md](LEDGER_VERIFICATION.md) | Implemented, tested offline on one testnet checkpoint | Relative to an anchor from the SDF testnet validators. Replay events are derived, not committed by any header. Completeness covers `payment` operations only. No effect on the engines, which keep `provider_claimed` |
| Persistence (PostgreSQL, optional) | Append-only store, closed snapshots, revision epochs, compare-and-swap publication, outbox | Implemented, tested on PostgreSQL 17.11 | Tests are opt-in. A migrator without superuser must run the documented provisioning step first |
| Logical backup and restore (optional) | `pg_dump`/`pg_restore` inside a pinned container; restores only to a new database and re-verifies every evaluation with its exact engine | Implemented, tested | Covers a logical backup of a local environment only: no PITR, high availability or managed service |
| Consultative MCP server (optional) | Seven read-only tools over stdio, scoped by an operator access profile | Implemented | stdio only; no network transport or tokens |
| Evidence console (optional) | Local read-only REST and HTML (no JavaScript), listening on 127.0.0.1 only | Implemented | Local demo: it assumes no hostile local processes; no accessibility audit with axe |

## Declared limits

### Absence of settlement (redemption, on-chain control)
This limit concerns one control: `no_settlement_after_cancellation` of the synthetic redemption, judged on the on-chain source with the chain certificates the current adapter produces. Showing that a cancelled redemption was **not** settled on-chain means showing that no retirement or settlement happened, not only that no admitted burn is visible. It is not a general statement about absences: other controls still conclude absences from the coverage their sources declare (for example, `redemption.payment_deadline` concludes that a payment was missed from bank coverage that extends past the deadline).

- **Current profile (`fund-redemption-synthetic@1.7.0`).** A chain certificate counts only if it declares its scope coherently with what its route and role can observe. No Horizon or SAC route that the adapter reads, for a holder or for the issuer, sees a third party's claim of a claimable balance. With these certificates the on-chain absence of settlement is therefore **never** shown: the control stays UNKNOWN (`INSUFFICIENT_COVERAGE`), and that is never evidence of compliance. A certificate from a route that does observe those claims would be needed; none exists here. Proven settlement despite a cancellation is still a BREAK.
- **Earlier profiles (1.3.0 to 1.6.0).** They keep their original rules. A certificate without a declared chain scope can still show that absence under 1.3.0 to 1.5.0; 1.6.0 requires a declared scope, without the coherence check.
- **Ledger verification** ([below](#ledger-verification)) does not change this: its completeness report covers Classic `payment` operations only, not retirements, settlements or claims.

### Completeness of what was observed
Observing a delivery or a burn needs no complete coverage. **Claiming that the observed set is complete does.**

- Under the current profiles (`fund-subscription-synthetic@1.2.0`, `fund-subscription-testnet@1.6.0`, `fund-redemption-synthetic@1.7.0`), an equal or short comparison of deliveries or burns is concluded only with a certificate that meets all of these:
  - its chain scope is coherent with its route and role;
  - it was read through the SAC route;
  - each representation of the instrument has its own certificate;
  - it reads every approved address on that representation's network.
- An excess stays a FAIL whatever the coverage, because unseen movements could only add to it.
- The synthetic corpora's chain certificates declare no scope, so under the current profiles many of their scenarios are UNKNOWN by design. The testnet demo's adapter certificates do qualify.

### `valid_at` and the evaluation clock
- A fact counts only if it is a snapshot member effective at both the evaluation clock and the economic cut: `valid_time <= min(clock, valid_at)`.
- Coverage intervals are half-open `[start, end)`. Covering an instant therefore requires `end` to be after it.
- **Burn completeness (redemption 1.7.0)** requires coverage of `[accepted_at, valid_at]`, `valid_at` included.
- **Absence of settlement** is judged on `[accepted_at, valid_at)`, so the instant `valid_at` is not covered there. This has no effect under 1.7.0, where that absence is never shown. Under the earlier profiles it is part of their rules: a settlement effective exactly at `valid_at` would not be seen by that check.

### Historical replay
- A retired engine is never used for new evaluations. It only reproduces conclusions recorded with it.
- Retired engines run as **compatibility implementations**: the current code with the later rules turned off, not the historical code. The verification report says so (`engine_status: retired`, `engine_implementation: compatibility`).
- The only historical source recovered is `invaria-engine@0.1.0`, from public commit `c736292`. It is stored in `tests/fixtures/engines/` and gives the same results on 13 recorded inputs; identity with that code is not claimed.
- **Reproducing a recorded conclusion does not validate it under the current rules, and does not update it.** Conclusions recorded under earlier profiles or engines keep their rules and limits.
- Blocked engines (`invaria-redemption-engine@0.1.0` to `0.3.0`) are never replayed and never substituted.

### Ledger verification
- **Scope.** Three separate results for the demo DEMOA issuance on testnet:
  - anchored inclusion of Classic operations in checkpoint 5027711 (ledgers 5027648 to 5027711);
  - an offline review of a kept replay of that checkpoint;
  - a completeness report of Classic `payment` operations of one asset and one account, as source or destination, in ledgers 5027650 to 5027672 (both included).
- **Anchor.** Every result is relative to the anchor of the checkpoint ledger. It was recorded from a `stellar-core verify-checkpoints` run that observed the three SDF testnet validators. A verification that reads that record imports the anchor; it observes no consensus itself. On testnet the same operator runs the validators, the archives and Horizon.
- **Inclusion is not completeness.** Including a failed transaction never shows a transfer.
- **Replay provenance.** Events and balance changes come from a replay of the anchored ledgers. No ledger header commits them.
  - The kept files are checked against the run's record before decoding.
  - Only an expected sha256 of that record, obtained through a trusted channel, makes them more than internally coherent. Even then it shows correspondence with the record, not that the run happened.
  - The buckets are not kept, so the full replay cannot be repeated offline.
- **Bounded completeness.** The absence it shows concerns those `payment` operations only. It does not cover the account's other movements of the asset (path payments, DEX fills, SAC, claimable balances, clawback), its balance, or other ledgers. A provider omission is declared only when the capture is shown complete for the interval and its filters include the operation.
- **No effect on evaluations.** No certificate, profile, coverage level or financial result changes. The engines keep `provider_claimed`, and the current profiles' completeness rules and the absence-of-settlement limit above stay as stated.

### Other limits
- **Coverage trust.** The on-chain coverage the engines use is `provider_claimed`: Horizon and RPC are run by the same provider. The ledger verification above checks one checkpoint against an anchor, but no certificate or profile uses it. The check that a certificate is coherent with its route is a check of the certificate, not of the ledger.
- **Approved addresses.** Investor addresses can only be Stellar accounts (G) or muxed accounts (M). Contracts cannot be approved.
- **Not supported:** fees, partial fills, multiple payments, FX, omnibus accounts, custom Soroban tokens, mainnet, R3 source proofs and compressed bundles.

## Engine and profile versions

| Kind | Current | Retired (replay only) | Blocked |
|---|---|---|---|
| Subscription engine | `invaria-engine@0.10.0` | `0.1.0` to `0.9.0` | — |
| Redemption engine | `invaria-redemption-engine@0.11.0` | `0.4.0` to `0.10.0` | `0.1.0` to `0.3.0` |

Each corpus directory states its profile. Earlier corpora are kept to reproduce their own results.

## Identifiers in recorded texts
A few recorded texts cite identifiers of the form `INV-nnn` or `ADR-nnn`: some assumptions the engines state in their conclusions, and the descriptions inside versioned profiles and scenario files. They name the project's internal design records, which are not published. Each sentence that cites one states the rule itself, so the reference is not needed to understand it. These texts are part of recorded, hash-pinned or reproducible outputs, so they are kept byte for byte.
