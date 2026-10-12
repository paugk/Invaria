-- Export attempts and outcomes. Append-only.
-- An attempt names the content an export was about to write (hashes, size), the policies
-- applied, the tenant and the configured principal; an outcome is a row of its own, at most
-- one per attempt, never an update. An attempt without outcome stays so: it is never turned
-- into a failure, because the file may or may not exist. No path, file name, host,
-- recipient, error message or content is stored. The record is the service's declaration
-- of what it executed: not proof of delivery or custody, and not atomic with the file system.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'invaria_exporter') THEN
    CREATE ROLE invaria_exporter NOLOGIN;
  END IF;
END
$$;

SET LOCAL ROLE invaria_owner;

CREATE TABLE invaria.export_attempts (
  export_id       text PRIMARY KEY
                  DEFAULT ('ex-' || replace(gen_random_uuid()::text, '-', '')),
  tenant_id       text NOT NULL,
  principal_id    text NOT NULL,
  kind            text NOT NULL CHECK (kind IN ('integrity_report_file')),
  file_format     text NOT NULL CHECK (file_format IN ('integrity-report-file@1')),
  policy_refs     text[] NOT NULL CHECK (cardinality(policy_refs) BETWEEN 1 AND 16),
  report_sha256   char(64) NOT NULL CHECK (report_sha256 ~ '^[0-9a-f]{64}$'),
  content_sha256  char(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  content_bytes   bigint NOT NULL CHECK (content_bytes > 0),
  attempted_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (export_id ~ '^ex-[0-9a-f]{32}$'),
  UNIQUE (tenant_id, export_id)
);
CREATE INDEX export_attempts_by_tenant ON invaria.export_attempts (tenant_id, attempted_at);

CREATE TABLE invaria.export_outcomes (
  export_id    text PRIMARY KEY,
  tenant_id    text NOT NULL,
  outcome      text NOT NULL CHECK (outcome IN ('completed', 'failed')),
  failure      text CHECK (failure IN
                 ('destination_exists', 'write_error', 'publish_error', 'interrupted')),
  recorded_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK ((outcome = 'failed') = (failure IS NOT NULL)),
  FOREIGN KEY (tenant_id, export_id) REFERENCES invaria.export_attempts (tenant_id, export_id)
);

REVOKE ALL ON invaria.export_attempts, invaria.export_outcomes FROM PUBLIC;
-- The exporter reads what a report computation reads (as invaria_reader does) and only
-- inserts into the export record; export_id, attempted_at and recorded_at are set by the
-- store. No business table is writable and nothing can be updated or deleted.
GRANT USAGE ON SCHEMA invaria TO invaria_exporter;
GRANT SELECT ON ALL TABLES IN SCHEMA invaria TO invaria_exporter;
REVOKE SELECT ON invaria.schema_migrations FROM invaria_exporter;
GRANT INSERT (tenant_id, principal_id, kind, file_format, policy_refs, report_sha256,
              content_sha256, content_bytes)
  ON invaria.export_attempts TO invaria_exporter;
GRANT INSERT (export_id, tenant_id, outcome, failure)
  ON invaria.export_outcomes TO invaria_exporter;
-- The reader may consult the record (attempts without outcome included); the application
-- role has no access to it.
GRANT SELECT ON invaria.export_attempts, invaria.export_outcomes TO invaria_reader;
REVOKE ALL ON invaria.export_attempts, invaria.export_outcomes FROM invaria_app;

RESET ROLE;
