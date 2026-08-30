DROP POLICY provider_actor_snapshots_exact_tenant
    ON wanwork_im.provider_actor_binding_snapshots;
DROP POLICY provider_actor_heads_exact_tenant
    ON wanwork_im.provider_actor_binding_heads;
DROP POLICY tenant_membership_snapshots_exact_tenant
    ON wanwork_im.tenant_membership_snapshots;
DROP POLICY tenant_membership_heads_exact_tenant
    ON wanwork_im.tenant_membership_heads;
DROP POLICY actor_snapshots_exact_tenant ON wanwork_im.actor_snapshots;
DROP POLICY actor_heads_exact_tenant ON wanwork_im.actor_heads;

ALTER TABLE wanwork_im.provider_actor_binding_heads
    DROP CONSTRAINT provider_actor_heads_current_snapshot_fk;
DROP TABLE wanwork_im.provider_actor_binding_snapshots;
DROP TABLE wanwork_im.provider_actor_binding_heads;

ALTER TABLE wanwork_im.tenant_membership_heads
    DROP CONSTRAINT tenant_membership_heads_current_snapshot_fk;
DROP TABLE wanwork_im.tenant_membership_snapshots;
DROP TABLE wanwork_im.tenant_membership_heads;

ALTER TABLE wanwork_im.actor_heads
    DROP CONSTRAINT actor_heads_current_snapshot_fk;
DROP TABLE wanwork_im.actor_snapshots;
DROP TABLE wanwork_im.actor_heads;

ALTER TABLE wanwork_im.human_identity_binding_heads
    DROP CONSTRAINT human_identity_heads_current_snapshot_fk;
DROP TABLE wanwork_im.human_identity_binding_snapshots;
DROP TABLE wanwork_im.human_identity_binding_heads;

ALTER TABLE wanwork_im.human_principal_heads
    DROP CONSTRAINT human_principal_heads_current_snapshot_fk;
DROP TABLE wanwork_im.human_principal_snapshots;
DROP TABLE wanwork_im.human_principal_heads;
