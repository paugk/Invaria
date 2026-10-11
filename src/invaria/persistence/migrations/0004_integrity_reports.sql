-- Registry of integrity reports computed by the service. Append-only.
-- document holds the exact bytes the service serialized (canonical JSON, UTF-8); they are
-- never re-serialized by the database, so jsonb normalization cannot change them.
-- report_sha256 is the report's own hash (its canonical JSON without that key);
-- document_sha256 is the sha256 of the stored bytes.

CREATE TABLE invaria.integrity_reports (
  tenant_id         text NOT NULL,
  report_id         text NOT NULL,
  report_sha256     text NOT NULL CHECK (report_sha256 ~ '^[0-9a-f]{64}$'),
  report_format     text NOT NULL,
  definition        text NOT NULL,
  selection_policy  text NOT NULL,
  evidence_horizon  timestamptz NOT NULL,
  store_read_at     timestamptz,
  registered_by     text NOT NULL,
  document          bytea NOT NULL,
  document_sha256   text NOT NULL,
  registered_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, report_id),
  CHECK (report_id = 'ir-' || report_sha256),
  CHECK (document_sha256 = encode(sha256(document), 'hex')),
  CHECK (octet_length(document) BETWEEN 2 AND 67108864)
);
CREATE INDEX integrity_reports_by_registration
  ON invaria.integrity_reports (tenant_id, registered_at DESC, report_id DESC);

ALTER TABLE invaria.integrity_reports OWNER TO invaria_owner;
REVOKE ALL ON invaria.integrity_reports FROM PUBLIC;
-- registered_at is excluded from the INSERT grant: only the store clock sets it.
GRANT SELECT ON invaria.integrity_reports TO invaria_app, invaria_reader;
GRANT INSERT (tenant_id, report_id, report_sha256, report_format, definition,
              selection_policy, evidence_horizon, store_read_at, registered_by, document,
              document_sha256)
  ON invaria.integrity_reports TO invaria_app;
