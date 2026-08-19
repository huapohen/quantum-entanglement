DROP INDEX IF EXISTS idx_artifact_versions_digest;
DROP INDEX IF EXISTS idx_artifact_versions_task;
DROP INDEX IF EXISTS idx_artifact_versions_head;
DROP TABLE IF EXISTS artifact_versions;
DROP TABLE IF EXISTS artifact_blobs;
DELETE FROM qe_schema_migrations WHERE version = 2;
