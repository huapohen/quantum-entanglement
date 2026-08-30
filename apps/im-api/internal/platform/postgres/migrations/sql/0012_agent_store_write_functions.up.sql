CREATE FUNCTION wanwork_im.write_agent_definition_revision(
    p_tenant_id text,
    p_definition_id text,
    p_expected_revision bigint,
    p_next_revision bigint,
    p_payload text
) RETURNS boolean
LANGUAGE plpgsql
VOLATILE
STRICT
SECURITY DEFINER
PARALLEL UNSAFE
SET search_path TO pg_catalog
AS $function$
DECLARE
    payload jsonb;
    changed_rows bigint;
BEGIN
    IF pg_catalog.current_setting('wanwork.tenant_id', true) IS DISTINCT FROM p_tenant_id THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'wanwork tenant context mismatch';
    END IF;
    payload := p_payload::jsonb;
    IF pg_catalog.jsonb_typeof(payload) <> 'object'
       OR (SELECT count(*) FROM pg_catalog.jsonb_object_keys(payload)) <> 8
       OR NOT (payload ?& ARRAY['id', 'tenantId', 'claimedBy', 'publisherId', 'displayName', 'summary', 'status', 'revision'])
       OR payload->>'id' IS DISTINCT FROM p_definition_id
       OR payload->>'tenantId' IS DISTINCT FROM p_tenant_id
       OR (payload->>'revision')::bigint IS DISTINCT FROM p_next_revision THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Agent definition payload';
    END IF;
    IF p_expected_revision = 0 AND p_next_revision = 1 THEN
        INSERT INTO wanwork_im.agent_definitions (
            tenant_id, definition_id, claimed_by, publisher_id, display_name, summary, status, revision
        ) VALUES (
            p_tenant_id, p_definition_id, payload->>'claimedBy', payload->>'publisherId',
            payload->>'displayName', payload->>'summary', payload->>'status', p_next_revision
        );
        GET DIAGNOSTICS changed_rows = ROW_COUNT;
        RETURN changed_rows = 1;
    ELSIF p_expected_revision > 0 AND p_next_revision = p_expected_revision + 1 THEN
        UPDATE wanwork_im.agent_definitions
        SET claimed_by = payload->>'claimedBy',
            publisher_id = payload->>'publisherId',
            display_name = payload->>'displayName',
            summary = payload->>'summary',
            status = payload->>'status',
            revision = p_next_revision,
            recorded_at = clock_timestamp()
        WHERE tenant_id = p_tenant_id AND definition_id = p_definition_id AND revision = p_expected_revision;
        GET DIAGNOSTICS changed_rows = ROW_COUNT;
        RETURN changed_rows = 1;
    END IF;
    RETURN false;
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.write_agent_definition_revision(text, text, bigint, bigint, text) FROM PUBLIC;

CREATE FUNCTION wanwork_im.write_agent_release_revision(
    p_tenant_id text,
    p_release_id text,
    p_expected_revision bigint,
    p_next_revision bigint,
    p_payload text
) RETURNS boolean
LANGUAGE plpgsql
VOLATILE
STRICT
SECURITY DEFINER
PARALLEL UNSAFE
SET search_path TO pg_catalog
AS $function$
DECLARE
    payload jsonb;
    changed_rows bigint;
