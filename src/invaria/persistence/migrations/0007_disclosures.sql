-- Disclosure issuances and revocations. Append-only.
-- An issuance binds one registered integrity report of a tenant to one recipient principal
-- under one exact policy version and view format. The view is projected when it is read,
-- from the immutable report; nothing of the view is stored here. view_id is random and set
-- by the store (the application cannot choose it); it names an issuance, it never grants
-- access. A revocation is a row of its own, never an update or a delete.
SET LOCAL ROLE invaria_owner;

CREATE TABLE invaria.disclosure_issuances (
  view_id              text PRIMARY KEY
                       DEFAULT ('dv-' || replace(gen_random_uuid()::text, '-', '')),
  tenant_id            text NOT NULL,
  recipient_principal  text NOT NULL,
  policy_ref           text NOT NULL,
  view_format          text NOT NULL,
  report_id            text NOT NULL,
  issued_by            text NOT NULL,
  issued_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (view_id ~ '^dv-[0-9a-f]{32}$'),
  CHECK (policy_ref ~ '^[a-z][a-z0-9-]{0,62}@[1-9][0-9]{0,5}$'),
  UNIQUE (tenant_id, view_id),
  FOREIGN KEY (tenant_id, report_id) REFERENCES invaria.integrity_reports (tenant_id, report_id)
);
CREATE INDEX disclosure_issuances_by_recipient
  ON invaria.disclosure_issuances (tenant_id, recipient_principal, issued_at DESC, view_id);

CREATE TABLE invaria.disclosure_revocations (
  view_id     text PRIMARY KEY,
  tenant_id   text NOT NULL,
  revoked_by  text NOT NULL,
  revoked_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (tenant_id, view_id) REFERENCES invaria.disclosure_issuances (tenant_id, view_id)
);

REVOKE ALL ON invaria.disclosure_issuances, invaria.disclosure_revocations FROM PUBLIC;
GRANT SELECT ON invaria.disclosure_issuances, invaria.disclosure_revocations
  TO invaria_app, invaria_reader;
-- view_id, issued_at and revoked_at are excluded from the INSERT grants: the store sets them.
GRANT INSERT (tenant_id, recipient_principal, policy_ref, view_format, report_id, issued_by)
  ON invaria.disclosure_issuances TO invaria_app;
GRANT INSERT (view_id, tenant_id, revoked_by)
  ON invaria.disclosure_revocations TO invaria_app;

RESET ROLE;
