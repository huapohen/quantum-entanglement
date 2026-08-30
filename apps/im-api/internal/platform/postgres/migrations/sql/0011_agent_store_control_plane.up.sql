CREATE TABLE wanwork_im.agent_definitions (
    tenant_id text COLLATE "C" NOT NULL,
    definition_id text COLLATE "C" NOT NULL,
    claimed_by text COLLATE "C" NOT NULL,
    publisher_id text COLLATE "C" NOT NULL,
    display_name text COLLATE "C" NOT NULL,
    summary text COLLATE "C" NOT NULL,
    status text COLLATE "C" NOT NULL,
    revision bigint NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (definition_id),
    CONSTRAINT agent_definitions_tenant_definition_uk
        UNIQUE (tenant_id, definition_id),
    CONSTRAINT agent_definitions_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_definitions_claimed_by_fk
        FOREIGN KEY (claimed_by)
        REFERENCES wanwork_im.human_principal_heads (principal_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_definitions_definition_id_check
        CHECK (
            octet_length(definition_id) BETWEEN 5 AND 128
            AND definition_id ~ '^agd_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT agent_definitions_claimed_by_check
        CHECK (
            octet_length(claimed_by) BETWEEN 5 AND 128
            AND claimed_by ~ '^hpr_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT agent_definitions_publisher_id_check
        CHECK (
            octet_length(publisher_id) BETWEEN 5 AND 128
            AND publisher_id ~ '^pub_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT agent_definitions_display_name_check
        CHECK (octet_length(display_name) BETWEEN 1 AND 128 AND display_name !~ '[[:cntrl:]]'),
    CONSTRAINT agent_definitions_summary_check
        CHECK (octet_length(summary) BETWEEN 1 AND 2048 AND summary !~ '[[:cntrl:]]'),
    CONSTRAINT agent_definitions_status_check
        CHECK (status IN ('draft', 'active', 'revoked')),
    CONSTRAINT agent_definitions_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807)
);

CREATE TABLE wanwork_im.agent_releases (
    tenant_id text COLLATE "C" NOT NULL,
    release_id text COLLATE "C" NOT NULL,
    definition_id text COLLATE "C" NOT NULL,
    version text COLLATE "C" NOT NULL,
    artifact_digest text COLLATE "C" NOT NULL,
    manifest_digest text COLLATE "C" NOT NULL,
    persona_digest text COLLATE "C" NOT NULL,
    requested_capabilities jsonb NOT NULL,
    prohibitions jsonb NOT NULL,
    data_routes jsonb NOT NULL,
    isolation text COLLATE "C" NOT NULL,
    status text COLLATE "C" NOT NULL,
    published_at timestamptz,
    revision bigint NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (release_id),
    CONSTRAINT agent_releases_tenant_definition_uk
        UNIQUE (tenant_id, release_id, definition_id),
    CONSTRAINT agent_releases_definition_fk
        FOREIGN KEY (tenant_id, definition_id)
        REFERENCES wanwork_im.agent_definitions (tenant_id, definition_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_releases_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_releases_release_id_check
        CHECK (
            octet_length(release_id) BETWEEN 5 AND 128
            AND release_id ~ '^agr_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT agent_releases_version_check
        CHECK (version ~ '^[0-9]+\.[0-9]+\.[0-9]+([+-][0-9A-Za-z.-]+)?$'),
    CONSTRAINT agent_releases_artifact_digest_check
        CHECK (artifact_digest ~ '^sha256:[0-9a-f]{64}$' AND artifact_digest <> 'sha256:' || repeat('0', 64)),
    CONSTRAINT agent_releases_manifest_digest_check
        CHECK (manifest_digest ~ '^sha256:[0-9a-f]{64}$' AND manifest_digest <> 'sha256:' || repeat('0', 64)),
    CONSTRAINT agent_releases_persona_digest_check
        CHECK (persona_digest ~ '^sha256:[0-9a-f]{64}$' AND persona_digest <> 'sha256:' || repeat('0', 64)),
    CONSTRAINT agent_releases_requested_capabilities_check
        CHECK (jsonb_typeof(requested_capabilities) = 'array' AND jsonb_array_length(requested_capabilities) BETWEEN 1 AND 128),
    CONSTRAINT agent_releases_prohibitions_check
        CHECK (jsonb_typeof(prohibitions) = 'array' AND jsonb_array_length(prohibitions) BETWEEN 0 AND 128),
    CONSTRAINT agent_releases_data_routes_check
        CHECK (jsonb_typeof(data_routes) = 'array' AND jsonb_array_length(data_routes) BETWEEN 1 AND 128),
    CONSTRAINT agent_releases_isolation_check
        CHECK (isolation IN ('process', 'container', 'microvm')),
    CONSTRAINT agent_releases_status_check
        CHECK (status IN ('draft', 'published', 'quarantined', 'revoked')),
    CONSTRAINT agent_releases_published_at_check
        CHECK ((status = 'draft' AND published_at IS NULL) OR (status <> 'draft' AND published_at IS NOT NULL)),
    CONSTRAINT agent_releases_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807)
);

CREATE TABLE wanwork_im.agent_passports (
    tenant_id text COLLATE "C" NOT NULL,
    release_id text COLLATE "C" NOT NULL,
    definition_id text COLLATE "C" NOT NULL,
    status text COLLATE "C" NOT NULL,
    attestations jsonb NOT NULL,
    revision bigint NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (release_id),
    CONSTRAINT agent_passports_release_fk
        FOREIGN KEY (tenant_id, release_id, definition_id)
        REFERENCES wanwork_im.agent_releases (tenant_id, release_id, definition_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_passports_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_passports_status_check
        CHECK (status IN ('active', 'quarantined', 'revoked')),
    CONSTRAINT agent_passports_attestations_check
        CHECK (jsonb_typeof(attestations) = 'array' AND jsonb_array_length(attestations) BETWEEN 3 AND 128),
    CONSTRAINT agent_passports_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807)
);

CREATE TABLE wanwork_im.agent_installation_heads (
    tenant_id text COLLATE "C" NOT NULL,
    installation_id text COLLATE "C" NOT NULL,
    current_revision bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, installation_id),
    CONSTRAINT agent_installation_heads_installation_id_check
        CHECK (
            octet_length(installation_id) BETWEEN 5 AND 128
            AND installation_id ~ '^ins_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT agent_installation_heads_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_installation_heads_revision_check
        CHECK (current_revision BETWEEN 1 AND 9223372036854775807)
);

CREATE TABLE wanwork_im.agent_installation_snapshots (
    tenant_id text COLLATE "C" NOT NULL,
    installation_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL,
    workspace_id text COLLATE "C" NOT NULL,
    definition_id text COLLATE "C" NOT NULL,
    release_id text COLLATE "C" NOT NULL,
    version text COLLATE "C" NOT NULL,
    agent_actor_id text COLLATE "C" NOT NULL,
    installed_by text COLLATE "C" NOT NULL,
    granted_capabilities jsonb NOT NULL,
    bound_data_routes jsonb NOT NULL,
    status text COLLATE "C" NOT NULL,
    created_at timestamptz NOT NULL,
    disabled_at timestamptz,
    PRIMARY KEY (tenant_id, installation_id, revision),
    CONSTRAINT agent_installation_snapshots_head_fk
        FOREIGN KEY (tenant_id, installation_id)
        REFERENCES wanwork_im.agent_installation_heads (tenant_id, installation_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_installation_snapshots_workspace_fk
        FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES wanwork_im.workspaces (tenant_id, workspace_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_installation_snapshots_definition_fk
        FOREIGN KEY (tenant_id, definition_id)
        REFERENCES wanwork_im.agent_definitions (tenant_id, definition_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_installation_snapshots_release_fk
        FOREIGN KEY (tenant_id, release_id, definition_id)
        REFERENCES wanwork_im.agent_releases (tenant_id, release_id, definition_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_installation_snapshots_actor_fk
        FOREIGN KEY (tenant_id, agent_actor_id)
        REFERENCES wanwork_im.actor_heads (tenant_id, actor_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_installation_snapshots_installed_by_fk
        FOREIGN KEY (installed_by)
        REFERENCES wanwork_im.human_principal_heads (principal_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_installation_snapshots_identity_check
        CHECK (
            octet_length(agent_actor_id) BETWEEN 5 AND 128
            AND agent_actor_id ~ '^agt_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
            AND octet_length(installed_by) BETWEEN 5 AND 128
            AND installed_by ~ '^hpr_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
        ),
    CONSTRAINT agent_installation_snapshots_version_check
        CHECK (version ~ '^[0-9]+\.[0-9]+\.[0-9]+([+-][0-9A-Za-z.-]+)?$'),
    CONSTRAINT agent_installation_snapshots_capabilities_check
        CHECK (jsonb_typeof(granted_capabilities) = 'array' AND jsonb_array_length(granted_capabilities) BETWEEN 1 AND 128),
    CONSTRAINT agent_installation_snapshots_routes_check
        CHECK (jsonb_typeof(bound_data_routes) = 'array' AND jsonb_array_length(bound_data_routes) BETWEEN 1 AND 128),
    CONSTRAINT agent_installation_snapshots_status_check
        CHECK (status IN ('pending', 'active', 'suspended', 'revoked', 'offboarded')),
    CONSTRAINT agent_installation_snapshots_disabled_at_check
        CHECK ((status IN ('revoked', 'offboarded') AND disabled_at IS NOT NULL AND disabled_at >= created_at) OR
               (status IN ('pending', 'active', 'suspended') AND disabled_at IS NULL)),
    CONSTRAINT agent_installation_snapshots_revision_check
        CHECK (revision BETWEEN 1 AND 9223372036854775807)
);

ALTER TABLE wanwork_im.agent_installation_heads
    ADD CONSTRAINT agent_installation_heads_current_snapshot_fk
    FOREIGN KEY (tenant_id, installation_id, current_revision)
    REFERENCES wanwork_im.agent_installation_snapshots (tenant_id, installation_id, revision)
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE wanwork_im.agent_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.agent_definitions FORCE ROW LEVEL SECURITY;
CREATE POLICY agent_definitions_exact_tenant ON wanwork_im.agent_definitions
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.agent_releases ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.agent_releases FORCE ROW LEVEL SECURITY;
CREATE POLICY agent_releases_exact_tenant ON wanwork_im.agent_releases
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.agent_passports ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.agent_passports FORCE ROW LEVEL SECURITY;
CREATE POLICY agent_passports_exact_tenant ON wanwork_im.agent_passports
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.agent_installation_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.agent_installation_heads FORCE ROW LEVEL SECURITY;
CREATE POLICY agent_installation_heads_exact_tenant ON wanwork_im.agent_installation_heads
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.agent_installation_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.agent_installation_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY agent_installation_snapshots_exact_tenant ON wanwork_im.agent_installation_snapshots
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));
