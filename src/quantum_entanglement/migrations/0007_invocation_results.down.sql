CREATE TEMP TABLE qe_invocation_results_down_guard (
    must_be_empty INTEGER NOT NULL CHECK(must_be_empty = 0)
);

INSERT INTO qe_invocation_results_down_guard(must_be_empty)
SELECT 1
WHERE EXISTS(SELECT 1 FROM invocation_result_manifests LIMIT 1)
   OR EXISTS(SELECT 1 FROM invocation_result_requests LIMIT 1)
   OR EXISTS(SELECT 1 FROM invocation_result_event_bindings LIMIT 1)
   OR EXISTS(SELECT 1 FROM invocation_result_receipts LIMIT 1)
   OR EXISTS(SELECT 1 FROM invocation_result_artifacts LIMIT 1)
   OR EXISTS(SELECT 1 FROM invocation_result_publications LIMIT 1);

INSERT INTO qe_invocation_results_down_guard(must_be_empty)
SELECT 1
WHERE EXISTS(
    SELECT 1
    FROM qe_schema_migration_dependencies
    WHERE depends_on_version = 7
      AND migration_version <> 7
    LIMIT 1
);

DROP TABLE qe_invocation_results_down_guard;

DROP TABLE invocation_result_publications;
DROP TABLE invocation_result_artifacts;
DROP TABLE invocation_result_receipts;
DROP TABLE invocation_result_event_bindings;
DROP TABLE invocation_result_requests;
DROP TABLE invocation_result_manifests;

DELETE FROM qe_schema_migration_dependencies WHERE migration_version = 7;
DELETE FROM qe_schema_migration_metadata WHERE migration_version = 7;
DELETE FROM qe_schema_migrations WHERE version = 7;
