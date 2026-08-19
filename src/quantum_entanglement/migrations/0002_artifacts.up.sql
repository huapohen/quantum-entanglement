CREATE TABLE IF NOT EXISTS artifact_blobs (
    digest TEXT PRIMARY KEY,
    content BLOB NOT NULL,
    byte_size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(
        digest LIKE 'sha256:%'
        AND length(digest) = 71
        AND substr(digest, 8) NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(byte_size >= 0),
    CHECK(length(content) = byte_size)
);

CREATE TABLE IF NOT EXISTS artifact_versions (
    artifact_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_version INTEGER,
    media_type TEXT NOT NULL,
    blob_digest TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    UNIQUE(tenant_id, workspace_id, session_id, name, version),
    UNIQUE(tenant_id, workspace_id, idempotency_key),
    FOREIGN KEY(blob_digest)
        REFERENCES artifact_blobs(digest) ON DELETE RESTRICT,
    CHECK(version > 0),
    CHECK(
        (version = 1 AND parent_version IS NULL)
        OR (version > 1 AND parent_version = version - 1)
    ),
    CHECK(byte_size >= 0),
    CHECK(length(request_digest) = 64 AND request_digest NOT GLOB '*[^0-9a-f]*')
);

CREATE INDEX IF NOT EXISTS idx_artifact_versions_head
    ON artifact_versions(tenant_id, workspace_id, session_id, name, version DESC);
CREATE INDEX IF NOT EXISTS idx_artifact_versions_task
    ON artifact_versions(tenant_id, workspace_id, session_id, task_id, version);
CREATE INDEX IF NOT EXISTS idx_artifact_versions_digest
    ON artifact_versions(blob_digest, artifact_id);
