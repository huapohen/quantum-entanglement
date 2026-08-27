CREATE TABLE IF NOT EXISTS native_im_auth_nonces (
    tenant_id TEXT NOT NULL CHECK(length(tenant_id) BETWEEN 1 AND 4096),
    workspace_id TEXT NOT NULL CHECK(length(workspace_id) BETWEEN 1 AND 4096),
    provider TEXT NOT NULL CHECK(length(provider) BETWEEN 1 AND 4096),
    channel_id TEXT NOT NULL CHECK(length(channel_id) BETWEEN 1 AND 4096),
    key_id TEXT NOT NULL CHECK(length(key_id) BETWEEN 1 AND 4096),
    nonce_digest TEXT NOT NULL CHECK(length(nonce_digest) = 64 AND nonce_digest NOT GLOB '*[^0-9a-f]*'),
    signed_at TEXT NOT NULL CHECK(length(signed_at) = 27),
    expires_at TEXT NOT NULL CHECK(length(expires_at) = 27 AND expires_at > signed_at),
    authentication_evidence_digest TEXT NOT NULL CHECK(length(authentication_evidence_digest) = 64 AND authentication_evidence_digest NOT GLOB '*[^0-9a-f]*'),
    profile_revision TEXT NOT NULL CHECK(length(profile_revision) BETWEEN 1 AND 4096),
    profile_digest TEXT NOT NULL CHECK(length(profile_digest) = 64 AND profile_digest NOT GLOB '*[^0-9a-f]*'),
    claimed_at TEXT NOT NULL CHECK(length(claimed_at) = 27),
    PRIMARY KEY (tenant_id, workspace_id, provider, channel_id, key_id, nonce_digest)
);

CREATE INDEX IF NOT EXISTS idx_native_im_auth_nonces_expiry
ON native_im_auth_nonces(tenant_id, workspace_id, provider, channel_id, expires_at);

CREATE TABLE IF NOT EXISTS native_im_inbox_events (
    tenant_id TEXT NOT NULL CHECK(length(tenant_id) BETWEEN 1 AND 4096),
    workspace_id TEXT NOT NULL CHECK(length(workspace_id) BETWEEN 1 AND 4096),
    provider TEXT NOT NULL CHECK(length(provider) BETWEEN 1 AND 4096),
    channel_id TEXT NOT NULL CHECK(length(channel_id) BETWEEN 1 AND 4096),
    event_id TEXT NOT NULL CHECK(length(event_id) BETWEEN 1 AND 4096),
    event_digest TEXT NOT NULL CHECK(length(event_digest) = 64 AND event_digest NOT GLOB '*[^0-9a-f]*'),
    event_json TEXT NOT NULL CHECK(length(CAST(event_json AS BLOB)) BETWEEN 1 AND 3145728),
    cursor TEXT NOT NULL CHECK(length(cursor) BETWEEN 1 AND 4096),
    sequence_number INTEGER NOT NULL CHECK(sequence_number >= 0),
    first_received_at TEXT NOT NULL CHECK(length(first_received_at) = 27),
    admitted_at TEXT NOT NULL CHECK(length(admitted_at) = 27),
    PRIMARY KEY (tenant_id, workspace_id, provider, channel_id, event_id),
    UNIQUE (tenant_id, workspace_id, provider, channel_id, sequence_number),
    UNIQUE (tenant_id, workspace_id, provider, channel_id, event_id, event_digest)
);

