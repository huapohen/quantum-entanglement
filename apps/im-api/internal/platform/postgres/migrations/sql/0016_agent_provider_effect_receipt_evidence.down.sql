ALTER TABLE wanwork_im.agent_provider_effects
    DROP CONSTRAINT agent_provider_effects_receipt_time_check;

ALTER TABLE wanwork_im.agent_provider_effects
    DROP CONSTRAINT agent_provider_effects_receipt_state_check;

ALTER TABLE wanwork_im.agent_provider_effects
    DROP CONSTRAINT agent_provider_effects_receipt_evidence_shape_check;

ALTER TABLE wanwork_im.agent_provider_effects
    DROP COLUMN provider_receipt_observed_at;

ALTER TABLE wanwork_im.agent_provider_effects
    DROP COLUMN provider_receipt_status;
