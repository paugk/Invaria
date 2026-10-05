-- Read-only role for consultative interfaces (query layer, MCP): SELECT only.
-- Tables created by later migrations must grant SELECT to invaria_reader explicitly.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'invaria_reader') THEN
    CREATE ROLE invaria_reader NOLOGIN;
  END IF;
END
$$;
GRANT USAGE ON SCHEMA invaria TO invaria_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA invaria TO invaria_reader;
REVOKE SELECT ON invaria.schema_migrations FROM invaria_reader;
