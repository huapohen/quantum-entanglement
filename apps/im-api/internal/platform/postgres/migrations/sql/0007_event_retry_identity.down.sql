DROP INDEX wanwork_im.event_log_scope_idempotency_key_uk;

ALTER TABLE wanwork_im.event_log
    DROP CONSTRAINT event_log_pkey;

ALTER TABLE wanwork_im.event_log
    ADD CONSTRAINT event_log_pkey
    PRIMARY KEY (tenant_id, event_id);
