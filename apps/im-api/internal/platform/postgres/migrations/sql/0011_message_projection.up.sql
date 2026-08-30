CREATE TABLE wanwork_im.message_projection_heads (
    tenant_id text COLLATE "C" NOT NULL,
    workspace_id text COLLATE "C" NOT NULL,
    conversation_id text COLLATE "C" NOT NULL,
    projection_id text COLLATE "C" NOT NULL,
    current_sequence bigint NOT NULL DEFAULT '0'::bigint,
    current_global_position bigint NOT NULL DEFAULT '0'::bigint,
    current_revision bigint NOT NULL DEFAULT '0'::bigint,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, workspace_id, conversation_id, projection_id),
    CONSTRAINT message_projection_heads_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT message_projection_heads_workspace_check
        CHECK (
            workspace_id = ''
            OR (
                octet_length(workspace_id) BETWEEN 5 AND 128
                AND workspace_id ~ '^wsp_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
            )
        ),
    CONSTRAINT message_projection_heads_conversation_check
        CHECK (octet_length(conversation_id) BETWEEN 1 AND 256),
    CONSTRAINT message_projection_heads_projection_id_check
        CHECK (octet_length(projection_id) BETWEEN 1 AND 256),
    CONSTRAINT message_projection_heads_sequence_check
        CHECK (current_sequence BETWEEN 0 AND 9223372036854775807),
    CONSTRAINT message_projection_heads_position_check
        CHECK (current_global_position BETWEEN 0 AND 9223372036854775807),
    CONSTRAINT message_projection_heads_revision_check
        CHECK (current_revision BETWEEN 0 AND 9223372036854775807),
    CONSTRAINT message_projection_heads_progress_shape_check
        CHECK (
            (current_sequence = 0 AND current_global_position = 0 AND current_revision = 0)
            OR (current_sequence > 0 AND current_global_position > 0 AND current_revision > 0)
        )
);

CREATE TABLE wanwork_im.message_snapshots (
    tenant_id text COLLATE "C" NOT NULL,
    workspace_id text COLLATE "C" NOT NULL,
    conversation_id text COLLATE "C" NOT NULL,
    message_id text COLLATE "C" NOT NULL,
    client_message_id text COLLATE "C" NOT NULL,
    sender_actor_id text COLLATE "C" NOT NULL,
    message_type text COLLATE "C" NOT NULL,
    status text COLLATE "C" NOT NULL,
    text text NOT NULL,
    ext_info text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL,
    revision bigint NOT NULL,
    last_event_sequence bigint NOT NULL,
    last_event_position bigint NOT NULL,
    projection_revision bigint NOT NULL,
    PRIMARY KEY (tenant_id, conversation_id, message_id),
    CONSTRAINT message_snapshots_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT message_snapshots_workspace_check
        CHECK (
            workspace_id = ''
            OR (
                octet_length(workspace_id) BETWEEN 5 AND 128
                AND workspace_id ~ '^wsp_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
            )
        ),
    CONSTRAINT message_snapshots_conversation_check
        CHECK (octet_length(conversation_id) BETWEEN 1 AND 256),
    CONSTRAINT message_snapshots_message_id_check
        CHECK (octet_length(message_id) BETWEEN 1 AND 256),
    CONSTRAINT message_snapshots_client_message_id_check
        CHECK (octet_length(client_message_id) BETWEEN 1 AND 256),
    CONSTRAINT message_snapshots_actor_id_check
        CHECK (octet_length(sender_actor_id) BETWEEN 1 AND 256),
    CONSTRAINT message_snapshots_type_check
        CHECK (message_type IN ('text', 'system')),
    CONSTRAINT message_snapshots_status_check
        CHECK (status IN ('active', 'edited', 'recalled')),
    CONSTRAINT message_snapshots_text_check
        CHECK (octet_length(text) <= 65536),
    CONSTRAINT message_snapshots_ext_info_check
        CHECK (octet_length(ext_info) <= 65536),
    CONSTRAINT message_snapshots_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT message_snapshots_sequence_check
        CHECK (last_event_sequence BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT message_snapshots_position_check
        CHECK (last_event_position BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT message_snapshots_projection_revision_check
        CHECK (projection_revision BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT message_snapshots_recalled_text_check
        CHECK (status <> 'recalled' OR text = '')
);

CREATE UNIQUE INDEX message_snapshots_scope_client_message_id_uk
    ON wanwork_im.message_snapshots (tenant_id, workspace_id, conversation_id, client_message_id);
CREATE INDEX message_snapshots_scope_created_id_idx
    ON wanwork_im.message_snapshots (tenant_id, workspace_id, conversation_id, created_at, message_id);
CREATE INDEX message_snapshots_scope_sequence_idx
    ON wanwork_im.message_snapshots (tenant_id, workspace_id, conversation_id, last_event_sequence);

ALTER TABLE wanwork_im.message_projection_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.message_projection_heads FORCE ROW LEVEL SECURITY;
CREATE POLICY message_projection_heads_exact_tenant
    ON wanwork_im.message_projection_heads
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.message_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.message_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY message_snapshots_exact_tenant
    ON wanwork_im.message_snapshots
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));
