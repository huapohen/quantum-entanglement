ALTER TABLE wanwork_im.agent_installation_snapshots
    DROP CONSTRAINT agent_installation_snapshots_capabilities_values_check;

ALTER TABLE wanwork_im.agent_releases
    DROP CONSTRAINT agent_releases_capabilities_disjoint_check;

ALTER TABLE wanwork_im.agent_releases
    DROP CONSTRAINT agent_releases_prohibitions_values_check;

ALTER TABLE wanwork_im.agent_releases
    DROP CONSTRAINT agent_releases_requested_capabilities_values_check;
