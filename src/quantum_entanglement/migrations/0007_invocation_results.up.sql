CREATE UNIQUE INDEX uq_invocation_jobs_result_binding
    ON invocation_jobs(
        invocation_id,
        session_id,
        plan_id,
        task_id,
        agent_id,
        idempotency_key
    );

CREATE UNIQUE INDEX uq_invocation_attempts_result_binding
    ON invocation_attempts(
        attempt_id,
        invocation_id,
        attempt_number,
        lease_epoch,
        worker_id,
        lease_token_digest
    );

CREATE UNIQUE INDEX uq_invocation_admissions_result_binding
    ON invocation_admissions(
        invocation_id,
        session_id,
        task_id,
        job_idempotency_key
    );

CREATE UNIQUE INDEX uq_artifact_versions_result_binding
    ON artifact_versions(tenant_id, workspace_id, artifact_id, version);

CREATE UNIQUE INDEX uq_events_result_receipt_coordinates
    ON events(event_id, stream_id, sequence, global_position, event_type, timestamp);

CREATE UNIQUE INDEX uq_outbox_result_publication_binding
    ON outbox(
        message_id,
        destination,
        idempotency_key,
        triggering_event_id,
        triggering_global_position,
        created_at
    );

CREATE TABLE invocation_result_manifests (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    canonical_bytes BLOB NOT NULL,
    byte_size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(tenant_id, workspace_id, manifest_digest),
    CHECK(typeof(tenant_id) = 'text' AND length(CAST(tenant_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(workspace_id) = 'text' AND length(CAST(workspace_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(
        typeof(manifest_digest) = 'text'
        AND length(manifest_digest) = 64
        AND manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(typeof(schema_version) = 'integer' AND schema_version = 2),
    CHECK(typeof(canonical_bytes) = 'blob'),
    CHECK(typeof(byte_size) = 'integer' AND byte_size BETWEEN 1 AND 1048576),
    CHECK(length(canonical_bytes) = byte_size),
    CHECK(typeof(created_at) = 'text' AND length(CAST(created_at AS BLOB)) = 27)
);

CREATE TABLE invocation_result_requests (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    acceptance_idempotency_key TEXT NOT NULL,
    request_identity_bytes BLOB NOT NULL,
    request_identity_byte_size INTEGER NOT NULL,
    invocation_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    job_idempotency_key TEXT NOT NULL,
    start_receipt_digest TEXT NOT NULL,
    execution_manifest_digest TEXT NOT NULL,
    result_manifest_digest TEXT NOT NULL,
    expected_stream_version INTEGER NOT NULL,
    running_task_revision INTEGER NOT NULL,
    terminal_task_revision INTEGER NOT NULL,
    correlation_id TEXT NOT NULL,
    causation_id TEXT NOT NULL,
    runtime_revision TEXT NOT NULL,
    effect_class TEXT NOT NULL,
    action_receipt_set_digest TEXT NOT NULL,
    result_ref TEXT NOT NULL,
    primary_artifact_id TEXT,
    artifact_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(tenant_id, workspace_id, request_digest),
    UNIQUE(tenant_id, workspace_id, invocation_id),
    UNIQUE(tenant_id, workspace_id, session_id, task_id),
    UNIQUE(tenant_id, workspace_id, session_id, acceptance_idempotency_key),
    UNIQUE(tenant_id, workspace_id, result_ref),
    FOREIGN KEY(
        invocation_id,
        session_id,
        plan_id,
        task_id,
        agent_id,
        job_idempotency_key
    ) REFERENCES invocation_jobs(
        invocation_id,
        session_id,
        plan_id,
        task_id,
        agent_id,
        idempotency_key
    ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(invocation_id, session_id, task_id, job_idempotency_key)
        REFERENCES invocation_admissions(
            invocation_id,
            session_id,
            task_id,
            job_idempotency_key
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(tenant_id, workspace_id, result_manifest_digest)
        REFERENCES invocation_result_manifests(
            tenant_id,
            workspace_id,
            manifest_digest
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK(typeof(tenant_id) = 'text' AND length(CAST(tenant_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(workspace_id) = 'text' AND length(CAST(workspace_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(
        typeof(request_digest) = 'text'
        AND length(request_digest) = 64
        AND request_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(typeof(schema_version) = 'integer' AND schema_version = 2),
    CHECK(
        typeof(acceptance_idempotency_key) = 'text'
        AND length(CAST(acceptance_idempotency_key AS BLOB)) BETWEEN 1 AND 4096
    ),
    CHECK(typeof(request_identity_bytes) = 'blob'),
    CHECK(
        typeof(request_identity_byte_size) = 'integer'
        AND request_identity_byte_size BETWEEN 1 AND 1048576
    ),
    CHECK(length(request_identity_bytes) = request_identity_byte_size),
    CHECK(typeof(invocation_id) = 'text' AND length(CAST(invocation_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(session_id) = 'text' AND length(CAST(session_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(plan_id) = 'text' AND length(CAST(plan_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(task_id) = 'text' AND length(CAST(task_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(agent_id) = 'text' AND length(CAST(agent_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(
        typeof(job_idempotency_key) = 'text'
        AND length(CAST(job_idempotency_key AS BLOB)) BETWEEN 1 AND 4096
    ),
    CHECK(
        typeof(start_receipt_digest) = 'text'
        AND length(start_receipt_digest) = 64
        AND start_receipt_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(
        typeof(execution_manifest_digest) = 'text'
        AND length(execution_manifest_digest) = 64
        AND execution_manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(
        typeof(result_manifest_digest) = 'text'
        AND length(result_manifest_digest) = 64
        AND result_manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(typeof(expected_stream_version) = 'integer' AND expected_stream_version >= 1),
    CHECK(typeof(running_task_revision) = 'integer' AND running_task_revision > 0),
    CHECK(
        typeof(terminal_task_revision) = 'integer'
        AND terminal_task_revision = running_task_revision + 1
    ),
    CHECK(typeof(correlation_id) = 'text' AND length(CAST(correlation_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(causation_id) = 'text' AND length(CAST(causation_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(causation_id = task_id),
    CHECK(typeof(runtime_revision) = 'text' AND length(CAST(runtime_revision AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(effect_class) = 'text' AND effect_class = 'pure'),
    CHECK(
        typeof(action_receipt_set_digest) = 'text'
        AND action_receipt_set_digest = '182345e882d96c5e54d8edfb195b03d2339b64a4da80d9b3d6f9a762fbd13da2'
    ),
    CHECK(typeof(result_ref) = 'text' AND length(CAST(result_ref AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(
        primary_artifact_id IS NULL
        OR (
            typeof(primary_artifact_id) = 'text'
            AND length(CAST(primary_artifact_id AS BLOB)) BETWEEN 1 AND 4096
        )
    ),
    CHECK(typeof(artifact_count) = 'integer' AND artifact_count BETWEEN 0 AND 256),
    CHECK(artifact_count > 0 OR primary_artifact_id IS NULL),
    CHECK(typeof(created_at) = 'text' AND length(CAST(created_at AS BLOB)) = 27)
);

CREATE INDEX idx_invocation_result_requests_scope
    ON invocation_result_requests(
        tenant_id,
        workspace_id,
        session_id,
        plan_id,
        task_id,
        agent_id,
        invocation_id
    );

CREATE INDEX idx_invocation_result_requests_manifest
    ON invocation_result_requests(tenant_id, workspace_id, result_manifest_digest);

CREATE TABLE invocation_result_receipts (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    request_digest TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    job_idempotency_key TEXT NOT NULL,
    acceptance_idempotency_key TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    lease_epoch INTEGER NOT NULL,
    worker_id TEXT NOT NULL,
    lease_token_digest TEXT NOT NULL,
    start_receipt_digest TEXT NOT NULL,
    execution_manifest_digest TEXT NOT NULL,
    result_manifest_schema_version INTEGER NOT NULL,
    result_manifest_digest TEXT NOT NULL,
    result_ref TEXT NOT NULL,
    effect_class TEXT NOT NULL,
    action_receipt_set_digest TEXT NOT NULL,
    expected_stream_version INTEGER NOT NULL,
    running_task_revision INTEGER NOT NULL,
    terminal_task_revision INTEGER NOT NULL,
    accepted_at TEXT NOT NULL,
    artifact_count INTEGER NOT NULL,
    result_evidence_digest TEXT NOT NULL,
    terminal_transition_digest TEXT NOT NULL,
    receipt_digest TEXT NOT NULL,
    result_event_id TEXT NOT NULL,
    result_event_stream_id TEXT NOT NULL,
    result_event_type TEXT NOT NULL,
    result_event_timestamp TEXT NOT NULL,
    result_event_sequence INTEGER NOT NULL,
    result_event_global_position INTEGER NOT NULL,
    result_event_envelope_digest TEXT NOT NULL,
    terminal_event_id TEXT NOT NULL,
    terminal_event_stream_id TEXT NOT NULL,
    terminal_event_type TEXT NOT NULL,
    terminal_event_timestamp TEXT NOT NULL,
    terminal_event_sequence INTEGER NOT NULL,
    terminal_event_global_position INTEGER NOT NULL,
    terminal_event_envelope_digest TEXT NOT NULL,
    PRIMARY KEY(tenant_id, workspace_id, receipt_id),
    UNIQUE(tenant_id, workspace_id, invocation_id),
    UNIQUE(tenant_id, workspace_id, attempt_id),
    UNIQUE(tenant_id, workspace_id, session_id, acceptance_idempotency_key),
    UNIQUE(tenant_id, workspace_id, result_ref),
    UNIQUE(tenant_id, workspace_id, result_event_id),
    UNIQUE(tenant_id, workspace_id, terminal_event_id),
    UNIQUE(tenant_id, workspace_id, result_event_global_position),
    UNIQUE(tenant_id, workspace_id, terminal_event_global_position),
    UNIQUE(
        tenant_id,
        workspace_id,
        receipt_id,
        terminal_event_id,
        terminal_event_global_position
    ),
    FOREIGN KEY(tenant_id, workspace_id, request_digest)
        REFERENCES invocation_result_requests(tenant_id, workspace_id, request_digest)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(
        attempt_id,
        invocation_id,
        attempt_number,
        lease_epoch,
        worker_id,
        lease_token_digest
    ) REFERENCES invocation_attempts(
        attempt_id,
        invocation_id,
        attempt_number,
        lease_epoch,
        worker_id,
        lease_token_digest
    ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(
        result_event_id,
        result_event_stream_id,
        result_event_sequence,
        result_event_global_position,
        result_event_type,
        result_event_timestamp
    ) REFERENCES events(
        event_id,
        stream_id,
        sequence,
        global_position,
        event_type,
        timestamp
    ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(
        terminal_event_id,
        terminal_event_stream_id,
        terminal_event_sequence,
        terminal_event_global_position,
        terminal_event_type,
        terminal_event_timestamp
    ) REFERENCES events(
        event_id,
        stream_id,
        sequence,
        global_position,
        event_type,
        timestamp
    ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK(typeof(tenant_id) = 'text' AND length(CAST(tenant_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(workspace_id) = 'text' AND length(CAST(workspace_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(receipt_id) = 'text' AND length(CAST(receipt_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(schema_version) = 'integer' AND schema_version = 2),
    CHECK(
        typeof(request_digest) = 'text'
        AND length(request_digest) = 64
        AND request_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(typeof(invocation_id) = 'text' AND length(CAST(invocation_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(session_id) = 'text' AND length(CAST(session_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(plan_id) = 'text' AND length(CAST(plan_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(task_id) = 'text' AND length(CAST(task_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(agent_id) = 'text' AND length(CAST(agent_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(
        typeof(job_idempotency_key) = 'text'
        AND length(CAST(job_idempotency_key AS BLOB)) BETWEEN 1 AND 4096
    ),
    CHECK(
        typeof(acceptance_idempotency_key) = 'text'
        AND length(CAST(acceptance_idempotency_key AS BLOB)) BETWEEN 1 AND 4096
    ),
    CHECK(typeof(attempt_id) = 'text' AND length(CAST(attempt_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(attempt_number) = 'integer' AND attempt_number > 0),
    CHECK(typeof(lease_epoch) = 'integer' AND lease_epoch > 0),
    CHECK(typeof(worker_id) = 'text' AND length(CAST(worker_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(
        typeof(lease_token_digest) = 'text'
        AND length(lease_token_digest) = 64
        AND lease_token_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(
        typeof(start_receipt_digest) = 'text'
        AND length(start_receipt_digest) = 64
        AND start_receipt_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(
        typeof(execution_manifest_digest) = 'text'
        AND length(execution_manifest_digest) = 64
        AND execution_manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(typeof(result_manifest_schema_version) = 'integer' AND result_manifest_schema_version = 2),
    CHECK(
        typeof(result_manifest_digest) = 'text'
        AND length(result_manifest_digest) = 64
        AND result_manifest_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(typeof(result_ref) = 'text' AND length(CAST(result_ref AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(effect_class) = 'text' AND effect_class = 'pure'),
    CHECK(
        typeof(action_receipt_set_digest) = 'text'
        AND action_receipt_set_digest = '182345e882d96c5e54d8edfb195b03d2339b64a4da80d9b3d6f9a762fbd13da2'
    ),
    CHECK(typeof(expected_stream_version) = 'integer' AND expected_stream_version >= 1),
    CHECK(typeof(running_task_revision) = 'integer' AND running_task_revision > 0),
    CHECK(
        typeof(terminal_task_revision) = 'integer'
        AND terminal_task_revision = running_task_revision + 1
    ),
    CHECK(typeof(accepted_at) = 'text' AND length(CAST(accepted_at AS BLOB)) = 27),
    CHECK(typeof(artifact_count) = 'integer' AND artifact_count BETWEEN 0 AND 256),
    CHECK(
        typeof(result_evidence_digest) = 'text'
        AND length(result_evidence_digest) = 64
        AND result_evidence_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(
        typeof(terminal_transition_digest) = 'text'
        AND length(terminal_transition_digest) = 64
        AND terminal_transition_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(
        typeof(receipt_digest) = 'text'
        AND length(receipt_digest) = 64
        AND receipt_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(typeof(result_event_id) = 'text' AND length(CAST(result_event_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(result_event_stream_id = 'session:' || session_id),
    CHECK(result_event_type = 'task.invocation.result.accepted'),
    CHECK(result_event_timestamp = accepted_at),
    CHECK(
        typeof(result_event_sequence) = 'integer'
        AND result_event_sequence = expected_stream_version + 1
    ),
    CHECK(
        typeof(result_event_global_position) = 'integer'
        AND result_event_global_position > 0
    ),
    CHECK(
        typeof(result_event_envelope_digest) = 'text'
        AND length(result_event_envelope_digest) = 64
        AND result_event_envelope_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(typeof(terminal_event_id) = 'text' AND length(CAST(terminal_event_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(terminal_event_id <> result_event_id),
    CHECK(terminal_event_stream_id = result_event_stream_id),
    CHECK(terminal_event_type = 'task.status.changed'),
    CHECK(terminal_event_timestamp = accepted_at),
    CHECK(
        typeof(terminal_event_sequence) = 'integer'
        AND terminal_event_sequence = result_event_sequence + 1
    ),
    CHECK(
        typeof(terminal_event_global_position) = 'integer'
        AND terminal_event_global_position = result_event_global_position + 1
    ),
    CHECK(
        typeof(terminal_event_envelope_digest) = 'text'
        AND length(terminal_event_envelope_digest) = 64
        AND terminal_event_envelope_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(result_event_envelope_digest <> terminal_event_envelope_digest)
);

CREATE INDEX idx_invocation_result_receipts_scope
    ON invocation_result_receipts(
        tenant_id,
        workspace_id,
        session_id,
        plan_id,
        task_id,
        agent_id,
        invocation_id
    );

CREATE INDEX idx_invocation_result_receipts_request
    ON invocation_result_receipts(tenant_id, workspace_id, request_digest);

CREATE INDEX idx_invocation_result_receipts_manifest
    ON invocation_result_receipts(tenant_id, workspace_id, result_manifest_digest);

CREATE INDEX idx_invocation_result_receipts_attempt
    ON invocation_result_receipts(invocation_id, attempt_number, lease_epoch);

CREATE TABLE invocation_result_artifacts (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_version INTEGER,
    media_type TEXT NOT NULL,
    blob_digest TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    metadata_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    artifact_request_digest TEXT NOT NULL,
    candidate_digest TEXT NOT NULL,
    PRIMARY KEY(tenant_id, workspace_id, receipt_id, ordinal),
    UNIQUE(tenant_id, workspace_id, receipt_id, artifact_id),
    UNIQUE(tenant_id, workspace_id, receipt_id, name),
    UNIQUE(tenant_id, workspace_id, receipt_id, idempotency_key),
    FOREIGN KEY(tenant_id, workspace_id, receipt_id)
        REFERENCES invocation_result_receipts(tenant_id, workspace_id, receipt_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(tenant_id, workspace_id, artifact_id, version)
        REFERENCES artifact_versions(tenant_id, workspace_id, artifact_id, version)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK(typeof(tenant_id) = 'text' AND length(CAST(tenant_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(workspace_id) = 'text' AND length(CAST(workspace_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(receipt_id) = 'text' AND length(CAST(receipt_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(ordinal) = 'integer' AND ordinal BETWEEN 0 AND 255),
    CHECK(typeof(session_id) = 'text' AND length(CAST(session_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(task_id) = 'text' AND length(CAST(task_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(artifact_id) = 'text' AND length(CAST(artifact_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(name) = 'text' AND length(CAST(name AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(version) = 'integer' AND version > 0),
    CHECK(
        (version = 1 AND parent_version IS NULL)
        OR (
            version > 1
            AND typeof(parent_version) = 'integer'
            AND parent_version = version - 1
        )
    ),
    CHECK(
        typeof(media_type) = 'text'
        AND length(CAST(media_type AS BLOB)) BETWEEN 1 AND 255
        AND instr(media_type, '/') > 1
        AND instr(media_type, ' ') = 0
        AND instr(media_type, char(9)) = 0
        AND instr(media_type, char(10)) = 0
        AND instr(media_type, char(13)) = 0
    ),
    CHECK(
        typeof(blob_digest) = 'text'
        AND length(blob_digest) = 71
        AND substr(blob_digest, 1, 7) = 'sha256:'
        AND substr(blob_digest, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(typeof(byte_size) = 'integer' AND byte_size >= 0),
    CHECK(
        typeof(metadata_digest) = 'text'
        AND length(metadata_digest) = 64
        AND metadata_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(typeof(created_by) = 'text' AND length(CAST(created_by AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(
        typeof(idempotency_key) = 'text'
        AND length(CAST(idempotency_key AS BLOB)) BETWEEN 1 AND 4096
    ),
    CHECK(
        typeof(artifact_request_digest) = 'text'
        AND length(artifact_request_digest) = 64
        AND artifact_request_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(
        typeof(candidate_digest) = 'text'
        AND length(candidate_digest) = 64
        AND candidate_digest NOT GLOB '*[^0-9a-f]*'
    )
);

CREATE INDEX idx_invocation_result_artifacts_reverse
    ON invocation_result_artifacts(tenant_id, workspace_id, artifact_id, receipt_id);

CREATE TABLE invocation_result_publications (
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    publication_kind TEXT NOT NULL,
    message_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    headers_digest TEXT NOT NULL,
    triggering_event_id TEXT NOT NULL,
    triggering_global_position INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(tenant_id, workspace_id, receipt_id),
    UNIQUE(tenant_id, workspace_id, message_id),
    FOREIGN KEY(
        tenant_id,
        workspace_id,
        receipt_id,
        triggering_event_id,
        triggering_global_position
    ) REFERENCES invocation_result_receipts(
        tenant_id,
        workspace_id,
        receipt_id,
        terminal_event_id,
        terminal_event_global_position
    ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(
        message_id,
        destination,
        idempotency_key,
        triggering_event_id,
        triggering_global_position,
        created_at
    ) REFERENCES outbox(
        message_id,
        destination,
        idempotency_key,
        triggering_event_id,
        triggering_global_position,
        created_at
    ) ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK(typeof(tenant_id) = 'text' AND length(CAST(tenant_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(workspace_id) = 'text' AND length(CAST(workspace_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(receipt_id) = 'text' AND length(CAST(receipt_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(publication_kind) = 'text' AND publication_kind = 'result_terminal_outbox_v1'),
    CHECK(typeof(message_id) = 'text' AND length(CAST(message_id AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(typeof(destination) = 'text' AND length(CAST(destination AS BLOB)) BETWEEN 1 AND 4096),
    CHECK(
        typeof(idempotency_key) = 'text'
        AND length(CAST(idempotency_key AS BLOB)) BETWEEN 1 AND 4096
    ),
    CHECK(
        typeof(payload_digest) = 'text'
        AND length(payload_digest) = 64
        AND payload_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(
        typeof(headers_digest) = 'text'
        AND length(headers_digest) = 64
        AND headers_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(
        typeof(triggering_event_id) = 'text'
        AND length(CAST(triggering_event_id AS BLOB)) BETWEEN 1 AND 4096
    ),
    CHECK(
        typeof(triggering_global_position) = 'integer'
        AND triggering_global_position > 0
    ),
    CHECK(typeof(created_at) = 'text' AND length(CAST(created_at AS BLOB)) = 27)
);

CREATE INDEX idx_invocation_result_publications_trigger
    ON invocation_result_publications(
        triggering_global_position,
        tenant_id,
        workspace_id,
        receipt_id
    );
