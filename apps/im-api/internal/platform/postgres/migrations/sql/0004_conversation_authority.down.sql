DROP POLICY tenant_command_receipts_exact_tenant
    ON wanwork_im.tenant_command_receipts;
DROP POLICY conversation_access_snapshots_exact_tenant
    ON wanwork_im.conversation_access_snapshots;
DROP POLICY conversation_access_heads_exact_tenant
    ON wanwork_im.conversation_access_heads;
DROP POLICY conversation_membership_snapshots_exact_tenant
    ON wanwork_im.conversation_membership_snapshots;
DROP POLICY conversation_membership_heads_exact_tenant
    ON wanwork_im.conversation_membership_heads;

DROP TABLE wanwork_im.tenant_command_receipts;

ALTER TABLE wanwork_im.conversation_access_heads
    DROP CONSTRAINT conversation_access_heads_current_snapshot_fk;
DROP TABLE wanwork_im.conversation_access_snapshots;
DROP TABLE wanwork_im.conversation_access_heads;

ALTER TABLE wanwork_im.conversation_membership_heads
    DROP CONSTRAINT conversation_membership_heads_current_snapshot_fk;
DROP TABLE wanwork_im.conversation_membership_snapshots;
DROP TABLE wanwork_im.conversation_membership_heads;
