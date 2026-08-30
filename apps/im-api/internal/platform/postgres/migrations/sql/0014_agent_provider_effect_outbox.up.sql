CREATE TABLE wanwork_im.agent_provider_effects (
    tenant_id text COLLATE "C" NOT NULL,
    workspace_id text COLLATE "C",
    installation_id text COLLATE "C" NOT NULL,
    effect_id text COLLATE "C" NOT NULL,
    effect_kind text COLLATE "C" NOT NULL,
    provider text COLLATE "C" NOT NULL,
    provider_realm_id text COLLATE "C" NOT NULL,
    provider_subject_id text COLLATE "C",
    operation_key text COLLATE "C" NOT NULL,
    request_ref text COLLATE "C" NOT NULL,
    request_sha256 text COLLATE "C" NOT NULL,
    status text COLLATE "C" NOT NULL,
    attempt_count bigint NOT NULL DEFAULT '0'::bigint,
    provider_receipt_digest text COLLATE "C",
    provider_external_id text COLLATE "C",
    last_error_code text COLLATE "C",
    first_sent_at timestamptz,
    last_attempt_at timestamptz,
    committed_at timestamptz,
    lease_token_digest text COLLATE "C",
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, effect_id),
    CONSTRAINT agent_provider_effects_operation_uk
        UNIQUE (tenant_id, operation_key),
    CONSTRAINT agent_provider_effects_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_provider_effects_workspace_fk
        FOREIGN KEY (tenant_id, workspace_id)
        REFERENCES wanwork_im.workspaces (tenant_id, workspace_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_provider_effects_installation_fk
        FOREIGN KEY (tenant_id, installation_id)
        REFERENCES wanwork_im.agent_installation_heads (tenant_id, installation_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_provider_effects_tenant_id_check
        CHECK (
            octet_length(tenant_id) BETWEEN 1 AND 256
            AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
        ),
    CONSTRAINT agent_provider_effects_workspace_id_check
        CHECK (
            workspace_id IS NULL OR (
                octet_length(workspace_id) BETWEEN 1 AND 256
                AND workspace_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
            )
        ),
    CONSTRAINT agent_provider_effects_installation_id_check
        CHECK (
            octet_length(installation_id) BETWEEN 1 AND 256
            AND installation_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
        ),
    CONSTRAINT agent_provider_effects_effect_id_check
        CHECK (
            octet_length(effect_id) BETWEEN 1 AND 256
            AND effect_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
        ),
    CONSTRAINT agent_provider_effects_effect_kind_check
        CHECK (effect_kind IN (
            'user_provision', 'user_revoke', 'group_create',
            'member_add', 'member_remove', 'text_send'
        )),
    CONSTRAINT agent_provider_effects_provider_check
        CHECK (
            octet_length(provider) BETWEEN 1 AND 256
            AND provider ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
        ),
    CONSTRAINT agent_provider_effects_provider_realm_id_check
        CHECK (
            octet_length(provider_realm_id) BETWEEN 1 AND 256
            AND provider_realm_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
        ),
    CONSTRAINT agent_provider_effects_provider_subject_id_check
        CHECK (
            provider_subject_id IS NULL OR (
                octet_length(provider_subject_id) BETWEEN 1 AND 256
                AND provider_subject_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
            )
        ),
    CONSTRAINT agent_provider_effects_operation_key_check
        CHECK (
            octet_length(operation_key) BETWEEN 1 AND 256
            AND operation_key ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
        ),
    CONSTRAINT agent_provider_effects_request_ref_check
        CHECK (
            octet_length(request_ref) BETWEEN 1 AND 256
            AND request_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
        ),
    CONSTRAINT agent_provider_effects_request_sha256_check
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT agent_provider_effects_status_check
        CHECK (status IN ('queued', 'sent', 'committed', 'replayed', 'unknown', 'failed')),
    CONSTRAINT agent_provider_effects_attempt_count_check
        CHECK (attempt_count BETWEEN 0 AND 9223372036854775807),
    CONSTRAINT agent_provider_effects_receipt_pair_check
        CHECK ((provider_receipt_digest IS NULL) = (provider_external_id IS NULL)),
    CONSTRAINT agent_provider_effects_receipt_digest_check
        CHECK (provider_receipt_digest IS NULL OR provider_receipt_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT agent_provider_effects_external_id_check
        CHECK (
            provider_external_id IS NULL OR (
                octet_length(provider_external_id) BETWEEN 1 AND 256
                AND provider_external_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
            )
        ),
    CONSTRAINT agent_provider_effects_last_error_code_check
        CHECK (
            last_error_code IS NULL OR (
                octet_length(last_error_code) BETWEEN 1 AND 256
                AND last_error_code ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
            )
        ),
    CONSTRAINT agent_provider_effects_lease_digest_check
        CHECK (lease_token_digest IS NULL OR lease_token_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT agent_provider_effects_state_shape_check
        CHECK (
            (
                status = 'queued'
                AND attempt_count = 0
                AND provider_receipt_digest IS NULL
                AND provider_external_id IS NULL
                AND first_sent_at IS NULL
                AND last_attempt_at IS NULL
                AND committed_at IS NULL
                AND lease_token_digest IS NULL
                AND lease_expires_at IS NULL
            )
            OR (
                status = 'sent'
                AND attempt_count > 0
                AND first_sent_at IS NOT NULL
                AND last_attempt_at IS NOT NULL
                AND provider_receipt_digest IS NULL
                AND provider_external_id IS NULL
                AND committed_at IS NULL
                AND lease_token_digest IS NOT NULL
                AND lease_expires_at IS NOT NULL
            )
            OR (
                status = 'failed'
                AND attempt_count > 0
                AND first_sent_at IS NOT NULL
                AND last_attempt_at IS NOT NULL
                AND provider_receipt_digest IS NULL
                AND provider_external_id IS NULL
                AND committed_at IS NULL
                AND lease_token_digest IS NULL
                AND lease_expires_at IS NULL
            )
            OR (
                status = 'unknown'
                AND attempt_count > 0
                AND first_sent_at IS NOT NULL
                AND last_attempt_at IS NOT NULL
                AND committed_at IS NULL
                AND lease_token_digest IS NULL
                AND lease_expires_at IS NULL
            )
            OR (
                status IN ('committed', 'replayed')
                AND attempt_count > 0
                AND first_sent_at IS NOT NULL
                AND last_attempt_at IS NOT NULL
                AND provider_receipt_digest IS NOT NULL
                AND provider_external_id IS NOT NULL
                AND committed_at IS NOT NULL
                AND lease_token_digest IS NULL
                AND lease_expires_at IS NULL
                AND last_error_code IS NULL
            )
        ),
    CONSTRAINT agent_provider_effects_time_order_check
        CHECK (
            (first_sent_at IS NULL OR first_sent_at >= created_at)
            AND (last_attempt_at IS NULL OR first_sent_at IS NOT NULL AND last_attempt_at >= first_sent_at)
            AND (committed_at IS NULL OR first_sent_at IS NOT NULL AND committed_at >= first_sent_at)
            AND (lease_expires_at IS NULL OR last_attempt_at IS NOT NULL AND lease_expires_at > last_attempt_at)
        )
);

CREATE INDEX agent_provider_effects_due_idx
    ON wanwork_im.agent_provider_effects (tenant_id, status, lease_expires_at, created_at);

ALTER TABLE wanwork_im.agent_provider_effects ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.agent_provider_effects FORCE ROW LEVEL SECURITY;
CREATE POLICY agent_provider_effects_exact_tenant ON wanwork_im.agent_provider_effects
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));