BEGIN
    IF pg_catalog.current_setting('wanwork.tenant_id', true) IS DISTINCT FROM p_tenant_id THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'wanwork tenant context mismatch';
    END IF;
    payload := p_payload::jsonb;
    IF pg_catalog.jsonb_typeof(payload) <> 'object'
       OR (SELECT count(*) FROM pg_catalog.jsonb_object_keys(payload)) <> 13
       OR NOT (payload ?& ARRAY['id', 'definitionId', 'version', 'artifactDigest', 'manifestDigest', 'personaDigest', 'requestedCapabilities', 'prohibitions', 'dataRoutes', 'isolation', 'status', 'publishedAt', 'revision'])
       OR payload->>'id' IS DISTINCT FROM p_release_id
       OR (payload->>'revision')::bigint IS DISTINCT FROM p_next_revision THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Agent release payload';
    END IF;
    IF p_expected_revision = 0 AND p_next_revision = 1 THEN
        INSERT INTO wanwork_im.agent_releases (
            tenant_id, release_id, definition_id, version, artifact_digest, manifest_digest, persona_digest,
            requested_capabilities, prohibitions, data_routes, isolation, status, published_at, revision
        ) VALUES (
            p_tenant_id, p_release_id, payload->>'definitionId', payload->>'version',
            'sha256:' || (payload->>'artifactDigest'), 'sha256:' || (payload->>'manifestDigest'), 'sha256:' || (payload->>'personaDigest'),
            payload->'requestedCapabilities', payload->'prohibitions', payload->'dataRoutes',
            payload->>'isolation', payload->>'status', (payload->>'publishedAt')::timestamptz, p_next_revision
        );
        GET DIAGNOSTICS changed_rows = ROW_COUNT;
        RETURN changed_rows = 1;
    ELSIF p_expected_revision > 0 AND p_next_revision = p_expected_revision + 1 THEN
        UPDATE wanwork_im.agent_releases
        SET status = payload->>'status',
            published_at = (payload->>'publishedAt')::timestamptz,
            revision = p_next_revision,
            recorded_at = clock_timestamp()
        WHERE tenant_id = p_tenant_id AND release_id = p_release_id AND revision = p_expected_revision;
        GET DIAGNOSTICS changed_rows = ROW_COUNT;
        RETURN changed_rows = 1;
    END IF;
    RETURN false;
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.write_agent_release_revision(text, text, bigint, bigint, text) FROM PUBLIC;

CREATE FUNCTION wanwork_im.write_agent_passport_revision(
    p_tenant_id text,
    p_release_id text,
    p_expected_revision bigint,
    p_next_revision bigint,
    p_payload text
) RETURNS boolean
LANGUAGE plpgsql
VOLATILE
STRICT
SECURITY DEFINER
PARALLEL UNSAFE
SET search_path TO pg_catalog
AS $function$
DECLARE
    payload jsonb;
    changed_rows bigint;
BEGIN
    IF pg_catalog.current_setting('wanwork.tenant_id', true) IS DISTINCT FROM p_tenant_id THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'wanwork tenant context mismatch';
    END IF;
    payload := p_payload::jsonb;
    IF pg_catalog.jsonb_typeof(payload) <> 'object'
       OR (SELECT count(*) FROM pg_catalog.jsonb_object_keys(payload)) <> 5
       OR NOT (payload ?& ARRAY['definition', 'release', 'attestations', 'status', 'revision'])
       OR (payload->'release'->>'id') IS DISTINCT FROM p_release_id
       OR (payload->>'revision')::bigint IS DISTINCT FROM p_next_revision THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Agent Passport payload';
    END IF;
    IF p_expected_revision = 0 AND p_next_revision = 1 THEN
        INSERT INTO wanwork_im.agent_passports (
            tenant_id, release_id, definition_id, status, attestations, revision
        ) VALUES (
            p_tenant_id, p_release_id, payload->'definition'->>'id', payload->>'status',
            payload->'attestations', p_next_revision
        );
        GET DIAGNOSTICS changed_rows = ROW_COUNT;
        RETURN changed_rows = 1;
    ELSIF p_expected_revision > 0 AND p_next_revision = p_expected_revision + 1 THEN
        UPDATE wanwork_im.agent_passports
        SET status = payload->>'status',
            attestations = payload->'attestations',
            revision = p_next_revision,
            recorded_at = clock_timestamp()
        WHERE tenant_id = p_tenant_id AND release_id = p_release_id AND revision = p_expected_revision;
        GET DIAGNOSTICS changed_rows = ROW_COUNT;
        RETURN changed_rows = 1;
    END IF;
    RETURN false;
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.write_agent_passport_revision(text, text, bigint, bigint, text) FROM PUBLIC;

