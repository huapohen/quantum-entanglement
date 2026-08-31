REVOKE ALL ON FUNCTION wanwork_im.resolve_agent_provider_effect(
    text, text, text, text, text, text, timestamptz
) FROM PUBLIC;
DROP FUNCTION wanwork_im.resolve_agent_provider_effect(
    text, text, text, text, text, text, timestamptz
);

REVOKE ALL ON FUNCTION wanwork_im.mark_agent_provider_effect_terminal(
    text, text, text, text, text
) FROM PUBLIC;
DROP FUNCTION wanwork_im.mark_agent_provider_effect_terminal(
    text, text, text, text, text
);

REVOKE ALL ON FUNCTION wanwork_im.record_agent_provider_effect_receipt(
    text, text, text, text, text, text, text, timestamptz
) FROM PUBLIC;
DROP FUNCTION wanwork_im.record_agent_provider_effect_receipt(
    text, text, text, text, text, text, text, timestamptz
);

REVOKE ALL ON FUNCTION wanwork_im.claim_agent_provider_effect(
    text, text, bigint
) FROM PUBLIC;
DROP FUNCTION wanwork_im.claim_agent_provider_effect(
    text, text, bigint
);
