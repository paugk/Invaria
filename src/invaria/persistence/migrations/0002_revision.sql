-- Revision epochs, compare-and-swap publication, transactional outbox and knowledge cuts.
-- Everything is append-only: the current view is derived, never updated in place.

CREATE TABLE invaria.scope_epochs (
  tenant_id          text NOT NULL,
  operation_ref      text NOT NULL,
  epoch              bigint NOT NULL CHECK (epoch >= 1),
  profile_ref        text NOT NULL REFERENCES invaria.profiles (profile_ref),
  cause              text NOT NULL CHECK (cause IN (
                       'registered', 'evidence_appended', 'profile_changed', 'explicit')),
  knowledge_floor    timestamptz,
  document           jsonb NOT NULL,
  inserted_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, operation_ref, epoch)
);

CREATE TABLE invaria.publications (
  tenant_id      text NOT NULL,
  operation_ref  text NOT NULL,
  epoch          bigint NOT NULL,
  evaluation_id  text NOT NULL,
  inserted_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, operation_ref, epoch),
  FOREIGN KEY (tenant_id, operation_ref, epoch)
    REFERENCES invaria.scope_epochs (tenant_id, operation_ref, epoch),
  FOREIGN KEY (tenant_id, evaluation_id) REFERENCES invaria.evaluations (tenant_id, evaluation_id)
);

CREATE TABLE invaria.publish_attempts (
  tenant_id      text NOT NULL,
  evaluation_id  text NOT NULL,
  epoch          bigint NOT NULL,
  outcome        text NOT NULL CHECK (outcome IN ('PUBLISHED', 'NOT_CURRENT')),
  detail         text NOT NULL,
  inserted_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, evaluation_id, epoch),
  FOREIGN KEY (tenant_id, evaluation_id) REFERENCES invaria.evaluations (tenant_id, evaluation_id)
);

CREATE TABLE invaria.outbox (
  event_seq      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id      text NOT NULL,
  event_type     text NOT NULL CHECK (event_type IN ('scope.invalidated', 'evaluation.published')),
  operation_ref  text NOT NULL,
  epoch          bigint NOT NULL,
  dedup_key      text NOT NULL UNIQUE,
  payload        jsonb NOT NULL,
  inserted_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE invaria.outbox_acks (
  consumer_id  text NOT NULL,
  event_seq    bigint NOT NULL REFERENCES invaria.outbox (event_seq),
  inserted_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (consumer_id, event_seq)
);

CREATE TABLE invaria.knowledge_cuts (
  tenant_id    text NOT NULL,
  known_at     timestamptz NOT NULL,
  inserted_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, known_at)
);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['scope_epochs', 'publications', 'publish_attempts', 'outbox',
                           'outbox_acks', 'knowledge_cuts'] LOOP
    EXECUTE format('ALTER TABLE invaria.%I OWNER TO invaria_owner', t);
    EXECUTE format('REVOKE ALL ON invaria.%I FROM PUBLIC', t);
    EXECUTE format('GRANT SELECT, INSERT ON invaria.%I TO invaria_app', t);
  END LOOP;
END
$$;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA invaria TO invaria_app;
