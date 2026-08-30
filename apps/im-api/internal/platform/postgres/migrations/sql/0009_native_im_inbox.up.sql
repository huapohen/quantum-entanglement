CREATE TABLE wanwork_im.native_im_inbox (
    tenant_id text COLLATE "C" NOT NULL,
    workspace_id text COLLATE "C" NOT NULL,
    provider text COLLATE "C" NOT NULL,
    channel_id text COLLATE "C" NOT NULL,
    event_id text COLLATE "C" NOT NULL,
    event_digest text COLLATE "C" NOT NULL,
    verification_id text COLLATE "C" NOT NULL,
    payload_kind text COLLATE "C" NOT NULL,
    payload_inline text,
    payload_storage text COLLATE "C" NOT NULL,
    payload_reference_id text COLLATE "C" NOT NULL,
    payload_byte_length bigint NOT NULL,
    payload_digest text COLLATE "C" NOT NULL,
    first_received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    delivery_count bigint NOT NULL DEFAULT '1'::bigint,
    PRIMARY KEY (tenant_id, workspace_id, provider, channel_id, event_id),
    CONSTRAINT native_im_inbox_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT native_im_inbox_workspace_check
        CHECK (
            workspace_id = ''
            OR (
                octet_length(workspace_id) BETWEEN 5 AND 128
                AND workspace_id ~ '^wsp_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
            )
        ),
    CONSTRAINT native_im_inbox_provider_check
        CHECK (octet_length(provider) BETWEEN 1 AND 64),
    CONSTRAINT native_im_inbox_channel_id_check
        CHECK (octet_length(channel_id) BETWEEN 1 AND 256),
    CONSTRAINT native_im_inbox_event_id_check
        CHECK (octet_length(event_id) BETWEEN 1 AND 256),
    CONSTRAINT native_im_inbox_event_digest_check
        CHECK (event_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT native_im_inbox_verification_id_check
        CHECK (octet_length(verification_id) BETWEEN 1 AND 256),
    CONSTRAINT native_im_inbox_payload_kind_check
        CHECK (payload_kind IN ('inline', 'reference')),
    CONSTRAINT native_im_inbox_payload_shape_check
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
    CONSTRAINT native_im_inbox_payload_digest_check
        CHECK (payload_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT native_im_inbox_received_order_check
        CHECK (last_received_at >= first_received_at),
    CONSTRAINT native_im_inbox_delivery_count_check
        CHECK (delivery_count BETWEEN 1 AND 9223372036854775807)
);

ALTER TABLE wanwork_im.native_im_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.native_im_inbox FORCE ROW LEVEL SECURITY;
CREATE POLICY native_im_inbox_exact_tenant
    ON wanwork_im.native_im_inbox
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

CREATE FUNCTION wanwork_im.admit_native_im_inbox(
    p_tenant_id text,
    p_workspace_id text,
    p_provider text,
    p_channel_id text,
    p_event_id text,
    p_event_digest text,
    p_verification_id text,
    p_payload_kind text,
    p_payload_inline text,
    p_payload_storage text,
    p_payload_reference_id text,
    p_payload_byte_length bigint,
    p_payload_digest text
) RETURNS text
LANGUAGE plpgsql
VOLATILE
STRICT
SECURITY DEFINER
PARALLEL UNSAFE
SET search_path TO pg_catalog
AS $function$
DECLARE
    changed_rows bigint;
    existing_event_digest text;
    existing_payload_digest text;
BEGIN
    IF pg_catalog.current_setting('wanwork.tenant_id', true) IS DISTINCT FROM p_tenant_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'wanwork tenant context mismatch';
    END IF;

    INSERT INTO wanwork_im.native_im_inbox (
        tenant_id, workspace_id, provider, channel_id, event_id,
        event_digest, verification_id, payload_kind, payload_inline,
        payload_storage, payload_reference_id, payload_byte_length, payload_digest
    ) VALUES (
        p_tenant_id, p_workspace_id, p_provider, p_channel_id, p_event_id,
        p_event_digest, p_verification_id, p_payload_kind, NULLIF(p_payload_inline, ''),
        p_payload_storage, p_payload_reference_id, p_payload_byte_length, p_payload_digest
    ) ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows = 1 THEN
        RETURN 'inserted';
    END IF;

    SELECT event_digest, payload_digest
    INTO existing_event_digest, existing_payload_digest
    FROM wanwork_im.native_im_inbox
    WHERE tenant_id = p_tenant_id
      AND workspace_id = p_workspace_id
      AND provider = p_provider
      AND channel_id = p_channel_id
      AND event_id = p_event_id;
    IF NOT FOUND OR existing_event_digest IS DISTINCT FROM p_event_digest OR
       existing_payload_digest IS DISTINCT FROM p_payload_digest THEN
        RETURN 'conflict';
    END IF;

    UPDATE wanwork_im.native_im_inbox
    SET last_received_at = clock_timestamp(),
        delivery_count = delivery_count + 1
    WHERE tenant_id = p_tenant_id
      AND workspace_id = p_workspace_id
      AND provider = p_provider
      AND channel_id = p_channel_id
      AND event_id = p_event_id
      AND event_digest = p_event_digest
      AND payload_digest = p_payload_digest;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows <> 1 THEN
        RETURN 'conflict';
    END IF;
    RETURN 'replayed';
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.admit_native_im_inbox(
    text, text, text, text, text, text, text, text, text, text, text, bigint, text
) FROM PUBLIC;
