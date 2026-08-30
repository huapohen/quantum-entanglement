ALTER TABLE wanwork_im.agent_provider_effects
    ADD COLUMN provider_receipt_status text COLLATE "C";

ALTER TABLE wanwork_im.agent_provider_effects
    ADD COLUMN provider_receipt_observed_at timestamptz;

ALTER TABLE wanwork_im.agent_provider_effects
    ADD CONSTRAINT agent_provider_effects_receipt_evidence_shape_check
    CHECK (
        (
            provider_receipt_digest IS NULL
            AND provider_external_id IS NULL
            AND provider_receipt_status IS NULL
            AND provider_receipt_observed_at IS NULL
        )
        OR (
            provider_receipt_digest IS NOT NULL
            AND provider_external_id IS NOT NULL
            AND provider_receipt_status IN ('committed', 'replayed', 'unknown')
            AND provider_receipt_observed_at IS NOT NULL
        )
    );

ALTER TABLE wanwork_im.agent_provider_effects
    ADD CONSTRAINT agent_provider_effects_receipt_state_check
    CHECK (
        (status = 'committed' AND provider_receipt_status = 'committed')
        OR (status = 'replayed' AND provider_receipt_status = 'replayed')
        OR (status = 'unknown' AND (provider_receipt_status IS NULL OR provider_receipt_status = 'unknown'))
        OR (status IN ('queued', 'sent', 'failed') AND provider_receipt_status IS NULL)
    );

ALTER TABLE wanwork_im.agent_provider_effects
    ADD CONSTRAINT agent_provider_effects_receipt_time_check
    CHECK (
        provider_receipt_observed_at IS NULL
        OR provider_receipt_observed_at >= created_at
    );
