CREATE FUNCTION wanwork_im.write_conversation_revision(
    p_tenant_id text,
    p_conversation_id text,
    p_expected_revision bigint,
    p_next_revision bigint,
    p_workspace_id text,
    p_conversation_type text,
    p_status text
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
BEGIN
    IF pg_catalog.current_setting('wanwork.tenant_id', true) IS DISTINCT FROM p_tenant_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'wanwork tenant context mismatch';
    END IF;
    IF p_expected_revision < 0 OR p_expected_revision >= 9223372036854775807 THEN
        RETURN false;
    END IF;
    IF p_next_revision <> p_expected_revision + 1 THEN
        RETURN false;
    END IF;

    IF p_expected_revision = 0 THEN
        INSERT INTO wanwork_im.conversation_heads (
            tenant_id,
            conversation_id,
            conversation_type,
            current_revision
        ) VALUES (
            p_tenant_id,
            p_conversation_id,
            p_conversation_type,
            p_next_revision
        ) ON CONFLICT DO NOTHING;
    ELSE
        UPDATE wanwork_im.conversation_heads
        SET current_revision = p_next_revision
        WHERE tenant_id = p_tenant_id
          AND conversation_id = p_conversation_id
          AND conversation_type = p_conversation_type
          AND current_revision = p_expected_revision;
    END IF;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows <> 1 THEN
        RETURN false;
    END IF;

    INSERT INTO wanwork_im.conversation_snapshots (
        tenant_id,
        conversation_id,
        revision,
        workspace_id,
        conversation_type,
        status
    ) VALUES (
        p_tenant_id,
        p_conversation_id,
        p_next_revision,
        NULLIF(p_workspace_id, ''),
        p_conversation_type,
        p_status
    );
    RETURN true;
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.write_conversation_revision(
    text, text, bigint, bigint, text, text, text
) FROM PUBLIC;

CREATE FUNCTION wanwork_im.write_provider_conversation_binding_revision(
    p_tenant_id text,
    p_provider text,
    p_realm_id text,
    p_provider_conversation_id text,
    p_expected_revision bigint,
    p_next_revision bigint,
    p_conversation_id text,
    p_status text
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
BEGIN
    IF pg_catalog.current_setting('wanwork.tenant_id', true) IS DISTINCT FROM p_tenant_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'wanwork tenant context mismatch';
    END IF;
    IF p_expected_revision < 0 OR p_expected_revision >= 9223372036854775807 THEN
        RETURN false;
    END IF;
    IF p_next_revision <> p_expected_revision + 1 THEN
        RETURN false;
    END IF;

    IF p_expected_revision = 0 THEN
        INSERT INTO wanwork_im.provider_conversation_binding_heads (
            tenant_id,
            provider,
            realm_id,
            provider_conversation_id,
            current_revision,
            current_conversation_id,
            current_conversation_type,
            current_status
        ) VALUES (
            p_tenant_id,
            p_provider,
            p_realm_id,
            p_provider_conversation_id,
            p_next_revision,
            p_conversation_id,
            'group',
            p_status
        ) ON CONFLICT DO NOTHING;
    ELSE
        UPDATE wanwork_im.provider_conversation_binding_heads
        SET current_revision = p_next_revision,
            current_conversation_id = p_conversation_id,
            current_conversation_type = 'group',
            current_status = p_status
        WHERE tenant_id = p_tenant_id
          AND provider = p_provider
          AND realm_id = p_realm_id
          AND provider_conversation_id = p_provider_conversation_id
          AND current_revision = p_expected_revision;
    END IF;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows <> 1 THEN
        RETURN false;
    END IF;

    INSERT INTO wanwork_im.provider_conversation_binding_snapshots (
        tenant_id,
        provider,
        realm_id,
        provider_conversation_id,
        revision,
        conversation_id,
        conversation_type,
        status
    ) VALUES (
        p_tenant_id,
        p_provider,
        p_realm_id,
        p_provider_conversation_id,
        p_next_revision,
        p_conversation_id,
        'group',
        p_status
    );
    RETURN true;
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.write_provider_conversation_binding_revision(
    text, text, text, text, bigint, bigint, text, text
) FROM PUBLIC;

CREATE FUNCTION wanwork_im.write_conversation_membership_revision(
    p_tenant_id text,
    p_conversation_id text,
    p_actor_id text,
    p_expected_revision bigint,
    p_next_revision bigint,
    p_role text,
    p_status text
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
BEGIN
    IF pg_catalog.current_setting('wanwork.tenant_id', true) IS DISTINCT FROM p_tenant_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'wanwork tenant context mismatch';
    END IF;
    IF p_expected_revision < 0 OR p_expected_revision >= 9223372036854775807 THEN
        RETURN false;
    END IF;
    IF p_next_revision <> p_expected_revision + 1 THEN
        RETURN false;
    END IF;

    IF p_expected_revision = 0 THEN
        INSERT INTO wanwork_im.conversation_membership_heads (
            tenant_id,
            conversation_id,
            actor_id,
            current_revision
        ) VALUES (
            p_tenant_id,
            p_conversation_id,
            p_actor_id,
            p_next_revision
        ) ON CONFLICT DO NOTHING;
    ELSE
        UPDATE wanwork_im.conversation_membership_heads
        SET current_revision = p_next_revision
        WHERE tenant_id = p_tenant_id
          AND conversation_id = p_conversation_id
          AND actor_id = p_actor_id
          AND current_revision = p_expected_revision;
    END IF;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows <> 1 THEN
        RETURN false;
    END IF;

    INSERT INTO wanwork_im.conversation_membership_snapshots (
        tenant_id,
        conversation_id,
        actor_id,
        revision,
        role,
        status
    ) VALUES (
        p_tenant_id,
        p_conversation_id,
        p_actor_id,
        p_next_revision,
        p_role,
        p_status
    );
    RETURN true;
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.write_conversation_membership_revision(
    text, text, text, bigint, bigint, text, text
) FROM PUBLIC;

CREATE FUNCTION wanwork_im.write_conversation_access_revision(
    p_tenant_id text,
    p_conversation_id text,
    p_actor_id text,
    p_expected_revision bigint,
    p_next_revision bigint,
    p_can_read boolean,
    p_can_send_message boolean,
    p_can_manage_members boolean,
    p_can_manage_conversation boolean,
    p_can_invoke_agent boolean,
    p_can_publish_artifact_reference boolean
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
BEGIN
    IF pg_catalog.current_setting('wanwork.tenant_id', true) IS DISTINCT FROM p_tenant_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'wanwork tenant context mismatch';
    END IF;
    IF p_expected_revision < 0 OR p_expected_revision >= 9223372036854775807 THEN
        RETURN false;
    END IF;
    IF p_next_revision <> p_expected_revision + 1 THEN
        RETURN false;
    END IF;

    IF p_expected_revision = 0 THEN
        INSERT INTO wanwork_im.conversation_access_heads (
            tenant_id,
            conversation_id,
            actor_id,
            current_revision
        ) VALUES (
            p_tenant_id,
            p_conversation_id,
            p_actor_id,
            p_next_revision
        ) ON CONFLICT DO NOTHING;
    ELSE
        UPDATE wanwork_im.conversation_access_heads
        SET current_revision = p_next_revision
        WHERE tenant_id = p_tenant_id
          AND conversation_id = p_conversation_id
          AND actor_id = p_actor_id
          AND current_revision = p_expected_revision;
    END IF;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows <> 1 THEN
        RETURN false;
    END IF;

    INSERT INTO wanwork_im.conversation_access_snapshots (
        tenant_id,
        conversation_id,
        actor_id,
        revision,
        can_read,
        can_send_message,
        can_manage_members,
        can_manage_conversation,
        can_invoke_agent,
        can_publish_artifact_reference
    ) VALUES (
        p_tenant_id,
        p_conversation_id,
        p_actor_id,
        p_next_revision,
        p_can_read,
        p_can_send_message,
        p_can_manage_members,
        p_can_manage_conversation,
        p_can_invoke_agent,
        p_can_publish_artifact_reference
    );
    RETURN true;
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.write_conversation_access_revision(
    text, text, text, bigint, bigint,
    boolean, boolean, boolean, boolean, boolean, boolean
) FROM PUBLIC;

CREATE FUNCTION wanwork_im.write_tenant_command_receipt(
    p_tenant_id text,
    p_command_kind text,
    p_idempotency_key text,
    p_request_sha256 text,
    p_result_sha256 text
) RETURNS timestamptz
LANGUAGE plpgsql
VOLATILE
STRICT
SECURITY DEFINER
PARALLEL UNSAFE
SET search_path TO pg_catalog
AS $function$
DECLARE
    receipt_committed_at timestamptz;
BEGIN
    IF pg_catalog.current_setting('wanwork.tenant_id', true) IS DISTINCT FROM p_tenant_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'wanwork tenant context mismatch';
    END IF;

    INSERT INTO wanwork_im.tenant_command_receipts (
        tenant_id,
        command_kind,
        idempotency_key,
        request_sha256,
        result_sha256
    ) VALUES (
        p_tenant_id,
        p_command_kind,
        p_idempotency_key,
        p_request_sha256,
        p_result_sha256
    ) RETURNING committed_at INTO receipt_committed_at;
    RETURN receipt_committed_at;
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.write_tenant_command_receipt(
    text, text, text, text, text
) FROM PUBLIC;
