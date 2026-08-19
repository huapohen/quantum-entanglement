DROP INDEX IF EXISTS idx_invocation_attempts_status;
DROP INDEX IF EXISTS idx_invocation_attempts_job;
DROP TABLE IF EXISTS invocation_attempts;
DROP INDEX IF EXISTS idx_invocation_jobs_lease_expiry;
DROP INDEX IF EXISTS idx_invocation_jobs_session;
DROP INDEX IF EXISTS idx_invocation_jobs_claim;
DROP TABLE IF EXISTS invocation_jobs;
DELETE FROM qe_schema_migrations WHERE version = 1;
