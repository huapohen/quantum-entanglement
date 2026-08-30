DROP FUNCTION wanwork_im.admit_native_im_inbox(
    text, text, text, text, text, text, text, text, text, text, text, bigint, text
);

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
