DROP INDEX IF EXISTS idx_invocation_admissions_stream;
DROP TABLE IF EXISTS invocation_admissions;
DELETE FROM qe_schema_migrations WHERE version = 4;
