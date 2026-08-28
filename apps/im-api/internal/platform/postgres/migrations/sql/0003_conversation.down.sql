DROP POLICY provider_conversation_snapshots_exact_tenant
    ON wanwork_im.provider_conversation_binding_snapshots;
DROP POLICY provider_conversation_heads_exact_tenant
    ON wanwork_im.provider_conversation_binding_heads;
DROP POLICY conversation_snapshots_exact_tenant
    ON wanwork_im.conversation_snapshots;
DROP POLICY conversation_heads_exact_tenant ON wanwork_im.conversation_heads;

ALTER TABLE wanwork_im.provider_conversation_binding_heads
    DROP CONSTRAINT provider_conversation_heads_current_snapshot_fk;
DROP TABLE wanwork_im.provider_conversation_binding_snapshots;
DROP TABLE wanwork_im.provider_conversation_binding_heads;

ALTER TABLE wanwork_im.conversation_heads
    DROP CONSTRAINT conversation_heads_current_snapshot_fk;
DROP TABLE wanwork_im.conversation_snapshots;
DROP TABLE wanwork_im.conversation_heads;
