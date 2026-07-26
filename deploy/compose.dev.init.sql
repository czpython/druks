-- Runs once on first boot (docker-entrypoint-initdb.d). POSTGRES_DB gave us
-- `druks_test` for the test suite; the dev server gets its own database so a
-- pytest run (which drops the public schema) can't eat dev data.
CREATE DATABASE druks_dev OWNER druks;
