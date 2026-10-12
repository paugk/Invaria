# Local console, API, events and controlled disclosure

This guide covers what a user can do today with the **local** console, its REST API (`/v1`), the CLI and the MCP server: integrity analytics, registered reports, the event feed by cursor, and controlled disclosure through access profiles and policies. Everything here runs on one machine against PostgreSQL. **It is not an institutional or hosted service, and it is not production-ready.**

## 1. Access profiles: identity, scopes and disclosure policies

Every process (`invaria console serve`, `invaria mcp serve` and the CLI commands that read the store) runs with **one access profile**: a JSON file the operator chooses, outside the repository.

```json
{
  "schema_version": "1.0",
  "principal_id": "operator-1",
  "tenant_id": "tenant-synthetic-demo",
  "scopes": ["operations:read", "evidence:read", "reports:read", "reports:register", "events:read"],
  "max_items": 50,
  "disclosure_policies": [
    "full-operations@1", "full-evidence@1", "full-report-index@1", "full-report-summary@1"
  ]
}
```

- **Identity and tenant** come only from this file. A client can never choose them: unknown query parameters are refused, and headers are ignored.
- **Scopes** say what the principal may read.
- **Disclosure policies** say what the principal may **receive**. Both apply cumulatively; a policy never grants a scope. **There is no implicit policy:** a profile without a policy for an output gets `403 DISCLOSURE_POLICY_UNAVAILABLE` for that output, before anything is read, so the existence of the resource is not confirmed either.
- **At startup**, `console serve` and `mcp serve` print to the operator's stderr which outputs this profile will have refused, naming scopes and policies only. The client never sees these warnings.
- **The profile is read when the process starts.** To remove a policy or a scope, edit the profile and **restart** the process. Until then, the running process keeps serving: there is no dynamic revocation.

### Policy catalogue (`disclosure-catalogue@1.1.0`)

| Policy | What it delivers | Needed for |
|---|---|---|
| `full-operations@1` | Operation data: conclusions, controls, evaluations, coverage, comparisons, diagnoses, obligations, computed integrity reports and their cases | `/v1/operations…`, `/v1/evaluations…`, `/v1/evaluation-comparisons`; the matching console pages; six MCP tools; `invaria analytics report\|cases\|reproduce`; events `operation.*` |
| `full-evidence@1` | Observations and coverage certificates (untrusted source content) | `/v1/evidence/{id}`, `/evidence/{id}`, MCP `get_evidence` |
| `full-report-index@1` | The list of registered reports (ids, hashes, horizons, times) | `/v1/integrity-reports`, `/reports` |
| `full-report-summary@1` | The summary of one registered report | `/v1/integrity-reports/{id}`, `/reports/{id}`; issuing a restricted view; events `integrity_report.registered` |
| `full-report-summary@1` **and** `full-operations@1` | The pinned cases behind a figure | `/v1/integrity-reports/{id}/cases`, `/reports/{id}/cases` |
| `aggregate-outcome-counts@1` | A **restricted** view of a report summary (section 5) | `/v1/disclosures…`, `/disclosures…` |

A policy is immutable: if what it delivers changes, a new version is created. A **full** policy delivers the existing contract unchanged: the responses are byte-identical to what an authorized principal got before policies existed. A test pins the field paths of every contract a full policy delivers, so a new field needs an explicit review. That check is structural: it does not detect a change of meaning, a more revealing value in the same field, a wider enum or new keys of a map. Those need a review of the policy whenever a contract changes.

### Upgrading from an earlier version

**Access profiles.** Earlier versions had no disclosure policies. A profile written for them keeps its scopes, but every output below is refused until its policies are added. Add exactly the policies of the outputs the principal should keep receiving:

