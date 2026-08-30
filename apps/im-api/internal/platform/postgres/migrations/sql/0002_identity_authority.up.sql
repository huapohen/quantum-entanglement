CREATE TABLE wanwork_im.human_principal_heads (
    principal_id text COLLATE "C" NOT NULL,
    current_revision bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (principal_id),
    CONSTRAINT human_principal_heads_principal_id_check
        CHECK (
            octet_length(principal_id) BETWEEN 5 AND 128
            AND principal_id ~ '^hpr_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT human_principal_heads_revision_check
        CHECK (current_revision BETWEEN 1 AND 9223372036854775807)
);

CREATE TABLE wanwork_im.human_principal_snapshots (
    principal_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL,
    status text COLLATE "C" NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (principal_id, revision),
    CONSTRAINT human_principal_snapshots_head_fk
        FOREIGN KEY (principal_id)
        REFERENCES wanwork_im.human_principal_heads (principal_id)
        ON DELETE RESTRICT,
    CONSTRAINT human_principal_snapshots_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT human_principal_snapshots_status_check
        CHECK (status IN ('active', 'suspended', 'closed'))
);

ALTER TABLE wanwork_im.human_principal_heads
    ADD CONSTRAINT human_principal_heads_current_snapshot_fk
    FOREIGN KEY (principal_id, current_revision)
    REFERENCES wanwork_im.human_principal_snapshots (principal_id, revision)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE wanwork_im.human_identity_binding_heads (
    provider text COLLATE "C" NOT NULL,
    realm_id text COLLATE "C" NOT NULL,
    subject_id text COLLATE "C" NOT NULL,
    current_revision bigint NOT NULL,
    current_principal_id text COLLATE "C" NOT NULL,
    current_status text COLLATE "C" NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (provider, realm_id, subject_id),
    CONSTRAINT human_identity_heads_realm_fk
        FOREIGN KEY (provider, realm_id)
        REFERENCES wanwork_im.provider_realms (provider, realm_id)
        ON DELETE RESTRICT,
    CONSTRAINT human_identity_heads_principal_fk
        FOREIGN KEY (current_principal_id)
        REFERENCES wanwork_im.human_principal_heads (principal_id)
        ON DELETE RESTRICT,
    CONSTRAINT human_identity_heads_provider_check
        CHECK (provider = 'clerk'),
    CONSTRAINT human_identity_heads_subject_id_check
        CHECK (
            octet_length(subject_id) BETWEEN 6 AND 129
            AND subject_id ~ '^user_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT human_identity_heads_revision_check
        CHECK (current_revision BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT human_identity_heads_status_check
        CHECK (current_status IN ('active', 'revoked'))
);

CREATE UNIQUE INDEX human_identity_heads_active_target_uk
    ON wanwork_im.human_identity_binding_heads (
        provider,
        realm_id,
        current_principal_id
    )
    WHERE current_status = 'active';

CREATE TABLE wanwork_im.human_identity_binding_snapshots (
    provider text COLLATE "C" NOT NULL,
    realm_id text COLLATE "C" NOT NULL,
    subject_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL,
    principal_id text COLLATE "C" NOT NULL,
    status text COLLATE "C" NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (provider, realm_id, subject_id, revision),
    CONSTRAINT human_identity_snapshots_current_uk
        UNIQUE (provider, realm_id, subject_id, revision, principal_id, status),
    CONSTRAINT human_identity_snapshots_head_fk
        FOREIGN KEY (provider, realm_id, subject_id)
        REFERENCES wanwork_im.human_identity_binding_heads (provider, realm_id, subject_id)
        ON DELETE RESTRICT,
    CONSTRAINT human_identity_snapshots_principal_fk
        FOREIGN KEY (principal_id)
        REFERENCES wanwork_im.human_principal_heads (principal_id)
        ON DELETE RESTRICT,
    CONSTRAINT human_identity_snapshots_provider_check
        CHECK (provider = 'clerk'),
    CONSTRAINT human_identity_snapshots_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT human_identity_snapshots_status_check
        CHECK (status IN ('active', 'revoked'))
);

ALTER TABLE wanwork_im.human_identity_binding_heads
    ADD CONSTRAINT human_identity_heads_current_snapshot_fk
    FOREIGN KEY (
        provider,
        realm_id,
        subject_id,
        current_revision,
        current_principal_id,
        current_status
    )
    REFERENCES wanwork_im.human_identity_binding_snapshots (
        provider,
        realm_id,
        subject_id,
        revision,
        principal_id,
        status
    )
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE wanwork_im.actor_heads (
    tenant_id text COLLATE "C" NOT NULL,
    actor_id text COLLATE "C" NOT NULL,
    current_revision bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, actor_id),
    CONSTRAINT actor_heads_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT actor_heads_actor_id_check
        CHECK (
            octet_length(actor_id) BETWEEN 5 AND 128
            AND actor_id ~ '^(usr|agt|sys|svc)_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT actor_heads_revision_check
        CHECK (current_revision BETWEEN 1 AND 9223372036854775807)
);

CREATE TABLE wanwork_im.actor_snapshots (
    tenant_id text COLLATE "C" NOT NULL,
    actor_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL,
    subject_type text COLLATE "C" NOT NULL,
    status text COLLATE "C" NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, actor_id, revision),
    CONSTRAINT actor_snapshots_head_fk
        FOREIGN KEY (tenant_id, actor_id)
        REFERENCES wanwork_im.actor_heads (tenant_id, actor_id)
        ON DELETE RESTRICT,
    CONSTRAINT actor_snapshots_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT actor_snapshots_type_check
        CHECK (
            (subject_type = 'human' AND actor_id ~ '^usr_')
            OR (subject_type = 'agent' AND actor_id ~ '^agt_')
            OR (subject_type = 'system' AND actor_id ~ '^sys_')
            OR (subject_type = 'service' AND actor_id ~ '^svc_')
        ),
    CONSTRAINT actor_snapshots_status_check
        CHECK (status IN ('active', 'suspended', 'removed'))
);

ALTER TABLE wanwork_im.actor_heads
    ADD CONSTRAINT actor_heads_current_snapshot_fk
    FOREIGN KEY (tenant_id, actor_id, current_revision)
    REFERENCES wanwork_im.actor_snapshots (tenant_id, actor_id, revision)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE wanwork_im.tenant_membership_heads (
    tenant_id text COLLATE "C" NOT NULL,
    principal_id text COLLATE "C" NOT NULL,
    actor_id text COLLATE "C" NOT NULL,
    current_revision bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, principal_id),
    CONSTRAINT tenant_membership_heads_actor_uk
        UNIQUE (tenant_id, actor_id),
    CONSTRAINT tenant_membership_heads_current_uk
        UNIQUE (tenant_id, principal_id, actor_id),
    CONSTRAINT tenant_membership_heads_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT tenant_membership_heads_principal_fk
        FOREIGN KEY (principal_id)
        REFERENCES wanwork_im.human_principal_heads (principal_id)
        ON DELETE RESTRICT,
    CONSTRAINT tenant_membership_heads_actor_fk
        FOREIGN KEY (tenant_id, actor_id)
        REFERENCES wanwork_im.actor_heads (tenant_id, actor_id)
        ON DELETE RESTRICT,
    CONSTRAINT tenant_membership_heads_actor_id_check
        CHECK (
            octet_length(actor_id) BETWEEN 5 AND 128
            AND actor_id ~ '^usr_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT tenant_membership_heads_revision_check
        CHECK (current_revision BETWEEN 1 AND 9223372036854775807)
);

CREATE TABLE wanwork_im.tenant_membership_snapshots (
    tenant_id text COLLATE "C" NOT NULL,
    principal_id text COLLATE "C" NOT NULL,
    actor_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL,
    role text COLLATE "C" NOT NULL,
    status text COLLATE "C" NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, principal_id, revision),
    CONSTRAINT tenant_membership_snapshots_head_actor_fk
        FOREIGN KEY (tenant_id, principal_id, actor_id)
        REFERENCES wanwork_im.tenant_membership_heads (tenant_id, principal_id, actor_id)
        ON DELETE RESTRICT,
    CONSTRAINT tenant_membership_snapshots_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT tenant_membership_snapshots_role_check
        CHECK (role IN ('owner', 'admin', 'member', 'guest')),
    CONSTRAINT tenant_membership_snapshots_status_check
        CHECK (status IN ('active', 'suspended', 'removed'))
);

ALTER TABLE wanwork_im.tenant_membership_heads
    ADD CONSTRAINT tenant_membership_heads_current_snapshot_fk
    FOREIGN KEY (tenant_id, principal_id, current_revision)
    REFERENCES wanwork_im.tenant_membership_snapshots (tenant_id, principal_id, revision)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE wanwork_im.provider_actor_binding_heads (
    tenant_id text COLLATE "C" NOT NULL,
    provider text COLLATE "C" NOT NULL,
    realm_id text COLLATE "C" NOT NULL,
    provider_user_id text COLLATE "C" NOT NULL,
    current_revision bigint NOT NULL,
    current_actor_id text COLLATE "C" NOT NULL,
    current_status text COLLATE "C" NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, provider, realm_id, provider_user_id),
    CONSTRAINT provider_actor_heads_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT provider_actor_heads_realm_fk
        FOREIGN KEY (provider, realm_id)
        REFERENCES wanwork_im.provider_realms (provider, realm_id)
        ON DELETE RESTRICT,
    CONSTRAINT provider_actor_heads_actor_fk
        FOREIGN KEY (tenant_id, current_actor_id)
        REFERENCES wanwork_im.actor_heads (tenant_id, actor_id)
        ON DELETE RESTRICT,
    CONSTRAINT provider_actor_heads_provider_check
        CHECK (provider = 'rongcloud'),
    CONSTRAINT provider_actor_heads_user_id_check
        CHECK (
            octet_length(provider_user_id) BETWEEN 5 AND 128
            AND provider_user_id ~ '^(usr|agt)_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT provider_actor_heads_target_check
        CHECK (provider_user_id = current_actor_id),
    CONSTRAINT provider_actor_heads_revision_check
        CHECK (current_revision BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT provider_actor_heads_status_check
        CHECK (current_status IN ('active', 'revoked'))
);

CREATE UNIQUE INDEX provider_actor_heads_active_target_uk
    ON wanwork_im.provider_actor_binding_heads (
        tenant_id,
        provider,
        realm_id,
        current_actor_id
    )
    WHERE current_status = 'active';

CREATE TABLE wanwork_im.provider_actor_binding_snapshots (
    tenant_id text COLLATE "C" NOT NULL,
    provider text COLLATE "C" NOT NULL,
    realm_id text COLLATE "C" NOT NULL,
    provider_user_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL,
    actor_id text COLLATE "C" NOT NULL,
    status text COLLATE "C" NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, provider, realm_id, provider_user_id, revision),
    CONSTRAINT provider_actor_snapshots_current_uk
        UNIQUE (
            tenant_id,
            provider,
            realm_id,
            provider_user_id,
            revision,
            actor_id,
            status
        ),
    CONSTRAINT provider_actor_snapshots_head_fk
        FOREIGN KEY (tenant_id, provider, realm_id, provider_user_id)
        REFERENCES wanwork_im.provider_actor_binding_heads (
            tenant_id,
            provider,
            realm_id,
            provider_user_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT provider_actor_snapshots_actor_fk
        FOREIGN KEY (tenant_id, actor_id)
        REFERENCES wanwork_im.actor_heads (tenant_id, actor_id)
        ON DELETE RESTRICT,
    CONSTRAINT provider_actor_snapshots_provider_check
        CHECK (provider = 'rongcloud'),
    CONSTRAINT provider_actor_snapshots_target_check
        CHECK (provider_user_id = actor_id),
    CONSTRAINT provider_actor_snapshots_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT provider_actor_snapshots_status_check
        CHECK (status IN ('active', 'revoked'))
);

ALTER TABLE wanwork_im.provider_actor_binding_heads
    ADD CONSTRAINT provider_actor_heads_current_snapshot_fk
    FOREIGN KEY (
        tenant_id,
        provider,
        realm_id,
        provider_user_id,
        current_revision,
        current_actor_id,
        current_status
    )
    REFERENCES wanwork_im.provider_actor_binding_snapshots (
        tenant_id,
        provider,
        realm_id,
        provider_user_id,
        revision,
        actor_id,
        status
    )
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE wanwork_im.actor_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.actor_heads FORCE ROW LEVEL SECURITY;
CREATE POLICY actor_heads_exact_tenant ON wanwork_im.actor_heads
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.actor_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.actor_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY actor_snapshots_exact_tenant ON wanwork_im.actor_snapshots
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.tenant_membership_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.tenant_membership_heads FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_membership_heads_exact_tenant ON wanwork_im.tenant_membership_heads
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.tenant_membership_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.tenant_membership_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_membership_snapshots_exact_tenant ON wanwork_im.tenant_membership_snapshots
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.provider_actor_binding_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.provider_actor_binding_heads FORCE ROW LEVEL SECURITY;
CREATE POLICY provider_actor_heads_exact_tenant ON wanwork_im.provider_actor_binding_heads
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.provider_actor_binding_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.provider_actor_binding_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY provider_actor_snapshots_exact_tenant ON wanwork_im.provider_actor_binding_snapshots
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));