CREATE FUNCTION wanwork_im.write_agent_installation_revision(
    p_tenant_id text,
    p_installation_id text,
    p_expected_revision bigint,
    p_next_revision bigint,
    p_payload text
) RETURNS boolean
LANGUAGE plpgsql
VOLATILE
STRICT
SECURITY DEFINER
PARALLEL UNSAFE
SET search_path TO pg_catalog
AS $function$
DECLARE
    payload jsonb;
    changed_rows bigint;
BEGIN
    IF pg_catalog.current_setting('wanwork.tenant_id', true) IS DISTINCT FROM p_tenant_id THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'wanwork tenant context mismatch';
    END IF;
    payload := p_payload::jsonb;
    IF pg_catalog.jsonb_typeof(payload) <> 'object'
       OR (SELECT count(*) FROM pg_catalog.jsonb_object_keys(payload)) <> 14
       OR NOT (payload ?& ARRAY['id', 'tenantId', 'workspaceId', 'definitionId', 'releaseId', 'version', 'agentActorId', 'installedBy', 'grantedCapabilities', 'boundDataRoutes', 'status', 'createdAt', 'disabledAt', 'revision'])
       OR payload->>'id' IS DISTINCT FROM p_installation_id
       OR payload->>'tenantId' IS DISTINCT FROM p_tenant_id
       OR (payload->>'revision')::bigint IS DISTINCT FROM p_next_revision THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Agent installation payload';
    END IF;
    IF p_expected_revision = 0 AND p_next_revision = 1 THEN
        INSERT INTO wanwork_im.agent_installation_heads (tenant_id, installation_id, current_revision)
        VALUES (p_tenant_id, p_installation_id, p_next_revision);
        INSERT INTO wanwork_im.agent_installation_snapshots (
            tenant_id, installation_id, revision, workspace_id, definition_id, release_id, version,
            agent_actor_id, installed_by, granted_capabilities, bound_data_routes, status, created_at, disabled_at
        ) VALUES (
            p_tenant_id, p_installation_id, p_next_revision, payload->>'workspaceId', payload->>'definitionId',
            payload->>'releaseId', payload->>'version', payload->>'agentActorId', payload->>'installedBy',
            payload->'grantedCapabilities', payload->'boundDataRoutes', payload->>'status',
            (payload->>'createdAt')::timestamptz, (payload->>'disabledAt')::timestamptz
        );
        RETURN true;
    ELSIF p_expected_revision > 0 AND p_next_revision = p_expected_revision + 1 THEN
        UPDATE wanwork_im.agent_installation_heads
        SET current_revision = p_next_revision
        WHERE tenant_id = p_tenant_id AND installation_id = p_installation_id AND current_revision = p_expected_revision;
        GET DIAGNOSTICS changed_rows = ROW_COUNT;
        IF changed_rows <> 1 THEN
            RETURN false;
        END IF;
        INSERT INTO wanwork_im.agent_installation_snapshots (
            tenant_id, installation_id, revision, workspace_id, definition_id, release_id, version,
            agent_actor_id, installed_by, granted_capabilities, bound_data_routes, status, created_at, disabled_at
        ) VALUES (
            p_tenant_id, p_installation_id, p_next_revision, payload->>'workspaceId', payload->>'definitionId',
            payload->>'releaseId', payload->>'version', payload->>'agentActorId', payload->>'installedBy',
            payload->'grantedCapabilities', payload->'boundDataRoutes', payload->>'status',
            (payload->>'createdAt')::timestamptz, (payload->>'disabledAt')::timestamptz
        );
        RETURN true;
    END IF;
    RETURN false;
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.write_agent_installation_revision(text, text, bigint, bigint, text) FROM PUBLIC;
