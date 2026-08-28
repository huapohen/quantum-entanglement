DROP FUNCTION wanwork_im.write_tenant_command_receipt(
    text, text, text, text, text
);

DROP FUNCTION wanwork_im.write_conversation_access_revision(
    text, text, text, bigint, bigint,
    boolean, boolean, boolean, boolean, boolean, boolean
);

DROP FUNCTION wanwork_im.write_conversation_membership_revision(
    text, text, text, bigint, bigint, text, text
);

DROP FUNCTION wanwork_im.write_provider_conversation_binding_revision(
    text, text, text, text, bigint, bigint, text, text
);

DROP FUNCTION wanwork_im.write_conversation_revision(
    text, text, bigint, bigint, text, text, text
);