| Use case (REST, console, MCP or CLI) | Scopes | Policies to add |
|---|---|---|
| Operations list, trace, timeline, conclusion as known, coverage; evaluation, discrepancy, missing evidence, comparison of two evaluations (REST, console pages, six MCP tools) | `operations:read` | `full-operations@1` |
| Evidence: one observation or certificate (REST, console page, MCP `get_evidence`) | `evidence:read` | `full-evidence@1` |
| Requirement diagnosis and obligations through the query service | `operations:read` | `full-operations@1` |
| `invaria analytics report` (stdout or `--json`), `analytics cases`, `analytics reproduce` | `operations:read` | `full-operations@1` |
| `invaria analytics report --out FILE` (a controlled export, section 3) | `operations:read` **and** `exports:create` | `full-operations@1` **and** `export-integrity-report-file@1` |
| `invaria analytics report --register` | `reports:register` (and `operations:read` to compute the report) | `full-operations@1` |
| List of registered reports (`/v1/integrity-reports`, `/reports`) | `reports:read` | `full-report-index@1` |
| Summary of a registered report (`/v1/integrity-reports/{id}`, `/reports/{id}`) | `reports:read` | `full-report-summary@1` |
| Pinned cases of a registered report (`…/{id}/cases`) | `reports:read` and `operations:read` | `full-report-summary@1` **and** `full-operations@1` |
| Event feed: events `operation.scope_reopened` and `operation.evaluation_published` | `events:read` and `operations:read` | `full-operations@1` |
| Event feed: events `integrity_report.registered` | `events:read` and `reports:read` | `full-report-summary@1` |
| Issue a restricted view (`invaria disclosure issue`) | `disclosures:issue` and `reports:read` | `full-report-summary@1` |
| Revoke a restricted view (`invaria disclosure revoke`) | `disclosures:revoke` | none (it delivers no report content) |
| Read restricted views issued to the principal (`/v1/disclosures…`, `/disclosures…`) | `disclosures:read` | `aggregate-outcome-counts@1` |

An operator who should keep everything they had needs the four full policies (`full-operations@1`, `full-evidence@1`, `full-report-index@1`, `full-report-summary@1`) plus the scopes of the new features they use. The startup warnings list what a profile is still missing. A policy assigned without its scope grants nothing.

**Database.** `migrate` applies the new migrations to an existing database: `0004` to `0007` (the registry of reports, domain events, the feed generation and the disclosure issuances) and `0008` (the export record and the role `invaria_exporter`). It changes no existing row; only `schema_migrations` gains their entries. A server whose migrator is not a superuser must run the provisioning step first, which now also creates `invaria_exporter` (NOLOGIN) and makes the migrator able to `SET ROLE` to it. To use `analytics report --out`, the login in `INVARIA_DATABASE_URL` must be a member of `invaria_exporter`. After the upgrade, `invaria events derive` turns the outbox rows written by the earlier version into events marked `historical`, since those rows carry no pinned publication references.

## 2. Console and REST API

```bash
uv sync --extra db
INVARIA_DATABASE_URL=postgresql://... uv run --locked invaria console serve \
  --access-profile access.json [--port 8765] [--audit-log access.jsonl]
```

The console listens on **127.0.0.1 only**, prints a sign-in URL with a per-process token, and accepts only `GET` and `HEAD`. Its HTML uses no JavaScript, and every response carries `Cache-Control: no-store`. The REST API is described in `docs/api/console-v1.openapi.json` (OpenAPI 3.1), including the scope and disclosure requirement of each operation (`x-invaria-scopes`, `x-invaria-disclosure`).

| Route | Answers | Scope |
|---|---|---|
| `GET /v1/operations[?result=]` | Operations of the tenant with their published result | `operations:read` |
| `GET /v1/operations/{op}`, `…/timeline`, `…/coverage`, `…/conclusion?valid_at&known_at` | Trace, publication history, coverage, and the conclusion as known at a time | `operations:read` |
| `GET /v1/evaluations/{id}`, `…/controls/{c}/discrepancy`, `…/controls/{c}/missing-evidence` | One evaluation, a control's operands and delta, what an UNKNOWN control lacks | `operations:read` |
| `GET /v1/evaluation-comparisons?left&right` | Controls and evidence that changed between two evaluations | `operations:read` |
| `GET /v1/evidence/{id}` | One observation or coverage certificate | `evidence:read` |
| `GET /v1/integrity-reports[?cursor&limit]` | Registered integrity reports, newest first | `reports:read` |
| `GET /v1/integrity-reports/{id}` | Summary of a registered report: figures with numerator and denominator, versions and times, no cases | `reports:read` |
| `GET /v1/integrity-reports/{id}/cases?key&cursor&limit` | The pinned operations behind one figure | `reports:read` and `operations:read` |
| `GET /v1/events?start=beginning\|now` or `?cursor=` | One page of the event feed (section 4) | `events:read`, plus each type's scope |
| `GET /v1/disclosures`, `GET /v1/disclosures/{view_id}` | Restricted views issued to this principal (section 5) | `disclosures:read` |

