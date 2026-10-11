-- Generation of the event feed. Append-only. The current generation is the row
-- with the highest ordinal. A new one is written by the supported restore (invaria db
-- restore) before the restored database is served, never by a process restart. A cursor
-- names its generation and is authenticated with that generation's key, so a cursor of a
-- restored (or otherwise replaced) history is refused explicitly.
SET LOCAL ROLE invaria_owner;

CREATE TABLE invaria.feed_generations (
  ordinal        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  generation_id  text NOT NULL UNIQUE DEFAULT ('fg-' || replace(gen_random_uuid()::text, '-', '')),
  -- 32 bytes from two random UUIDs (~244 random bits): server-side key for cursor tags.
  cursor_key     bytea NOT NULL DEFAULT (uuid_send(gen_random_uuid()) || uuid_send(gen_random_uuid())),
  reason         text NOT NULL CHECK (reason = 'initial' OR reason LIKE 'restore:%'),
  created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (generation_id ~ '^fg-[0-9a-f]{32}$'),
  CHECK (octet_length(cursor_key) = 32)
);
INSERT INTO invaria.feed_generations (reason) VALUES ('initial');

REVOKE ALL ON invaria.feed_generations FROM PUBLIC;
-- The serving roles read it (the key verifies cursor tags); nobody but the owner or an
-- administrator running the restore can add a generation.
GRANT SELECT ON invaria.feed_generations TO invaria_app, invaria_reader;

RESET ROLE;
