CREATE TABLE IF NOT EXISTS invocation_admissions (
    invocation_id TEXT PRIMARY KEY,
    receipt_format TEXT NOT NULL,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    job_idempotency_key TEXT NOT NULL,
    original_version INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    event_ids_json TEXT NOT NULL,
    first_sequence INTEGER NOT NULL,
    last_sequence INTEGER NOT NULL,
    first_global_position INTEGER NOT NULL,
    last_global_position INTEGER NOT NULL,
    event_manifest_sha256 TEXT NOT NULL,
    job_binding_sha256 TEXT NOT NULL,
    admitted_at TEXT NOT NULL,
    UNIQUE(session_id, task_id),
    UNIQUE(session_id, job_idempotency_key),
    FOREIGN KEY(invocation_id)
        REFERENCES invocation_jobs(invocation_id) ON DELETE RESTRICT,
    FOREIGN KEY(stream_id, first_sequence)
        REFERENCES events(stream_id, sequence) ON DELETE RESTRICT,
    FOREIGN KEY(stream_id, last_sequence)
        REFERENCES events(stream_id, sequence) ON DELETE RESTRICT,
    FOREIGN KEY(first_global_position)
        REFERENCES events(global_position) ON DELETE RESTRICT,
    FOREIGN KEY(last_global_position)
        REFERENCES events(global_position) ON DELETE RESTRICT,
    CHECK(receipt_format = 'qe.invocation-admission-receipt/1'),
    CHECK(stream_id = 'session:' || session_id),
    CHECK(original_version >= 0),
    CHECK(event_count > 0),
    CHECK(first_sequence = original_version + 1),
    CHECK(last_sequence = original_version + event_count),
    CHECK(last_global_position = first_global_position + event_count - 1),
    CHECK(
        length(event_manifest_sha256) = 64
        AND event_manifest_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(
        length(job_binding_sha256) = 64
        AND job_binding_sha256 NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE INDEX IF NOT EXISTS idx_invocation_admissions_stream
    ON invocation_admissions(stream_id, first_sequence);
