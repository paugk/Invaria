-- Domain events derived from the outbox. Append-only.
-- Runs as the owner: it alters a table invaria_owner owns and creates objects it will own.
SET LOCAL ROLE invaria_owner;

-- 1. The outbox learns one more internal type: a newly registered integrity report. It is
--    not about an operation, so operation_ref and epoch are null exactly for that type.
ALTER TABLE invaria.outbox DROP CONSTRAINT outbox_event_type_check;
ALTER TABLE invaria.outbox ADD CONSTRAINT outbox_event_type_check CHECK (event_type IN (
  'scope.invalidated', 'evaluation.published', 'integrity_report.registered'));
ALTER TABLE invaria.outbox ALTER COLUMN operation_ref DROP NOT NULL;
ALTER TABLE invaria.outbox ALTER COLUMN epoch DROP NOT NULL;
ALTER TABLE invaria.outbox ADD CONSTRAINT outbox_subject_check CHECK (
  (event_type = 'integrity_report.registered') = (operation_ref IS NULL AND epoch IS NULL));

-- 2. Per-tenant derivation counter. Only the trigger below writes it: the row lock taken by
--    its upsert serializes derivations of one tenant until commit, and a rollback undoes the
--    increment (unlike a sequence), so tenant_seq has no gaps.
CREATE TABLE invaria.domain_event_counters (
  tenant_id  text PRIMARY KEY,
  last_seq   bigint NOT NULL CHECK (last_seq >= 1)
);

-- 3. The events. payload holds the exact canonical bytes of the event (registry convention);
--    tenant_seq and derived_at are record metadata outside the payload, set by the trigger.
CREATE TABLE invaria.domain_events (
  tenant_id         text NOT NULL,
  tenant_seq        bigint NOT NULL CHECK (tenant_seq >= 1),
  event_id          text NOT NULL UNIQUE CHECK (event_id ~ '^ev-[0-9a-f]{64}$'),
  event_type        text NOT NULL CHECK (event_type IN (
                      'operation.scope_reopened', 'operation.evaluation_published',
                      'integrity_report.registered')),
  type_version      integer NOT NULL CHECK (type_version >= 1),
  operation_ref     text,
  epoch             bigint,
  report_id         text,
  source_event_seq  bigint NOT NULL UNIQUE REFERENCES invaria.outbox (event_seq),
  payload           bytea NOT NULL,
  payload_sha256    text NOT NULL,
  derived_at        timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, tenant_seq),
  CHECK (payload_sha256 = encode(sha256(payload), 'hex')),
  CHECK (octet_length(payload) BETWEEN 2 AND 1048576),
  CHECK ((event_type = 'integrity_report.registered') =
         (report_id IS NOT NULL AND operation_ref IS NULL AND epoch IS NULL)),
  CHECK ((event_type <> 'integrity_report.registered') =
         (operation_ref IS NOT NULL AND epoch IS NOT NULL AND report_id IS NULL))
);

CREATE FUNCTION invaria.assign_domain_event_seq() RETURNS trigger
  LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  INSERT INTO invaria.domain_event_counters AS c (tenant_id, last_seq)
    VALUES (NEW.tenant_id, 1)
    ON CONFLICT (tenant_id) DO UPDATE SET last_seq = c.last_seq + 1
    RETURNING c.last_seq INTO NEW.tenant_seq;
  NEW.derived_at := clock_timestamp();
  RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION invaria.assign_domain_event_seq() FROM PUBLIC;
CREATE TRIGGER domain_events_assign_seq BEFORE INSERT ON invaria.domain_events
  FOR EACH ROW EXECUTE FUNCTION invaria.assign_domain_event_seq();

REVOKE ALL ON invaria.domain_event_counters, invaria.domain_events FROM PUBLIC;
GRANT SELECT ON invaria.domain_events TO invaria_app, invaria_reader;
GRANT SELECT ON invaria.domain_event_counters TO invaria_reader;
-- tenant_seq and derived_at are not in the grant: the application cannot set them.
GRANT INSERT (tenant_id, event_id, event_type, type_version, operation_ref, epoch, report_id,
              source_event_seq, payload, payload_sha256)
  ON invaria.domain_events TO invaria_app;

RESET ROLE;