Errors are structured (`{"error": {"code", "message", "retryable", "request_id"}}`):
- `404 NOT_FOUND` is the same for an absent resource and for another tenant's;
- `403 FORBIDDEN` means a missing scope;
- `403 DISCLOSURE_POLICY_UNAVAILABLE` means a missing policy;
- `409`, `500` and `501` cover a registered report or event that is inconsistent with the store, corrupt or of a format this code cannot read.

**Console pages.**
- **Operations:** `/`, operation, coverage, evaluation, control, comparison and evidence pages.
- **Integrity reports:** `/reports`, `/reports/{id}` and `/reports/{id}/cases`.
- **Events:** `/events`.
- **Disclosed views:** `/disclosures`.

Each page presents what the same use case returns to `/v1`; the console computes no figure of its own. The top navigation lists only the destinations available to the profile. An internal link appears only when its destination is available; otherwise its identifier, already authorized on the page, is shown as plain text.

**Reading the analytics pages.**
- Every figure shows numerator and denominator; a zero denominator reads "not calculable", never 0 %.
- The financial result, the technical and publication state, and the diagnosed blocks are separate sections.
- The cases of a figure are pinned to the report. The operation and evaluation pages show the current state, and say so.

## 3. Integrity analytics and registered reports (CLI)

```bash
INVARIA_DATABASE_URL=postgresql://... uv run --locked invaria analytics report \
  --access-profile access.json --evidence-horizon 2026-10-20T00:00:00Z [--profile REF] \
  [--max-replays N] [--out report.json] [--json] [--register]
uv run --locked invaria analytics cases --access-profile access.json --report report.json --key result:UNKNOWN
uv run --locked invaria analytics reproduce --access-profile access.json --report report.json
```

- **Population:** the operations registered and accessible to the profile, each counted once.
- **What a report gives:**
  - the distribution of MATCH, BREAK and UNKNOWN among admitted conclusions, with the operations outside them counted apart;
  - discrepancies by control, with their age;
  - diagnosed blocks;
  - every figure with numerator, denominator and a key to its cases.
- **Admitted conclusions:** only the current publication of each scope, from a current engine, whose snapshot is within the evidence horizon.
- **Time:** the publication state read is the present one. The evidence horizon is compared with snapshot `known_at` only; it does not reconstruct what was published at that time.
- **`--register`** (scope `reports:register`) stores the exact bytes of the report this service just computed, append-only, under the id `ir-<report_sha256>`. It never accepts a file or a hash from the caller.
- **`report_sha256` checks internal consistency only:** anyone can edit a report and recompute it. It does not authenticate the issuer or show that the population covers all real activity.
- **stdout and `--json`** are the output of the query, under its own permissions. Redirecting them with the shell saves a copy that the application does not record: an authorized output is not a way to stop the user from keeping one.

### Controlled export of a computed report (`--out`)

