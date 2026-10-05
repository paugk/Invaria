-- Initial schema: append-only evidence store with closed snapshots.
-- Documents are stored whole (JSONB) and re-validated by the contracts on read;
-- extracted columns exist only for keys, filters and integrity checks.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'invaria_owner') THEN
    CREATE ROLE invaria_owner NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'invaria_app') THEN
    CREATE ROLE invaria_app NOLOGIN;
  END IF;
END
$$;

CREATE TABLE invaria.profiles (
  profile_ref  text PRIMARY KEY,
  document     jsonb NOT NULL,
  inserted_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (document->>'profile_ref' = profile_ref)
);

CREATE TABLE invaria.observations (
  tenant_id      text NOT NULL,
  observation_id text NOT NULL,
  source_id      text NOT NULL,
  record_key     text NOT NULL,
  revision       integer NOT NULL CHECK (revision >= 1),
  fact_type      text NOT NULL,
  kind           text NOT NULL CHECK (kind IN ('assertion', 'retraction')),
  operation_ref  text,
  valid_time     timestamptz NOT NULL,
  recorded_at    timestamptz NOT NULL,
  raw_sha256     char(64) NOT NULL,
  mapping_ref    text NOT NULL,
  supersedes     text,
  document       jsonb NOT NULL,
  journal_seq    bigint GENERATED ALWAYS AS IDENTITY,
  inserted_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, observation_id),
  CHECK (document->>'observation_id' = observation_id),
  CHECK (document->>'tenant_id' = tenant_id),
  CHECK (document->'source'->>'record_key' = record_key)
);
CREATE INDEX observations_known ON invaria.observations (tenant_id, recorded_at);
CREATE INDEX observations_record ON invaria.observations (tenant_id, source_id, record_key, revision);
CREATE INDEX observations_operation ON invaria.observations (tenant_id, operation_ref);

CREATE TABLE invaria.coverage_certificates (
  tenant_id    text NOT NULL,
  coverage_id  text NOT NULL,
  source_id    text NOT NULL,
  recorded_at  timestamptz NOT NULL,
  document     jsonb NOT NULL,
  journal_seq  bigint GENERATED ALWAYS AS IDENTITY,
  inserted_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, coverage_id),
  CHECK (document->>'coverage_id' = coverage_id),
  CHECK (document->>'tenant_id' = tenant_id)
);

CREATE TABLE invaria.identity_links (
  tenant_id    text NOT NULL,
  link_id      text NOT NULL,
  recorded_at  timestamptz NOT NULL,
  document     jsonb NOT NULL,
  journal_seq  bigint GENERATED ALWAYS AS IDENTITY,
  inserted_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, link_id),
  CHECK (document->>'link_id' = link_id)
);

CREATE TABLE invaria.snapshots (
  tenant_id         text NOT NULL,
  snapshot_id       text NOT NULL,
  operation_ref     text NOT NULL,
  valid_at          timestamptz NOT NULL,
  known_at          timestamptz NOT NULL,
  evaluation_clock  timestamptz NOT NULL,
  profile_ref       text NOT NULL REFERENCES invaria.profiles (profile_ref),
  document          jsonb NOT NULL,
  inserted_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, snapshot_id),
  CHECK (document->>'snapshot_id' = snapshot_id),
  CHECK (document->>'tenant_id' = tenant_id),
  CHECK (evaluation_clock >= known_at)
);

CREATE TABLE invaria.snapshot_observations (
  tenant_id      text NOT NULL,
  snapshot_id    text NOT NULL,
  observation_id text NOT NULL,
  PRIMARY KEY (tenant_id, snapshot_id, observation_id),
  FOREIGN KEY (tenant_id, snapshot_id) REFERENCES invaria.snapshots (tenant_id, snapshot_id),
  FOREIGN KEY (tenant_id, observation_id) REFERENCES invaria.observations (tenant_id, observation_id)
);

CREATE TABLE invaria.snapshot_coverage (
  tenant_id    text NOT NULL,
  snapshot_id  text NOT NULL,
  coverage_id  text NOT NULL,
  PRIMARY KEY (tenant_id, snapshot_id, coverage_id),
  FOREIGN KEY (tenant_id, snapshot_id) REFERENCES invaria.snapshots (tenant_id, snapshot_id),
  FOREIGN KEY (tenant_id, coverage_id) REFERENCES invaria.coverage_certificates (tenant_id, coverage_id)
);

CREATE TABLE invaria.snapshot_identity_links (
  tenant_id    text NOT NULL,
  snapshot_id  text NOT NULL,
  link_id      text NOT NULL,
  PRIMARY KEY (tenant_id, snapshot_id, link_id),
  FOREIGN KEY (tenant_id, snapshot_id) REFERENCES invaria.snapshots (tenant_id, snapshot_id),
  FOREIGN KEY (tenant_id, link_id) REFERENCES invaria.identity_links (tenant_id, link_id)
);

CREATE TABLE invaria.evaluations (
  tenant_id      text NOT NULL,
  evaluation_id  text NOT NULL,
  snapshot_id    text NOT NULL,
  result         text NOT NULL CHECK (result IN ('MATCH', 'BREAK', 'UNKNOWN')),
  engine_ref     text NOT NULL,
  document       jsonb NOT NULL,
  inserted_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, evaluation_id),
  FOREIGN KEY (tenant_id, snapshot_id) REFERENCES invaria.snapshots (tenant_id, snapshot_id),
  CHECK (document->>'evaluation_id' = evaluation_id),
  CHECK (document->>'result' = result)
);

-- Ownership and privileges: history is append-only for the application role.
DO $$
DECLARE t record;
BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'invaria' LOOP
    EXECUTE format('ALTER TABLE invaria.%I OWNER TO invaria_owner', t.tablename);
    EXECUTE format('REVOKE ALL ON invaria.%I FROM PUBLIC', t.tablename);
  END LOOP;
END
$$;
ALTER SCHEMA invaria OWNER TO invaria_owner;
REVOKE ALL ON SCHEMA invaria FROM PUBLIC;
GRANT USAGE ON SCHEMA invaria TO invaria_app;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA invaria TO invaria_app;
REVOKE INSERT ON invaria.schema_migrations FROM invaria_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA invaria TO invaria_app;
