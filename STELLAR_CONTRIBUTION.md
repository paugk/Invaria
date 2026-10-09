# Invaria — What it offers the Stellar ecosystem

Status: describes the published branch `opensource/core-initial` at commit `92a9fa76d71a55f8b662a712ad0dd31f07fdc6d5`. Limits: [CAPABILITIES.md](CAPABILITIES.md).

**This is independent open-source software published in the Invaria repository.** It is not a contribution to Stellar Development Foundation repositories. No upstream contribution has been submitted to or accepted by Stellar maintainers, and no affiliation or endorsement is claimed.

## What the public code offers

- **A read-only evidence adapter.** Horizon and RPC readers that check the network passphrase and the asset identity (code, issuer and locally derived SAC contract id) before reading anything. They never sign, fund or submit.
- **Explicit semantics for the hard cases:**
  - Classic payments, including failed transactions, which never count as movements;
  - SAC `transfer`/`mint`/`burn` events, counted once with their Classic counterpart;
  - path payments, DEX fills, clawbacks, claimable balances, contract and liquidity-pool counterparties, muxed accounts and memos. Each is represented explicitly or quarantined with a reason; none counts as a delivery by inference.
- **Provenance and coverage.**
  - Every observation keeps its ledger, transaction, operation and the sha256 of the raw response.
  - Coverage certificates declare their chain scope and what their route cannot see.
  - Unavailable data is never reported as no activity.
- **Ledger verification from history archives.** Checks against an anchored testnet checkpoint, without Horizon or RPC, using the official `stellar-core` and Stellar CLI tools for consensus and XDR:
  - anchored inclusion of Classic operations;
  - contrast of a kept replay;
  - a bounded completeness report of Classic payments.
- **Operational controls on top of chain evidence.** A demo subscription shows how a delivery is accepted only through an explicit, approved execution link, and how missing or ambiguous evidence yields UNKNOWN rather than a guess.

## Reproducing the published examples

The commands and expected results live in the guides, so they are not repeated here:
- [README.md](README.md): installation from the lock file, the offline scenario checks, the testnet demo `invaria demo-testnet tests/fixtures/corpus/subscription-testnet-1.6.0`, and bundle verification.
- [LEDGER_VERIFICATION.md](LEDGER_VERIFICATION.md): inclusion, replay review, bounded completeness, and a simulated omission that leaves the fixtures untouched.
- The CI workflow (`.github/workflows/ci.yml`) runs the same checks on every push. Its `ledger-verification` job installs the pinned Stellar CLI and fails if it is missing or any of its tests is skipped.

Everything runs offline from recorded responses. Testnet resets periodically, so the opt-in live test may skip. The presence of code and fixtures is not evidence that every live-network scenario has passed.

## What the fixtures cover

Described in [tests/fixtures/stellar/README.md](tests/fixtures/stellar/README.md) and [tests/fixtures/stellar/ledger-archive/README.md](tests/fixtures/stellar/ledger-archive/README.md):
- **Real public testnet samples from third parties** (USDC and other test assets), recorded with their capture dates:
  - payments and SAC events;
  - path payments and DEX fills;
  - clawbacks as seen by the holder and by the issuer, and a failed clawback;
  - claimable balance creation and claim;
  - a contract counterparty;
  - a fee-bump payment;
  - a payment to a muxed account.
- **Invaria's own DEMOA issuance** on testnet: a trustline, a delivery, a deliberately failed payment, and an unlinked duplicate with the same memo.
- **History-archive evidence for DEMOA:**
  - checkpoint 5027711 (ledgers 5027648 to 5027711);
  - the recorded anchoring run;
  - a kept replay of that checkpoint, with its original record and a separate later revision.
- **Synthetic Stellar fixtures,** clearly labelled, for cases without a convenient real sample.

## Dependencies and trust limits

- **One provider.** On testnet the Horizon and RPC endpoints, the validators and the history archives are all operated by SDF. The archives are a different system from the provider's database, not a different operator.
- **Relative to an anchor.** Ledger verification depends on the anchor of the checkpoint ledger, recorded from a `stellar-core verify-checkpoints` run that observed the three SDF testnet validators. Reading that record imports the anchor; the verifier observes no consensus itself.
- **Replay events are derived.** Events come from a replay with a pinned `stellar-core` build, not from a ledger header.
  - A trusted hash of the run's record shows correspondence with that record, not that the run happened.
  - The hashes shipped with the fixtures travel through the same channel as the fixtures.
- **Pinned tools.**
  - Stellar CLI 28.1.0, with checksum and version checked in CI, decodes and re-encodes XDR exactly.
  - `stellar-core` 29.0.0, pinned by digest, is needed only to observe an anchor again or to repeat a replay.
- **The evaluators keep `provider_claimed` on-chain coverage.** Ledger verification does not change any evaluation.

## Reporting problems and proposing improvements

- **GitHub Issues** of [paugk/Invaria](https://github.com/paugk/Invaria/issues). It is the mechanism this repository has. Include:
  - the commit;
  - the exact command;
  - the output;
  - for Stellar data, the network, ledger and transaction hash.
- **Do not include secrets or private keys.** The repository publishes no security policy yet, so do not post sensitive details of a vulnerability in a public issue. Open an issue asking for a private contact instead.
- **Larger changes.** There is no contribution guide yet. Propose them in an issue before writing code.

## Publication and claims

- **Published content.** Only reviewed public coordinates and eligible recordings are published, never seeds, private keys, confidential records or private development history.
- **Separate tooling.** Demo transaction creation is kept apart from the read-only runtime.
- **Claims.** A contribution report identifies the exact commit, commands, results and known failures. It does not invent adoption metrics or describe a testnet token as an institutional asset.
- **Funding.** Any funding application's requirements are checked separately at submission time.
