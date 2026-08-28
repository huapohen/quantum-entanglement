CREATE SCHEMA wanwork_im;

CREATE TABLE wanwork_im.provider_realms (
    provider text COLLATE "C" NOT NULL,
    realm_id text COLLATE "C" NOT NULL,
    status text COLLATE "C" NOT NULL,
    revision bigint NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (provider, realm_id),
    CONSTRAINT provider_realms_provider_check
        CHECK (provider IN ('clerk', 'rongcloud')),
    CONSTRAINT provider_realms_realm_id_check
        CHECK (
            octet_length(realm_id) BETWEEN 5 AND 128
            AND realm_id ~ '^rlm_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT provider_realms_status_check
        CHECK (status IN ('active', 'disabled')),
    CONSTRAINT provider_realms_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807)
);

CREATE TABLE wanwork_im.tenants (
    tenant_id text COLLATE "C" NOT NULL,
    status text COLLATE "C" NOT NULL,
    revision bigint NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id),
    CONSTRAINT tenants_tenant_id_check
        CHECK (
            octet_length(tenant_id) BETWEEN 5 AND 128
            AND tenant_id ~ '^ten_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT tenants_status_check
        CHECK (status IN ('active', 'suspended', 'closed')),
    CONSTRAINT tenants_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807)
);

CREATE TABLE wanwork_im.workspaces (
    tenant_id text COLLATE "C" NOT NULL,
    workspace_id text COLLATE "C" NOT NULL,
    status text COLLATE "C" NOT NULL,
    revision bigint NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, workspace_id),
    CONSTRAINT workspaces_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT workspaces_workspace_id_check
        CHECK (
            octet_length(workspace_id) BETWEEN 5 AND 128
            AND workspace_id ~ '^wsp_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT workspaces_status_check
        CHECK (status IN ('active', 'archived', 'closed')),
    CONSTRAINT workspaces_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807)
);

ALTER TABLE wanwork_im.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenants_exact_tenant ON wanwork_im.tenants
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.workspaces FORCE ROW LEVEL SECURITY;
CREATE POLICY workspaces_exact_tenant ON wanwork_im.workspaces
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));
