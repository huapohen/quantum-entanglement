ALTER TABLE wanwork_im.agent_releases
    ADD CONSTRAINT agent_releases_requested_capabilities_values_check
    CHECK (
        NOT pg_catalog.jsonb_path_exists(
            requested_capabilities,
            '$[*] ? (@.type() != "string" || !(@ like_regex "^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$"))'
        )
    );

ALTER TABLE wanwork_im.agent_releases
    ADD CONSTRAINT agent_releases_prohibitions_values_check
    CHECK (
        NOT pg_catalog.jsonb_path_exists(
            prohibitions,
            '$[*] ? (@.type() != "string" || !(@ like_regex "^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$"))'
        )
    );

ALTER TABLE wanwork_im.agent_releases
    ADD CONSTRAINT agent_releases_capabilities_disjoint_check
    CHECK (
        NOT pg_catalog.jsonb_path_exists(
            requested_capabilities,
            '$[*] ? (@ == $prohibitions[*])',
            pg_catalog.jsonb_build_object('prohibitions', prohibitions)
        )
    );

ALTER TABLE wanwork_im.agent_installation_snapshots
    ADD CONSTRAINT agent_installation_snapshots_capabilities_values_check
    CHECK (
        NOT pg_catalog.jsonb_path_exists(
            granted_capabilities,
            '$[*] ? (@.type() != "string" || !(@ like_regex "^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$"))'
        )
    );
