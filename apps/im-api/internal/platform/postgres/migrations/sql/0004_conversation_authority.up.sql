CREATE TABLE wanwork_im.conversation_membership_heads (
    tenant_id text COLLATE "C" NOT NULL,
    conversation_id text COLLATE "C" NOT NULL,
    actor_id text COLLATE "C" NOT NULL,
    current_revision bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, conversation_id, actor_id),
    CONSTRAINT conversation_membership_heads_conversation_fk
        FOREIGN KEY (tenant_id, conversation_id)
        REFERENCES wanwork_im.conversation_heads (tenant_id, conversation_id)
        ON DELETE RESTRICT,
    CONSTRAINT conversation_membership_heads_actor_fk
        FOREIGN KEY (tenant_id, actor_id)
        REFERENCES wanwork_im.actor_heads (tenant_id, actor_id)
        ON DELETE RESTRICT,
    CONSTRAINT conversation_membership_heads_actor_id_check
        CHECK (
            octet_length(actor_id) BETWEEN 5 AND 128
            AND actor_id ~ '^(usr|agt)_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT conversation_membership_heads_revision_check
        CHECK (current_revision BETWEEN 1 AND 9223372036854775807)
);

CREATE TABLE wanwork_im.conversation_membership_snapshots (
    tenant_id text COLLATE "C" NOT NULL,
    conversation_id text COLLATE "C" NOT NULL,
    actor_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL,
    role text COLLATE "C" NOT NULL,
    status text COLLATE "C" NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, conversation_id, actor_id, revision),
    CONSTRAINT conversation_membership_snapshots_head_fk
        FOREIGN KEY (tenant_id, conversation_id, actor_id)
        REFERENCES wanwork_im.conversation_membership_heads (
            tenant_id,
            conversation_id,
            actor_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT conversation_membership_snapshots_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT conversation_membership_snapshots_role_check
        CHECK (role IN ('owner', 'manager', 'member')),
    CONSTRAINT conversation_membership_snapshots_status_check
        CHECK (status IN ('active', 'removed'))
);

ALTER TABLE wanwork_im.conversation_membership_heads
    ADD CONSTRAINT conversation_membership_heads_current_snapshot_fk
    FOREIGN KEY (tenant_id, conversation_id, actor_id, current_revision)
    REFERENCES wanwork_im.conversation_membership_snapshots (
        tenant_id,
        conversation_id,
        actor_id,
        revision
    )
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE wanwork_im.conversation_access_heads (
    tenant_id text COLLATE "C" NOT NULL,
    conversation_id text COLLATE "C" NOT NULL,
    actor_id text COLLATE "C" NOT NULL,
    current_revision bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, conversation_id, actor_id),
    CONSTRAINT conversation_access_heads_membership_fk
        FOREIGN KEY (tenant_id, conversation_id, actor_id)
        REFERENCES wanwork_im.conversation_membership_heads (
            tenant_id,
            conversation_id,
            actor_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT conversation_access_heads_revision_check
        CHECK (current_revision BETWEEN 1 AND 9223372036854775807)
);

CREATE TABLE wanwork_im.conversation_access_snapshots (
    tenant_id text COLLATE "C" NOT NULL,
    conversation_id text COLLATE "C" NOT NULL,
    actor_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL,
    can_read boolean NOT NULL,
    can_send_message boolean NOT NULL,
    can_manage_members boolean NOT NULL,
    can_manage_conversation boolean NOT NULL,
    can_invoke_agent boolean NOT NULL,
    can_publish_artifact_reference boolean NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, conversation_id, actor_id, revision),
    CONSTRAINT conversation_access_snapshots_head_fk
        FOREIGN KEY (tenant_id, conversation_id, actor_id)
        REFERENCES wanwork_im.conversation_access_heads (
            tenant_id,
            conversation_id,
            actor_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT conversation_access_snapshots_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807)
);

ALTER TABLE wanwork_im.conversation_access_heads
    ADD CONSTRAINT conversation_access_heads_current_snapshot_fk
    FOREIGN KEY (tenant_id, conversation_id, actor_id, current_revision)
    REFERENCES wanwork_im.conversation_access_snapshots (
        tenant_id,
        conversation_id,
        actor_id,
        revision
    )
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE wanwork_im.tenant_command_receipts (
    tenant_id text COLLATE "C" NOT NULL,
    command_kind text COLLATE "C" NOT NULL,
    idempotency_key text COLLATE "C" NOT NULL,
    request_sha256 text COLLATE "C" NOT NULL,
    result_sha256 text COLLATE "C" NOT NULL,
    committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, command_kind, idempotency_key),
    CONSTRAINT tenant_command_receipts_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT tenant_command_receipts_kind_check
        CHECK (
            octet_length(command_kind) BETWEEN 1 AND 64
            AND command_kind ~ '^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$'
        ),
    CONSTRAINT tenant_command_receipts_key_check
        CHECK (
            octet_length(idempotency_key) BETWEEN 1 AND 128
            AND idempotency_key ~ '^[A-Za-z0-9](?:[A-Za-z0-9_.:-]{0,126}[A-Za-z0-9])?$'
        ),
    CONSTRAINT tenant_command_receipts_request_sha256_check
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT tenant_command_receipts_result_sha256_check
        CHECK (result_sha256 ~ '^[0-9a-f]{64}$')
);

ALTER TABLE wanwork_im.conversation_membership_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.conversation_membership_heads FORCE ROW LEVEL SECURITY;
CREATE POLICY conversation_membership_heads_exact_tenant
    ON wanwork_im.conversation_membership_heads
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.conversation_membership_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.conversation_membership_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY conversation_membership_snapshots_exact_tenant
    ON wanwork_im.conversation_membership_snapshots
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.conversation_access_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.conversation_access_heads FORCE ROW LEVEL SECURITY;
CREATE POLICY conversation_access_heads_exact_tenant
    ON wanwork_im.conversation_access_heads
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.conversation_access_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.conversation_access_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY conversation_access_snapshots_exact_tenant
    ON wanwork_im.conversation_access_snapshots
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.tenant_command_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.tenant_command_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_command_receipts_exact_tenant
    ON wanwork_im.tenant_command_receipts
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));