`--out FILE` is an export: creating a file with the report is a decision of its own, on top of the query.
- **Permissions:** the scope `exports:create` and the policy `export-integrity-report-file@1` (catalogue `disclosure-catalogue@1.2.0`, a full policy that is never issued as a restricted view), **plus** `operations:read` and `full-operations@1`. All of them are checked before the destination is opened or anything is read; a refusal leaves no file and no record.
- **Database role:** the command runs as `invaria_exporter`, which reads what a report computation reads and can only append to the export record. It can neither write business data nor change or delete the record.
- **Content:** exactly the bytes `--out` wrote before (the report as indented JSON), with no envelope or export metadata. `analytics cases` and `analytics reproduce` read the file as before.
- **Record:** an attempt row (export id, tenant, principal, policies applied, `report_sha256`, the content's sha256 and size, store time) and, separately, at most one outcome row: `completed` or `failed` with a reason. No path, file name, host, recipient or content is stored. Nothing is updated or deleted.
  - `completed` means the service published the final name in the directory it opened and validated, after syncing the file, removing its temporary and syncing the directory. It does not mean that the textual path still leads to that directory, nor that anyone received the file.
  - An attempt without an outcome (a crash, or an error recording it) stays so; it is never turned into a failure, because the file may or may not exist. The reader role can list such attempts.
  - There is no atomicity between the database and the file system, and no recovery service.
- **Writing:** as every file the commands write (see the README, "Files written by the commands"): a new file only, mode 0600, no symbolic link followed, published without replacement.
- **Exit codes:** 0 completed; 2 refused (permissions or destination) or failed (nothing left under that name); 1 when the file may have been published but its completion is not confirmed (the message gives the export id; check the destination before retrying); 130 interrupted before publishing.
- **`--out` cannot be combined with `--register` in this version.** The command refuses the combination before connecting. It is a limit of the current design (each is its own use case, with its own database role and its own computation of the report), not a property of the data; run them separately.
- **What it does not do:** it governs the files this application creates. It does not stop copies of a query's output, it does not authenticate any recipient (none is asked for), and revoking the policy affects later exports only (after restarting), never files already delivered.

## 4. Domain events and the feed by cursor

```bash
INVARIA_DATABASE_URL=postgresql://... uv run --locked invaria events derive [--limit N] [--json]
```

`events derive` is a local, resumable command; there is no daemon. It turns the outbox into persisted domain events:
- `operation.scope_reopened`;
- `operation.evaluation_published`, with what changed against the previous publication;
- `integrity_report.registered`.

**Event properties:**
- Each event has a deterministic id and canonical bytes, and is never rewritten.
- `tenant_seq` is the confirmed derivation order within the tenant. It is not economic order and not the order of the facts: causality is in `epoch` and in the explicit publication references.
- Events written before publication references were recorded are marked `historical`.

**Reading the feed.**
1. Start explicitly: `GET /v1/events?start=beginning` (the whole retained feed) or `?start=now` (only what comes next).
2. Each page returns `items`, `next_cursor`, `examined_after`, `continuation_position`, `stop_reason`, `caught_up` and `notices`.
3. Store `next_cursor` **after** processing the page, and continue with `GET /v1/events?cursor=…`. Delivery is at least once: deduplicate by `event_id`.

**Semantics:**
- **`caught_up: true`** means "reached the last observed event", not "end of the feed". New events can arrive later.
- **Limits never skip an authorized event.** `item_limit` and `byte_limit` stop before the next authorized event. An event larger than `max_bytes` gives `422 EVENT_EXCEEDS_MAX_BYTES` with the required size, which you can retry with a larger `max_bytes`.
- **Type filter:** `types=a,b` is normalized and bound to the cursor.
- **Readable types** are computed on every request from the profile's current scopes **and** policies, never from the cursor. If they change between pages, the page carries a notice:
  - `readable_types_narrowed` when types stop being readable;
  - `readable_types_widened` when they become readable. Events of those types up to `examined_without_delivery_through` were examined without delivery; to read them, start again from the beginning with `types` set to them.
- **Volume is visible.** With `events:read`, positions and volume (`observed_head`) are visible even for types the profile cannot read. This is accepted for the local API.
- **Restoring a backup** with `invaria db restore` renews the feed generation. A cursor from before the restore gives `409 CURSOR_RESET`, to be resolved by starting again explicitly. A cursor ahead of the feed gives `409 CURSOR_BEYOND_FEED`. A manual restore without the tool is not detected.
- **An event authorizes nothing** and does not say that its conclusion is still current.

There are **no webhooks, no server-sent events, no signatures on events and no per-client acknowledgements or retention**.

## 5. Restricted views (controlled disclosure of one output)

An operator can disclose **a selection** of a registered report's summary to another principal.

```bash
# operator: disclosures:issue, reports:read and full-report-summary@1
INVARIA_DATABASE_URL=postgresql://... uv run --locked invaria disclosure issue \
  --access-profile operator.json --report ir-<sha256> --recipient reviewer-1 \
  --policy aggregate-outcome-counts@1 [--json]
# later: disclosures:revoke
uv run --locked invaria disclosure revoke --access-profile operator.json --view dv-<32 hex>
```

**Issuance:**
- The store chooses a **random, opaque** `view_id` and records the issuance append-only: tenant, recipient principal, exact policy version, view format, report, issuer and time.
- **No domain event is emitted.**
- A revocation is another append-only row.
- The view is **projected when it is read**, from the immutable report and the policy version fixed at issue.

**What the reviewer can do.** The reviewer's profile has only `disclosures:read` and `aggregate-outcome-counts@1`; it inherits no other scope or policy. With it, the reviewer reads `GET /v1/disclosures/{view_id}`, which returns exactly:
- the policy and view identifiers;
- the number of included operations;
- the MATCH, BREAK and UNKNOWN counts with their denominator (admitted operations);
- the operations outside those results with their denominator (included operations);
- fixed definitions and fixed statements, starting with *"This view contains selected information from the original report. It does not allow reproducing the evaluation or independently verifying its correspondence with the original."*

**What the view never contains:**
- identifiers or hashes of the original;
- tenant, principals, operations or evaluations;
- financial profiles or engines;
- assets, amounts, addresses or evidence;
- dates, times or ages;
- filters, segments, case keys or links to the original.

`withheld` lists the same fixed categories for every view: it says nothing about what the original contains, and a withheld category is not missing evidence, a zero or UNKNOWN.

**Access and revocation:**
- The `view_id` grants nothing. An absent view, another principal's or tenant's, and a revoked one all give the same `404`.
- A response already being built when a revocation commits still completes. The next read gives `404`.
- **Revoking never withdraws a copy already delivered.**

**Limits:**
- The counts are exact: in small populations they can reveal information about individual operations, including by comparing several views. There is no suppression, noise or differential privacy.
- The view is not signed and cannot be verified independently of this service.
- The listing `GET /v1/disclosures` returns `view_id` and `policy_ref` of the views assigned to the principal, which reveals how many views it has.

## 6. What the disclosure boundary does not cover yet

The boundary applies to every interactive query (REST, console, MCP and the CLI query commands) and to each event type of the feed. It does **not** yet cover:
- **Exports, beyond `analytics report --out`:**
  - evidence bundles (`invaria bundle`, `sign`);
  - the offline commands over corpus files (`check`, `evaluate --out`, `diagnose`, `obligations`, `explain-change`, `demo*`) and `stellar ledger-verify --out`.

  These now write safely (no overwriting, no symbolic links followed), but **safe writing is not a disclosure policy**: they still have no export permission, policy or projection. Once written, any file, including the one `analytics report --out` creates, is in the custody of whoever controls the file system. A file or a terminal output can be copied by anyone with access to the system. The `disclosure: private | synthetic_public` field of a bundle manifest is a label, not an executable policy.
- **Operational outputs:**
  - the access audit log (it stores a sha256 of the arguments);
  - the console's request log on stderr, which includes requested paths;
  - PostgreSQL's own logs;
  - logical backups;
  - what an MCP client, or the model provider behind it, does with what it receives.
- **External delivery of events:** no webhooks or server-sent events exist.

The restrictions are enforced by the application. They do **not** protect against anyone with direct administrative access to the database, the files or the backups.

## 7. Not implemented

- Zero-knowledge proofs, fully homomorphic encryption or any other cryptographic privacy technique.
- Webhooks, server-sent events, signed events, event retention.
- Institutional or remote authentication (OIDC, SSO), per-user sessions, hosted or multi-tenant operation, row-level security. One access profile per local process is the only identity.
- Restricted views of any other output than a report summary; controlled exports of other outputs (a restricted view, bundles, `evaluate --out`, `ledger-verify --out`).
- Techniques against inference from small or repeated counts.

## 8. Results and coverage keep their limits

None of this changes how conclusions are reached:
- The engines, profiles, coverage levels (on-chain coverage is `provider_claimed`) and financial results are unchanged.
- Analytics counts and presents stored conclusions; it never re-evaluates them.
- UNKNOWN never means unpaid, and BREAK is never a loss.
- A disclosed or full view of a conclusion carries the same limits as the conclusion itself; see [CAPABILITIES.md](CAPABILITIES.md).
