ALTER TABLE wanwork_im.agent_installation_heads
    DROP CONSTRAINT agent_installation_heads_current_snapshot_fk;
DROP TABLE wanwork_im.agent_installation_snapshots;
DROP TABLE wanwork_im.agent_installation_heads;
DROP TABLE wanwork_im.agent_passports;
DROP TABLE wanwork_im.agent_releases;
DROP TABLE wanwork_im.agent_definitions;
