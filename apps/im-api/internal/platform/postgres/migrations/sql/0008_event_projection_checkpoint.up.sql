CREATE TABLE wanwork_im.event_projection_checkpoints (
    tenant_id text COLLATE "C" NOT NULL,
    workspace_id text COLLATE "C" NOT NULL,
    projection_id text COLLATE "C" NOT NULL,
    global_position bigint NOT NULL DEFAULT '0'::bigint,
    cursor text COLLATE "C" NOT NULL DEFAULT '',
    last_event_id text COLLATE "C" NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, workspace_id, projection_id),
    CONSTRAINT event_projection_checkpoints_tenant_fk
        FOREIGN KEY (tenant_id)
        REFERENCES wanwork_im.tenants (tenant_id)
        ON DELETE RESTRICT,
    CONSTRAINT event_projection_checkpoints_workspace_check
        CHECK (
            workspace_id = ''
            OR (
                octet_length(workspace_id) BETWEEN 5 AND 128
                AND workspace_id ~ '^wsp_[A-Za-z0-9]([A-Za-z0-9_-]{0,122}[A-Za-z0-9])?$'
            )
        ),
    CONSTRAINT event_projection_checkpoints_projection_id_check
        CHECK (octet_length(projection_id) BETWEEN 1 AND 256),
    CONSTRAINT event_projection_checkpoints_position_check
        CHECK (global_position BETWEEN 0 AND 9223372036854775807),
    CONSTRAINT event_projection_checkpoints_cursor_check
        CHECK (octet_length(cursor) <= 4096),
    CONSTRAINT event_projection_checkpoints_event_id_check
        CHECK (octet_length(last_event_id) <= 256),
    CONSTRAINT event_projection_checkpoints_progress_shape_check
        CHECK (
            (global_position = 0 AND cursor = '' AND last_event_id = '')
            OR (global_position > 0 AND cursor <> '' AND last_event_id <> '')
        )
);

ALTER TABLE wanwork_im.event_projection_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE wanwork_im.event_projection_checkpoints FORCE ROW LEVEL SECURITY;
CREATE POLICY event_projection_checkpoints_exact_tenant
    ON wanwork_im.event_projection_checkpoints
    USING (tenant_id = current_setting('wanwork.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('wanwork.tenant_id', true));

CREATE FUNCTION wanwork_im.write_projection_checkpoint(
    p_tenant_id text,
    p_workspace_id text,
    p_projection_id text,
    p_expected_position bigint,
    p_expected_cursor text,
    p_expected_last_event_id text,
    p_next_position bigint,
    p_next_cursor text,
    p_next_last_event_id text
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
    IF p_expected_position < 0 OR p_next_position < 0 OR p_next_position < p_expected_position OR
       octet_length(p_expected_cursor) > 4096 OR octet_length(p_expected_last_event_id) > 256 OR
       octet_length(p_next_cursor) > 4096 OR octet_length(p_next_last_event_id) > 256 OR
       (p_expected_position = 0 AND (p_expected_cursor <> '' OR p_expected_last_event_id <> '')) OR
       (p_expected_position > 0 AND (p_expected_cursor = '' OR p_expected_last_event_id = '')) OR
       (p_next_position = 0 AND (p_next_cursor <> '' OR p_next_last_event_id <> '')) OR
       (p_next_position > 0 AND (p_next_cursor = '' OR p_next_last_event_id = '')) THEN
        RETURN false;
    END IF;

    UPDATE wanwork_im.event_projection_checkpoints
    SET global_position = p_next_position,
        cursor = p_next_cursor,
        last_event_id = p_next_last_event_id,
        updated_at = clock_timestamp()
    WHERE tenant_id = p_tenant_id
      AND workspace_id = p_workspace_id
      AND projection_id = p_projection_id
      AND global_position = p_expected_position
      AND cursor = p_expected_cursor
      AND last_event_id = p_expected_last_event_id;
    GET DIAGNOSTICS changed_rows = ROW_COUNT;
    IF changed_rows = 1 THEN
        RETURN true;
    END IF;

    IF p_expected_position = 0 THEN
        INSERT INTO wanwork_im.event_projection_checkpoints (
            tenant_id, workspace_id, projection_id,
            global_position, cursor, last_event_id
        ) VALUES (
            p_tenant_id, p_workspace_id, p_projection_id,
            p_next_position, p_next_cursor, p_next_last_event_id
        ) ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS changed_rows = ROW_COUNT;
        RETURN changed_rows = 1;
    END IF;
    RETURN false;
END
$function$;

REVOKE ALL ON FUNCTION wanwork_im.write_projection_checkpoint(
    text, text, text, bigint, text, text, bigint, text, text
) FROM PUBLIC;
