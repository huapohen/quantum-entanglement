CREATE FUNCTION wanwork_im.claim_agent_provider_effect(
    p_tenant_id text,
    p_lease_digest text,
    p_lease_microseconds bigint
) RETURNS text
LANGUAGE plpgsql
VOLATILE
STRICT
SECURITY DEFINER
PARALLEL UNSAFE
SET search_path TO pg_catalog
AS $function$
DECLARE
    claimed_effect_id text;
BEGIN
    IF pg_catalog.current_setting('wanwork.tenant_id', true) IS DISTINCT FROM p_tenant_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'wanwork tenant context mismatch';
    END IF;
    IF p_lease_digest !~ '^[0-9a-f]{64}$'
       OR p_lease_microseconds < 1
       OR p_lease_microseconds > 3600000000 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid provider effect claim';
    END IF;

    WITH candidate AS (
        SELECT effect.effect_id
        FROM wanwork_im.agent_provider_effects AS effect
        WHERE effect.tenant_id = p_tenant_id
          AND effect.attempt_count < 9223372036854775807
          AND (
              effect.status IN ('queued', 'failed')
              OR (
                  effect.status = 'sent'
                  AND effect.lease_expires_at <= pg_catalog.clock_timestamp()
              )
          )
        ORDER BY effect.created_at, effect.effect_id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    ), changed AS (
        UPDATE wanwork_im.agent_provider_effects AS effect
        SET status = 'sent',
            attempt_count = effect.attempt_count + 1,
            first_sent_at = COALESCE(effect.first_sent_at, pg_catalog.clock_timestamp()),
            last_attempt_at = pg_catalog.clock_timestamp(),
            last_error_code = NULL,
            lease_token_digest = p_lease_digest,
            lease_expires_at = pg_catalog.clock_timestamp()
                + (p_lease_microseconds * interval '1 microsecond'),
            updated_at = pg_catalog.clock_timestamp()
        FROM candidate
        WHERE effect.tenant_id = p_tenant_id
          AND effect.effect_id = candidate.effect_id
        RETURNING effect.effect_id
    )
    SELECT changed.effect_id
    INTO claimed_effect_id
    FROM changed;

    RETURN COALESCE(claimed_effect_id, '');
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.claim_agent_provider_effect(
    text, text, bigint
) FROM PUBLIC;

CREATE FUNCTION wanwork_im.record_agent_provider_effect_receipt(
    p_tenant_id text,
    p_effect_id text,
    p_lease_digest text,
    p_operation_key text,
    p_status text,
    p_receipt_digest text,
    p_external_id text,
    p_observed_at timestamptz
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
    IF p_status NOT IN ('committed', 'replayed', 'unknown')
       OR p_lease_digest !~ '^[0-9a-f]{64}$'
       OR p_receipt_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid provider effect receipt';
    END IF;

    UPDATE wanwork_im.agent_provider_effects
    SET status = p_status,
        provider_receipt_digest = p_receipt_digest,
        provider_external_id = p_external_id,
        provider_receipt_status = p_status,
        provider_receipt_observed_at = p_observed_at,
        committed_at = CASE WHEN p_status = 'unknown' THEN NULL ELSE p_observed_at END,
        last_error_code = NULL,
        lease_token_digest = NULL,
        lease_expires_at = NULL,
        updated_at = pg_catalog.clock_timestamp()
    WHERE tenant_id = p_tenant_id
      AND effect_id = p_effect_id
      AND operation_key = p_operation_key
      AND status = 'sent'
      AND lease_token_digest = p_lease_digest
      AND lease_expires_at > pg_catalog.clock_timestamp();
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    RETURN changed_rows = 1;
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.record_agent_provider_effect_receipt(
    text, text, text, text, text, text, text, timestamptz
) FROM PUBLIC;

CREATE FUNCTION wanwork_im.mark_agent_provider_effect_terminal(
    p_tenant_id text,
    p_effect_id text,
    p_lease_digest text,
    p_status text,
    p_error_code text
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
    IF p_status NOT IN ('unknown', 'failed') THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid provider effect terminal state';
    END IF;

    UPDATE wanwork_im.agent_provider_effects
    SET status = p_status,
        provider_receipt_digest = NULL,
        provider_external_id = NULL,
        provider_receipt_status = NULL,
        provider_receipt_observed_at = NULL,
        committed_at = NULL,
        last_error_code = p_error_code,
        lease_token_digest = NULL,
        lease_expires_at = NULL,
        updated_at = pg_catalog.clock_timestamp()
    WHERE tenant_id = p_tenant_id
      AND effect_id = p_effect_id
      AND status = 'sent'
      AND lease_token_digest = p_lease_digest
      AND lease_expires_at > pg_catalog.clock_timestamp();
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    RETURN changed_rows = 1;
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.mark_agent_provider_effect_terminal(
    text, text, text, text, text
) FROM PUBLIC;

CREATE FUNCTION wanwork_im.resolve_agent_provider_effect(
    p_tenant_id text,
    p_effect_id text,
    p_operation_key text,
    p_status text,
    p_receipt_digest text,
    p_external_id text,
    p_observed_at timestamptz
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
    IF p_status NOT IN ('committed', 'replayed')
       OR p_receipt_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid provider effect reconciliation';
    END IF;

    UPDATE wanwork_im.agent_provider_effects
    SET status = p_status,
        provider_receipt_digest = p_receipt_digest,
        provider_external_id = p_external_id,
        provider_receipt_status = p_status,
        provider_receipt_observed_at = p_observed_at,
        committed_at = p_observed_at,
        last_error_code = NULL,
        updated_at = pg_catalog.clock_timestamp()
    WHERE tenant_id = p_tenant_id
      AND effect_id = p_effect_id
      AND operation_key = p_operation_key
      AND status = 'unknown';
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    RETURN changed_rows = 1;
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.resolve_agent_provider_effect(
    text, text, text, text, text, text, timestamptz
) FROM PUBLIC;
