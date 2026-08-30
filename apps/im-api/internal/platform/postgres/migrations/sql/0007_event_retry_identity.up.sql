ALTER TABLE wanwork_im.event_log
    DROP CONSTRAINT event_log_pkey;

ALTER TABLE wanwork_im.event_log
    ADD CONSTRAINT event_log_pkey
    PRIMARY KEY (tenant_id, workspace_id, event_id);

CREATE UNIQUE INDEX event_log_scope_idempotency_key_uk
    ON wanwork_im.event_log (tenant_id, workspace_id, stream_id, idempotency_key)
    WHERE idempotency_key <> '';
