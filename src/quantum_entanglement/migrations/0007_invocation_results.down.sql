CREATE TEMP TABLE qe_invocation_results_down_guard (
    must_be_empty INTEGER NOT NULL CHECK(must_be_empty = 0)
);

INSERT INTO qe_invocation_results_down_guard(must_be_empty)
SELECT 1
WHERE EXISTS(SELECT 1 FROM invocation_result_manifests LIMIT 1)
   OR EXISTS(SELECT 1 FROM invocation_result_requests LIMIT 1)
   OR EXISTS(SELECT 1 FROM invocation_result_receipts LIMIT 1)
   OR EXISTS(SELECT 1 FROM invocation_result_artifacts LIMIT 1)
   OR EXISTS(SELECT 1 FROM invocation_result_publications LIMIT 1);

DROP TABLE qe_invocation_results_down_guard;

DROP TABLE invocation_result_publications;
DROP TABLE invocation_result_artifacts;
DROP TABLE invocation_result_receipts;
DROP TABLE invocation_result_requests;
DROP TABLE invocation_result_manifests;

DROP INDEX uq_outbox_result_publication_binding;
DROP INDEX uq_events_result_receipt_coordinates;
DROP INDEX uq_artifact_versions_result_binding;
DROP INDEX uq_invocation_admissions_result_binding;
DROP INDEX uq_invocation_attempts_result_binding;
DROP INDEX uq_invocation_jobs_result_binding;

DELETE FROM qe_schema_migrations WHERE version = 7;
