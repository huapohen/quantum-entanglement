CREATE TABLE wanwork_im.event_stream_heads (
    tenant_id text COLLATE "C" NOT NULL,
    workspace_id text COLLATE "C" NOT NULL,
    stream_id text COLLATE "C" NOT NULL,
    current_sequence bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, workspace_id, stream_id),
    CONSTRAINT event_stream_heads_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT event_stream_heads_workspace_check
        CHECK (
            workspace_id = ''
            OR (
                octet_length(workspace_id) BETWEEN 5 AND 128
                AND workspace_id ~ '^wsp_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
            )
        ),
    CONSTRAINT event_stream_heads_stream_id_check
        CHECK (octet_length(stream_id) BETWEEN 1 AND 256),
    CONSTRAINT event_stream_heads_sequence_check
        CHECK (current_sequence BETWEEN 1 AND 9223372036854775807)
);

CREATE TABLE wanwork_im.event_tenant_heads (
    tenant_id text COLLATE "C" NOT NULL,
    current_global_position bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id),
    CONSTRAINT event_tenant_heads_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT event_tenant_heads_position_check
        CHECK (current_global_position BETWEEN 1 AND 9223372036854775807)
);

CREATE TABLE wanwork_im.event_log (
    tenant_id text COLLATE "C" NOT NULL,
    workspace_id text COLLATE "C" NOT NULL,
    stream_id text COLLATE "C" NOT NULL,
    sequence bigint NOT NULL,
    global_position bigint NOT NULL,
    event_id text COLLATE "C" NOT NULL,
    schema_version bigint NOT NULL,
    event_type text COLLATE "C" NOT NULL,
    actor_id text COLLATE "C" NOT NULL,
    occurred_at timestamptz NOT NULL,
    correlation_id text COLLATE "C" NOT NULL,
    causation_id text COLLATE "C" NOT NULL,
    idempotency_key text COLLATE "C" NOT NULL,
    traceparent text COLLATE "C" NOT NULL,
    payload_kind text COLLATE "C" NOT NULL,
    payload_inline text,
    payload_storage text COLLATE "C" NOT NULL,
    payload_reference_id text COLLATE "C" NOT NULL,
    payload_byte_length bigint NOT NULL,
    payload_digest text COLLATE "C" NOT NULL,
    append_digest text COLLATE "C" NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, event_id),
    CONSTRAINT event_log_stream_sequence_uk
        UNIQUE (tenant_id, workspace_id, stream_id, sequence),
    CONSTRAINT event_log_global_position_uk
        UNIQUE (tenant_id, global_position),
    CONSTRAINT event_log_stream_fk
        FOREIGN KEY (tenant_id, workspace_id, stream_id)
        REFERENCES wanwork_im.event_stream_heads (tenant_id, workspace_id, stream_id)
        ON DELETE RESTRICT,
    CONSTRAINT event_log_tenant_head_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.event_tenant_heads (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT event_log_workspace_check
        CHECK (
            workspace_id = ''
            OR (
                octet_length(workspace_id) BETWEEN 5 AND 128
                AND workspace_id ~ '^wsp_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
            )
        ),
    CONSTRAINT event_log_stream_id_check
        CHECK (octet_length(stream_id) BETWEEN 1 AND 256),
    CONSTRAINT event_log_sequence_check
        CHECK (sequence BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT event_log_global_position_check
        CHECK (global_position BETWEEN 1 AND 9223372036854775807),
    CONSTRAINT event_log_event_id_check
        CHECK (octet_length(event_id) BETWEEN 1 AND 256),
    CONSTRAINT event_log_schema_version_check
        CHECK (schema_version BETWEEN 1 AND 4294967295),
    CONSTRAINT event_log_event_type_check
        CHECK (octet_length(event_type) BETWEEN 1 AND 192),
    CONSTRAINT event_log_actor_id_check
        CHECK (octet_length(actor_id) BETWEEN 1 AND 256),
    CONSTRAINT event_log_correlation_id_check
        CHECK (octet_length(correlation_id) BETWEEN 1 AND 256),
    CONSTRAINT event_log_causation_id_check
        CHECK (octet_length(causation_id) <= 256),
    CONSTRAINT event_log_idempotency_key_check
        CHECK (octet_length(idempotency_key) <= 128),
    CONSTRAINT event_log_traceparent_check
        CHECK (octet_length(traceparent) <= 128),
    CONSTRAINT event_log_payload_kind_check
        CHECK (payload_kind IN ('inline', 'reference')),
    CONSTRAINT event_log_payload_shape_check
        CHECK (
            (
                payload_kind = 'inline'
                AND payload_inline IS NOT NULL
                AND pg_catalog.jsonb_typeof(payload_inline::jsonb) = 'object'
                AND payload_storage = ''
                AND payload_reference_id = ''
                AND payload_byte_length = -1
            )
            OR (
                payload_kind = 'reference'
                AND payload_inline IS NULL
                AND octet_length(payload_storage) BETWEEN 1 AND 64
                AND payload_reference_id <> ''
                AND payload_byte_length BETWEEN 0 AND 9223372036854775807
            )
        ),
    CONSTRAINT event_log_payload_digest_check
        CHECK (payload_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT event_log_append_digest_check
        CHECK (append_digest ~ '^sha256:[0-9a-f]{64}$')
);

ALTER TABLE wanwork_im.event_stream_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.event_stream_heads FORCE ROW LEVEL SECURITY;
CREATE POLICY event_stream_heads_exact_tenant
    ON wanwork_im.event_stream_heads
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.event_tenant_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.event_tenant_heads FORCE ROW LEVEL SECURITY;
CREATE POLICY event_tenant_heads_exact_tenant
    ON wanwork_im.event_tenant_heads
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

ALTER TABLE wanwork_im.event_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.event_log FORCE ROW LEVEL SECURITY;
CREATE POLICY event_log_exact_tenant
    ON wanwork_im.event_log
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

CREATE FUNCTION wanwork_im.write_event(
    p_tenant_id text,
    p_workspace_id text,
    p_stream_id text,
    p_expected_version bigint,
    p_event_id text,
    p_schema_version bigint,
    p_event_type text,
    p_actor_id text,
    p_occurred_at timestamptz,
    p_correlation_id text,
    p_causation_id text,
    p_idempotency_key text,
    p_traceparent text,
    p_payload_kind text,
    p_payload_inline text,
    p_payload_storage text,
    p_payload_reference_id text,
    p_payload_byte_length bigint,
    p_payload_digest text,
    p_append_digest text
) RETURNS boolean
LANGUAGE plpgsql
VOLATILE
STRICT
SECURITY DEFINER
PARALLEL UNSAFE
SET search_path TO pg_catalog
AS $function$
DECLARE
    changed_rows bigint;
    next_sequence bigint;
    next_global_position bigint;
BEGIN
    IF pg_catalog.current_setting('wanwork.tenant_id', true) IS DISTINCT FROM p_tenant_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'wanwork tenant context mismatch';
    END IF;
    IF p_expected_version < 0 OR p_expected_version >= 9223372036854775807 THEN
        RETURN false;
    END IF;
    next_sequence := p_expected_version + 1;

    IF p_expected_version = 0 THEN
        INSERT INTO wanwork_im.event_stream_heads (
            tenant_id, workspace_id, stream_id, current_sequence
        ) VALUES (
            p_tenant_id, p_workspace_id, p_stream_id, next_sequence
        ) ON CONFLICT DO NOTHING;
    ELSE
        UPDATE wanwork_im.event_stream_heads
        SET current_sequence = next_sequence
        WHERE tenant_id = p_tenant_id
          AND workspace_id = p_workspace_id
          AND stream_id = p_stream_id
          AND current_sequence = p_expected_version;
    END IF;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows <> 1 THEN
        RETURN false;
    END IF;

    INSERT INTO wanwork_im.event_tenant_heads (
        tenant_id, current_global_position
    ) VALUES (
        p_tenant_id, 1
    ) ON CONFLICT (tenant_id) DO UPDATE
        SET current_global_position = wanwork_im.event_tenant_heads.current_global_position + 1
    RETURNING current_global_position INTO next_global_position;

    INSERT INTO wanwork_im.event_log (
        tenant_id, workspace_id, stream_id, sequence, global_position,
        event_id, schema_version, event_type, actor_id, occurred_at,
        correlation_id, causation_id, idempotency_key, traceparent,
        payload_kind, payload_inline, payload_storage, payload_reference_id,
        payload_byte_length, payload_digest, append_digest
    ) VALUES (
        p_tenant_id, p_workspace_id, p_stream_id, next_sequence, next_global_position,
        p_event_id, p_schema_version, p_event_type, p_actor_id, p_occurred_at,
        p_correlation_id, p_causation_id, p_idempotency_key, p_traceparent,
        p_payload_kind, NULLIF(p_payload_inline, ''), p_payload_storage,
        p_payload_reference_id, p_payload_byte_length, p_payload_digest, p_append_digest
    );
    RETURN true;
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.write_event(
    text, text, text, bigint, text, bigint, text, text, timestamptz, text,
    text, text, text, text, text, text, text, bigint, text, text
) FROM PUBLIC;
