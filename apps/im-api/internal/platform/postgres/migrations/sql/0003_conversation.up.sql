CREATE TABLE wanwork_im.conversation_heads (
    tenant_id text COLLATE "C" NOT NULL,
    conversation_id text COLLATE "C" NOT NULL,
    conversation_type text COLLATE "C" NOT NULL,
    current_revision bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, conversation_id),
    CONSTRAINT conversation_heads_type_uk
        UNIQUE (tenant_id, conversation_id, conversation_type),
    CONSTRAINT conversation_heads_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT conversation_heads_conversation_id_check
        CHECK (
            octet_length(conversation_id) BETWEEN 5 AND 128
            AND conversation_id ~ '^cnv_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT conversation_heads_type_check
        CHECK (conversation_type IN ('direct', 'group')),
    CONSTRAINT conversation_heads_revision_check
        CHECK (current_revision BETWEEN 1 AND 9223372036854775807)
);

CREATE TABLE wanwork_im.conversation_snapshots (
    tenant_id text COLLATE "C" NOT NULL,
    conversation_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL,
    workspace_id text COLLATE "C",
    conversation_type text COLLATE "C" NOT NULL,
    status text COLLATE "C" NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, conversation_id, revision),
    CONSTRAINT conversation_snapshots_current_uk
        UNIQUE (tenant_id, conversation_id, conversation_type, revision),
    CONSTRAINT conversation_snapshots_head_fk
        FOREIGN KEY (tenant_id, conversation_id, conversation_type)
        REFERENCES wanwork_im.conversation_heads (
            tenant_id,
            conversation_id,
            conversation_type
        )
        ON DELETE RESTRICT,
    CONSTRAINT conversation_snapshots_workspace_fk
        FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES wanwork_im.workspaces (tenant_id, workspace_id)
        ON DELETE RESTRICT,
    CONSTRAINT conversation_snapshots_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT conversation_snapshots_type_check
        CHECK (conversation_type IN ('direct', 'group')),
    CONSTRAINT conversation_snapshots_status_check
        CHECK (status IN ('active', 'archived', 'closed'))
);

ALTER TABLE wanwork_im.conversation_heads
    ADD CONSTRAINT conversation_heads_current_snapshot_fk
    FOREIGN KEY (
        tenant_id,
        conversation_id,
        conversation_type,
        current_revision
    )
    REFERENCES wanwork_im.conversation_snapshots (
        tenant_id,
        conversation_id,
        conversation_type,
        revision
    )
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE wanwork_im.provider_conversation_binding_heads (
    tenant_id text COLLATE "C" NOT NULL,
    provider text COLLATE "C" NOT NULL,
    realm_id text COLLATE "C" NOT NULL,
    provider_conversation_id text COLLATE "C" NOT NULL,
    current_revision bigint NOT NULL,
    current_conversation_id text COLLATE "C" NOT NULL,
    current_conversation_type text COLLATE "C" NOT NULL,
    current_status text COLLATE "C" NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, provider, realm_id, provider_conversation_id),
    CONSTRAINT provider_conversation_heads_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT provider_conversation_heads_realm_fk
        FOREIGN KEY (provider, realm_id)
        REFERENCES wanwork_im.provider_realms (provider, realm_id)
        ON DELETE RESTRICT,
    CONSTRAINT provider_conversation_heads_conversation_fk
        FOREIGN KEY (
            tenant_id,
            current_conversation_id,
            current_conversation_type
        )
        REFERENCES wanwork_im.conversation_heads (
            tenant_id,
            conversation_id,
            conversation_type
        )
        ON DELETE RESTRICT,
    CONSTRAINT provider_conversation_heads_provider_check
        CHECK (provider = 'rongcloud'),
    CONSTRAINT provider_conversation_heads_subject_id_check
        CHECK (
            octet_length(provider_conversation_id) BETWEEN 5 AND 128
            AND provider_conversation_id ~ '^cnv_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT provider_conversation_heads_target_check
        CHECK (provider_conversation_id = current_conversation_id),
    CONSTRAINT provider_conversation_heads_type_check
        CHECK (current_conversation_type = 'group'),
    CONSTRAINT provider_conversation_heads_revision_check
        CHECK (current_revision BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT provider_conversation_heads_status_check
        CHECK (current_status IN ('active', 'revoked'))
);

CREATE UNIQUE INDEX provider_conversation_heads_active_subject_uk
    ON wanwork_im.provider_conversation_binding_heads (
        provider,
        realm_id,
        provider_conversation_id
    )
    WHERE current_status = 'active';

CREATE TABLE wanwork_im.provider_conversation_binding_snapshots (
    tenant_id text COLLATE "C" NOT NULL,
    provider text COLLATE "C" NOT NULL,
    realm_id text COLLATE "C" NOT NULL,
    provider_conversation_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL,
    conversation_id text COLLATE "C" NOT NULL,
    conversation_type text COLLATE "C" NOT NULL,
    status text COLLATE "C" NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (
        tenant_id,
        provider,
        realm_id,
        provider_conversation_id,
        revision
    ),
    CONSTRAINT provider_conversation_snapshots_current_uk
        UNIQUE (
            tenant_id,
            provider,
            realm_id,
            provider_conversation_id,
            revision,
            conversation_id,
            conversation_type,
            status
        ),
    CONSTRAINT provider_conversation_snapshots_head_fk
        FOREIGN KEY (tenant_id, provider, realm_id, provider_conversation_id)
        REFERENCES wanwork_im.provider_conversation_binding_heads (
            tenant_id,
            provider,
            realm_id,
            provider_conversation_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT provider_conversation_snapshots_conversation_fk
        FOREIGN KEY (tenant_id, conversation_id, conversation_type)
        REFERENCES wanwork_im.conversation_heads (
            tenant_id,
            conversation_id,
            conversation_type
        )
        ON DELETE RESTRICT,
    CONSTRAINT provider_conversation_snapshots_provider_check
        CHECK (provider = 'rongcloud'),
    CONSTRAINT provider_conversation_snapshots_target_check
        CHECK (provider_conversation_id = conversation_id),
    CONSTRAINT provider_conversation_snapshots_type_check
        CHECK (conversation_type = 'group'),
    CONSTRAINT provider_conversation_snapshots_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT provider_conversation_snapshots_status_check
        CHECK (status IN ('active', 'revoked'))
);

ALTER TABLE wanwork_im.provider_conversation_binding_heads
    ADD CONSTRAINT provider_conversation_heads_current_snapshot_fk
    FOREIGN KEY (
        tenant_id,
        provider,
        realm_id,
        provider_conversation_id,
        current_revision,
        current_conversation_id,
        current_conversation_type,
        current_status
    )
    REFERENCES wanwork_im.provider_conversation_binding_snapshots (
        tenant_id,
        provider,
        realm_id,
        provider_conversation_id,
        revision,
        conversation_id,
        conversation_type,
        status
    )
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE wanwork_im.conversation_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.conversation_heads FORCE ROW LEVEL SECURITY;
CREATE POLICY conversation_heads_exact_tenant ON wanwork_im.conversation_heads
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.conversation_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.conversation_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY conversation_snapshots_exact_tenant ON wanwork_im.conversation_snapshots
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.provider_conversation_binding_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.provider_conversation_binding_heads FORCE ROW LEVEL SECURITY;
CREATE POLICY provider_conversation_heads_exact_tenant
    ON wanwork_im.provider_conversation_binding_heads
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.provider_conversation_binding_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.provider_conversation_binding_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY provider_conversation_snapshots_exact_tenant
    ON wanwork_im.provider_conversation_binding_snapshots
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));
