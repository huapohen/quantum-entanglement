CREATE FUNCTION wanwork_im.write_message_projection(
    p_tenant_id text,
    p_workspace_id text,
    p_conversation_id text,
    p_projection_id text,
    p_expected_sequence bigint,
    p_expected_global_position bigint,
    p_expected_revision bigint,
    p_next_sequence bigint,
    p_next_global_position bigint,
    p_next_revision bigint,
    p_message_id text,
    p_client_message_id text,
    p_sender_actor_id text,
    p_message_type text,
    p_status text,
    p_text text,
    p_ext_info text,
    p_created_at timestamptz,
    p_message_revision bigint,
    p_last_event_sequence bigint,
    p_last_event_position bigint,
    p_projection_revision bigint
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
    current_sequence bigint;
    current_global_position bigint;
    current_revision bigint;
    existing_workspace_id text;
    existing_client_message_id text;
    existing_sender_actor_id text;
    existing_message_type text;
    existing_status text;
    existing_text text;
    existing_ext_info text;
    existing_created_at timestamptz;
    existing_message_revision bigint;
    existing_last_event_sequence bigint;
    existing_last_event_position bigint;
    existing_projection_revision bigint;
BEGIN
    IF pg_catalog.current_setting('wanwork.tenant_id', true) IS DISTINCT FROM p_tenant_id OR
       p_projection_id <> 'messages-v1' OR
       p_expected_sequence < 0 OR p_expected_global_position < 0 OR p_expected_revision < 0 OR
       p_next_sequence <> p_expected_sequence + 1 OR
       p_next_revision <> p_expected_revision + 1 OR
       p_next_global_position <= p_expected_global_position OR
       p_next_sequence <= 0 OR p_next_global_position <= 0 OR p_next_revision <= 0 OR
       p_message_revision <= 0 OR p_last_event_sequence <> p_next_sequence OR
       p_last_event_position <> p_next_global_position OR
       p_projection_revision <> p_next_revision OR
       octet_length(p_tenant_id) NOT BETWEEN 5 AND 128 OR
       octet_length(p_conversation_id) NOT BETWEEN 1 AND 256 OR
       octet_length(p_message_id) NOT BETWEEN 1 AND 256 OR
       octet_length(p_client_message_id) NOT BETWEEN 1 AND 256 OR
       octet_length(p_sender_actor_id) NOT BETWEEN 1 AND 256 OR
       p_message_type NOT IN ('text', 'system') OR
       p_status NOT IN ('active', 'edited', 'recalled') OR
       octet_length(p_text) > 65536 OR octet_length(p_ext_info) > 65536 OR
       (p_status = 'recalled' AND p_text <> '') THEN
        RETURN false;
    END IF;

    SELECT heads.current_sequence, heads.current_global_position, heads.current_revision
    INTO current_sequence, current_global_position, current_revision
    FROM wanwork_im.message_projection_heads AS heads
    WHERE heads.tenant_id = p_tenant_id
      AND heads.workspace_id = p_workspace_id
      AND heads.conversation_id = p_conversation_id
      AND heads.projection_id = p_projection_id
    FOR UPDATE;

    IF NOT FOUND THEN
        IF p_expected_sequence <> 0 OR p_expected_global_position <> 0 OR p_expected_revision <> 0 THEN
            RETURN false;
        END IF;
        INSERT INTO wanwork_im.message_projection_heads (
            tenant_id, workspace_id, conversation_id, projection_id,
            current_sequence, current_global_position, current_revision
        ) VALUES (
            p_tenant_id, p_workspace_id, p_conversation_id, p_projection_id,
            p_next_sequence, p_next_global_position, p_next_revision
        );
    ELSIF current_sequence = p_next_sequence AND
          current_global_position = p_next_global_position AND
          current_revision = p_next_revision THEN
        NULL;
    ELSIF current_sequence <> p_expected_sequence OR
          current_global_position <> p_expected_global_position OR
          current_revision <> p_expected_revision THEN
        RETURN false;
    ELSE
        UPDATE wanwork_im.message_projection_heads AS heads
        SET current_sequence = p_next_sequence,
            current_global_position = p_next_global_position,
            current_revision = p_next_revision,
            updated_at = clock_timestamp()
        WHERE heads.tenant_id = p_tenant_id
          AND heads.workspace_id = p_workspace_id
          AND heads.conversation_id = p_conversation_id
          AND heads.projection_id = p_projection_id
          AND heads.current_sequence = p_expected_sequence
          AND heads.current_global_position = p_expected_global_position
          AND heads.current_revision = p_expected_revision;
        GET DIAGNOSTICS changed_rows = ROW_COUNT;
        IF changed_rows <> 1 THEN
            RETURN false;
        END IF;
    END IF;

    BEGIN
        INSERT INTO wanwork_im.message_snapshots (
            tenant_id, workspace_id, conversation_id, message_id,
            client_message_id, sender_actor_id, message_type, status,
            text, ext_info, created_at, revision, last_event_sequence,
            last_event_position, projection_revision
        ) VALUES (
            p_tenant_id, p_workspace_id, p_conversation_id, p_message_id,
            p_client_message_id, p_sender_actor_id, p_message_type, p_status,
            p_text, p_ext_info, p_created_at, p_message_revision,
            p_last_event_sequence, p_last_event_position, p_projection_revision
        );
        RETURN true;
    EXCEPTION WHEN unique_violation THEN
        NULL;
    END;

    SELECT snapshot.workspace_id, snapshot.client_message_id, snapshot.sender_actor_id,
           snapshot.message_type, snapshot.status, snapshot.text, snapshot.ext_info,
           snapshot.created_at, snapshot.revision, snapshot.last_event_sequence,
           snapshot.last_event_position, snapshot.projection_revision
    INTO existing_workspace_id, existing_client_message_id, existing_sender_actor_id,
         existing_message_type, existing_status, existing_text, existing_ext_info,
         existing_created_at, existing_message_revision, existing_last_event_sequence,
         existing_last_event_position, existing_projection_revision
    FROM wanwork_im.message_snapshots AS snapshot
    WHERE snapshot.tenant_id = p_tenant_id
      AND snapshot.conversation_id = p_conversation_id
      AND snapshot.message_id = p_message_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF existing_workspace_id = p_workspace_id AND
       existing_client_message_id = p_client_message_id AND
       existing_sender_actor_id = p_sender_actor_id AND
       existing_message_type = p_message_type AND
       existing_status = p_status AND
       existing_text = p_text AND
       existing_ext_info = p_ext_info AND
       existing_created_at = p_created_at AND
       existing_message_revision = p_message_revision AND
       existing_last_event_sequence = p_last_event_sequence AND
       existing_last_event_position = p_last_event_position AND
       existing_projection_revision = p_projection_revision THEN
        RETURN true;
    END IF;
    IF existing_workspace_id <> p_workspace_id OR
       existing_last_event_sequence >= p_last_event_sequence OR
       existing_message_revision + 1 <> p_message_revision THEN
        RETURN false;
    END IF;

    UPDATE wanwork_im.message_snapshots AS snapshot
    SET workspace_id = p_workspace_id,
        client_message_id = p_client_message_id,
        sender_actor_id = p_sender_actor_id,
        message_type = p_message_type,
        status = p_status,
        text = p_text,
        ext_info = p_ext_info,
        created_at = p_created_at,
        revision = p_message_revision,
        last_event_sequence = p_last_event_sequence,
        last_event_position = p_last_event_position,
        projection_revision = p_projection_revision
    WHERE snapshot.tenant_id = p_tenant_id
      AND snapshot.conversation_id = p_conversation_id
      AND snapshot.message_id = p_message_id
      AND snapshot.revision = existing_message_revision
      AND snapshot.last_event_sequence = existing_last_event_sequence;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    RETURN changed_rows = 1;
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.write_message_projection(
    text, text, text, text, bigint, bigint, bigint, bigint, bigint, bigint,
    text, text, text, text, text, text, text, timestamptz, bigint, bigint, bigint, bigint
) FROM PUBLIC;

CREATE FUNCTION wanwork_im.advance_message_projection_head(
    p_tenant_id text,
    p_workspace_id text,
    p_conversation_id text,
    p_projection_id text,
    p_expected_sequence bigint,
    p_expected_global_position bigint,
    p_expected_revision bigint,
    p_next_sequence bigint,
    p_next_global_position bigint,
    p_next_revision bigint
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
    current_sequence bigint;
    current_global_position bigint;
    current_revision bigint;
BEGIN
    IF pg_catalog.current_setting('wanwork.tenant_id', true) IS DISTINCT FROM p_tenant_id OR
       p_projection_id <> 'messages-v1' OR
       p_expected_sequence < 0 OR p_expected_global_position < 0 OR p_expected_revision < 0 OR
       p_next_sequence <> p_expected_sequence + 1 OR
       p_next_revision <> p_expected_revision + 1 OR
       p_next_global_position <= p_expected_global_position OR
       p_next_sequence <= 0 OR p_next_global_position <= 0 OR p_next_revision <= 0 THEN
        RETURN false;
    END IF;

    SELECT heads.current_sequence, heads.current_global_position, heads.current_revision
    INTO current_sequence, current_global_position, current_revision
    FROM wanwork_im.message_projection_heads AS heads
    WHERE heads.tenant_id = p_tenant_id
      AND heads.workspace_id = p_workspace_id
      AND heads.conversation_id = p_conversation_id
      AND heads.projection_id = p_projection_id
    FOR UPDATE;

    IF NOT FOUND THEN
        IF p_expected_sequence <> 0 OR p_expected_global_position <> 0 OR p_expected_revision <> 0 THEN
            RETURN false;
        END IF;
        INSERT INTO wanwork_im.message_projection_heads (
            tenant_id, workspace_id, conversation_id, projection_id,
            current_sequence, current_global_position, current_revision
        ) VALUES (
            p_tenant_id, p_workspace_id, p_conversation_id, p_projection_id,
            p_next_sequence, p_next_global_position, p_next_revision
        );
        RETURN true;
    END IF;
    IF current_sequence = p_next_sequence AND
       current_global_position = p_next_global_position AND
       current_revision = p_next_revision THEN
        RETURN true;
    END IF;
    IF current_sequence <> p_expected_sequence OR
       current_global_position <> p_expected_global_position OR
       current_revision <> p_expected_revision THEN
        RETURN false;
    END IF;
    UPDATE wanwork_im.message_projection_heads AS heads
    SET current_sequence = p_next_sequence,
        current_global_position = p_next_global_position,
        current_revision = p_next_revision,
        updated_at = clock_timestamp()
    WHERE heads.tenant_id = p_tenant_id
      AND heads.workspace_id = p_workspace_id
      AND heads.conversation_id = p_conversation_id
      AND heads.projection_id = p_projection_id
      AND heads.current_sequence = p_expected_sequence
      AND heads.current_global_position = p_expected_global_position
      AND heads.current_revision = p_expected_revision;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    RETURN changed_rows = 1;
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.advance_message_projection_head(
    text, text, text, text, bigint, bigint, bigint, bigint, bigint, bigint
) FROM PUBLIC;
