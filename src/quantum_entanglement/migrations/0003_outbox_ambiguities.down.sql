DROP INDEX IF EXISTS idx_outbox_ambiguities_opened;
DROP INDEX IF EXISTS idx_outbox_ambiguities_one_open;
DROP TABLE IF EXISTS outbox_ambiguities;
DELETE FROM qe_schema_migrations WHERE version = 3;
