CREATE FUNCTION wanwork_im.write_agent_provider_effect(
    p_tenant_id text,
    p_workspace_id text,
    p_installation_id text,
    p_effect_id text,
    p_effect_kind text,
    p_provider text,
    p_provider_realm_id text,
    p_provider_subject_id text,
    p_operation_key text,
    p_request_ref text,
    p_request_sha256 text
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
    existing_effect_id text;
    existing_workspace_id text;
    existing_installation_id text;
    existing_effect_kind text;
    existing_provider text;
    existing_provider_realm_id text;
    existing_provider_subject_id text;
    existing_operation_key text;
    existing_request_ref text;
    existing_request_sha256 text;
BEGIN
    IF pg_catalog.current_setting('wanwork.tenant_id', true) IS DISTINCT FROM p_tenant_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'wanwork tenant context mismatch';
    END IF;

    INSERT INTO wanwork_im.agent_provider_effects (
        tenant_id, workspace_id, installation_id, effect_id, effect_kind,
        provider, provider_realm_id, provider_subject_id, operation_key,
        request_ref, request_sha256, status
    ) VALUES (
        p_tenant_id, NULLIF(p_workspace_id, ''), p_installation_id, p_effect_id, p_effect_kind,
        p_provider, p_provider_realm_id, NULLIF(p_provider_subject_id, ''), p_operation_key,
        p_request_ref, p_request_sha256, 'queued'
    ) ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows = 1 THEN
        RETURN 'inserted';
    END IF;

    SELECT effect_id, workspace_id, installation_id, effect_kind, provider,
           provider_realm_id, provider_subject_id, operation_key, request_ref, request_sha256
    INTO existing_effect_id, existing_workspace_id, existing_installation_id, existing_effect_kind, existing_provider,
         existing_provider_realm_id, existing_provider_subject_id, existing_operation_key, existing_request_ref,
         existing_request_sha256
    FROM wanwork_im.agent_provider_effects
    WHERE tenant_id = p_tenant_id
      AND (effect_id = p_effect_id OR operation_key = p_operation_key)
    LIMIT 1;
    IF NOT FOUND OR existing_effect_id IS DISTINCT FROM p_effect_id OR
       existing_workspace_id IS DISTINCT FROM NULLIF(p_workspace_id, '') OR
       existing_installation_id IS DISTINCT FROM p_installation_id OR
       existing_effect_kind IS DISTINCT FROM p_effect_kind OR
       existing_provider IS DISTINCT FROM p_provider OR
       existing_provider_realm_id IS DISTINCT FROM p_provider_realm_id OR
       existing_provider_subject_id IS DISTINCT FROM NULLIF(p_provider_subject_id, '') OR
       existing_operation_key IS DISTINCT FROM p_operation_key OR
       existing_request_ref IS DISTINCT FROM p_request_ref OR
       existing_request_sha256 IS DISTINCT FROM p_request_sha256 THEN
        RETURN 'conflict';
    END IF;
    RETURN 'replayed';
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.write_agent_provider_effect(
    text, text, text, text, text, text, text, text, text, text, text
) FROM PUBLIC;