CREATE TABLE IF NOT EXISTS native_im_inbox_verifications (
    tenant_id TEXT NOT NULL CHECK(length(tenant_id) BETWEEN 1 AND 4096),
    workspace_id TEXT NOT NULL CHECK(length(workspace_id) BETWEEN 1 AND 4096),
    provider TEXT NOT NULL CHECK(length(provider) BETWEEN 1 AND 4096),
    channel_id TEXT NOT NULL CHECK(length(channel_id) BETWEEN 1 AND 4096),
    verification_id TEXT NOT NULL CHECK(length(verification_id) BETWEEN 1 AND 4096),
    event_id TEXT NOT NULL CHECK(length(event_id) BETWEEN 1 AND 4096),
    event_digest TEXT NOT NULL CHECK(length(event_digest) = 64 AND event_digest NOT GLOB '*[^0-9a-f]*'),
    envelope_digest TEXT NOT NULL CHECK(length(envelope_digest) = 64 AND envelope_digest NOT GLOB '*[^0-9a-f]*'),
    verifier_id TEXT NOT NULL CHECK(length(verifier_id) BETWEEN 1 AND 4096),
    authentication_evidence_digest TEXT NOT NULL CHECK(length(authentication_evidence_digest) = 64 AND authentication_evidence_digest NOT GLOB '*[^0-9a-f]*'),
    tenant_mapping_revision TEXT NOT NULL CHECK(length(tenant_mapping_revision) BETWEEN 1 AND 4096),
    verified_at TEXT NOT NULL CHECK(length(verified_at) = 27),
    traceparent TEXT NULL CHECK(traceparent IS NULL OR length(traceparent) = 55),
    admitted_at TEXT NOT NULL CHECK(length(admitted_at) = 27),
    PRIMARY KEY (tenant_id, workspace_id, provider, channel_id, verification_id),
    UNIQUE (tenant_id, workspace_id, provider, channel_id, verification_id, envelope_digest),
    FOREIGN KEY (tenant_id, workspace_id, provider, channel_id, event_id, event_digest)
        REFERENCES native_im_inbox_events(tenant_id, workspace_id, provider, channel_id, event_id, event_digest)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS native_im_inbound_reads (
    tenant_id TEXT NOT NULL CHECK(length(tenant_id) BETWEEN 1 AND 4096),
    workspace_id TEXT NOT NULL CHECK(length(workspace_id) BETWEEN 1 AND 4096),
    provider TEXT NOT NULL CHECK(length(provider) BETWEEN 1 AND 4096),
    channel_id TEXT NOT NULL CHECK(length(channel_id) BETWEEN 1 AND 4096),
    read_request_id TEXT NOT NULL CHECK(length(read_request_id) BETWEEN 1 AND 4096),
    read_request_digest TEXT NOT NULL CHECK(length(read_request_digest) = 64 AND read_request_digest NOT GLOB '*[^0-9a-f]*'),
    request_json TEXT NOT NULL CHECK(length(CAST(request_json AS BLOB)) BETWEEN 1 AND 1048576),
    base_checkpoint_revision INTEGER NOT NULL CHECK(base_checkpoint_revision >= 0),
    after_cursor TEXT NULL CHECK(after_cursor IS NULL OR length(after_cursor) BETWEEN 1 AND 4096),
    after_sequence INTEGER NULL CHECK(after_sequence IS NULL OR after_sequence >= 0),
    request_snapshot_token TEXT NULL CHECK(request_snapshot_token IS NULL OR length(request_snapshot_token) BETWEEN 1 AND 4096),
    status TEXT NOT NULL CHECK(status IN ('prepared', 'admitted')),
    prepared_at TEXT NOT NULL CHECK(length(prepared_at) = 27),
    page_digest TEXT NULL CHECK(page_digest IS NULL OR (length(page_digest) = 64 AND page_digest NOT GLOB '*[^0-9a-f]*')),
    response_snapshot_token TEXT NULL CHECK(response_snapshot_token IS NULL OR length(response_snapshot_token) BETWEEN 1 AND 4096),
    next_cursor TEXT NULL CHECK(next_cursor IS NULL OR length(next_cursor) BETWEEN 1 AND 4096),
    next_sequence INTEGER NULL CHECK(next_sequence IS NULL OR next_sequence >= 0),
    continuation_snapshot_token TEXT NULL CHECK(continuation_snapshot_token IS NULL OR length(continuation_snapshot_token) BETWEEN 1 AND 4096),
    has_more INTEGER NULL CHECK(has_more IS NULL OR has_more IN (0, 1)),
    envelope_count INTEGER NULL CHECK(envelope_count IS NULL OR envelope_count BETWEEN 0 AND 1000),
    event_manifest_sha256 TEXT NULL CHECK(event_manifest_sha256 IS NULL OR (length(event_manifest_sha256) = 64 AND event_manifest_sha256 NOT GLOB '*[^0-9a-f]*')),
    capability_revision TEXT NULL CHECK(capability_revision IS NULL OR length(capability_revision) BETWEEN 1 AND 4096),
    capability_digest TEXT NULL CHECK(capability_digest IS NULL OR (length(capability_digest) = 64 AND capability_digest NOT GLOB '*[^0-9a-f]*')),
    admitted_checkpoint_revision INTEGER NULL CHECK(admitted_checkpoint_revision IS NULL OR admitted_checkpoint_revision > 0),
    admitted_at TEXT NULL CHECK(admitted_at IS NULL OR length(admitted_at) = 27),
    PRIMARY KEY (tenant_id, workspace_id, provider, channel_id, read_request_digest),
    UNIQUE (tenant_id, workspace_id, provider, channel_id, read_request_id),
    UNIQUE (tenant_id, workspace_id, provider, channel_id, read_request_digest, page_digest),
    CHECK ((after_cursor IS NULL) = (after_sequence IS NULL)),
    CHECK (request_snapshot_token IS NULL OR after_cursor IS NOT NULL),
    CHECK ((next_cursor IS NULL) = (next_sequence IS NULL)),
    CHECK (
        (status = 'prepared'
         AND page_digest IS NULL
         AND response_snapshot_token IS NULL
         AND next_cursor IS NULL
         AND next_sequence IS NULL
         AND continuation_snapshot_token IS NULL
         AND has_more IS NULL
         AND envelope_count IS NULL
         AND event_manifest_sha256 IS NULL
         AND capability_revision IS NULL
         AND capability_digest IS NULL
         AND admitted_checkpoint_revision IS NULL
         AND admitted_at IS NULL)
        OR
        (status = 'admitted'
         AND page_digest IS NOT NULL
         AND response_snapshot_token IS NOT NULL
         AND has_more IS NOT NULL
         AND envelope_count IS NOT NULL
         AND event_manifest_sha256 IS NOT NULL
         AND capability_revision IS NOT NULL
         AND capability_digest IS NOT NULL
         AND admitted_checkpoint_revision = base_checkpoint_revision + 1
         AND admitted_at IS NOT NULL)
    ),
    CHECK (status != 'admitted' OR has_more = 0 OR (continuation_snapshot_token = response_snapshot_token AND next_cursor IS NOT NULL AND envelope_count > 0)),
    CHECK (status != 'admitted' OR has_more = 1 OR continuation_snapshot_token IS NULL),
    CHECK (status != 'admitted' OR envelope_count > 0 OR (next_cursor IS after_cursor AND next_sequence IS after_sequence))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_native_im_inbound_reads_one_prepared
ON native_im_inbound_reads(tenant_id, workspace_id, provider, channel_id)
WHERE status = 'prepared';

CREATE UNIQUE INDEX IF NOT EXISTS idx_native_im_inbound_reads_checkpoint_revision
ON native_im_inbound_reads(tenant_id, workspace_id, provider, channel_id, admitted_checkpoint_revision)
WHERE status = 'admitted';

CREATE TABLE IF NOT EXISTS native_im_inbound_read_events (
    tenant_id TEXT NOT NULL CHECK(length(tenant_id) BETWEEN 1 AND 4096),
    workspace_id TEXT NOT NULL CHECK(length(workspace_id) BETWEEN 1 AND 4096),
    provider TEXT NOT NULL CHECK(length(provider) BETWEEN 1 AND 4096),
    channel_id TEXT NOT NULL CHECK(length(channel_id) BETWEEN 1 AND 4096),
    read_request_digest TEXT NOT NULL CHECK(length(read_request_digest) = 64 AND read_request_digest NOT GLOB '*[^0-9a-f]*'),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0 AND ordinal < 1000),
    event_id TEXT NOT NULL CHECK(length(event_id) BETWEEN 1 AND 4096),
    verification_id TEXT NOT NULL CHECK(length(verification_id) BETWEEN 1 AND 4096),
    envelope_digest TEXT NOT NULL CHECK(length(envelope_digest) = 64 AND envelope_digest NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY (tenant_id, workspace_id, provider, channel_id, read_request_digest, ordinal),
    UNIQUE (tenant_id, workspace_id, provider, channel_id, verification_id),
    UNIQUE (tenant_id, workspace_id, provider, channel_id, event_id),
    FOREIGN KEY (tenant_id, workspace_id, provider, channel_id, read_request_digest)
        REFERENCES native_im_inbound_reads(tenant_id, workspace_id, provider, channel_id, read_request_digest)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, workspace_id, provider, channel_id, verification_id, envelope_digest)
        REFERENCES native_im_inbox_verifications(tenant_id, workspace_id, provider, channel_id, verification_id, envelope_digest)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS native_im_inbound_checkpoints (
    tenant_id TEXT NOT NULL CHECK(length(tenant_id) BETWEEN 1 AND 4096),
    workspace_id TEXT NOT NULL CHECK(length(workspace_id) BETWEEN 1 AND 4096),
    provider TEXT NOT NULL CHECK(length(provider) BETWEEN 1 AND 4096),
    channel_id TEXT NOT NULL CHECK(length(channel_id) BETWEEN 1 AND 4096),
    after_cursor TEXT NULL CHECK(after_cursor IS NULL OR length(after_cursor) BETWEEN 1 AND 4096),
    after_sequence INTEGER NULL CHECK(after_sequence IS NULL OR after_sequence >= 0),
    continuation_snapshot_token TEXT NULL CHECK(continuation_snapshot_token IS NULL OR length(continuation_snapshot_token) BETWEEN 1 AND 4096),
    checkpoint_revision INTEGER NOT NULL CHECK(checkpoint_revision > 0),
    last_read_request_digest TEXT NOT NULL CHECK(length(last_read_request_digest) = 64 AND last_read_request_digest NOT GLOB '*[^0-9a-f]*'),
    last_page_digest TEXT NOT NULL CHECK(length(last_page_digest) = 64 AND last_page_digest NOT GLOB '*[^0-9a-f]*'),
    updated_at TEXT NOT NULL CHECK(length(updated_at) = 27),
    PRIMARY KEY (tenant_id, workspace_id, provider, channel_id),
    CHECK ((after_cursor IS NULL) = (after_sequence IS NULL)),
    CHECK (continuation_snapshot_token IS NULL OR after_cursor IS NOT NULL),
    FOREIGN KEY (tenant_id, workspace_id, provider, channel_id, last_read_request_digest, last_page_digest)
        REFERENCES native_im_inbound_reads(tenant_id, workspace_id, provider, channel_id, read_request_digest, page_digest)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);
