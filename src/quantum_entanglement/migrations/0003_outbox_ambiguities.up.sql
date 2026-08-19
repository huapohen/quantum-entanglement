PRAGMA secure_delete=ON;

-- The pre-migration publisher created this table directly, with a raw
-- lease_token column and no migration ledger entry. Creating that exact shape
-- when absent lets one rebuild cover both clean v2 databases and legacy ones.
CREATE TABLE IF NOT EXISTS outbox_ambiguities (
    message_id TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    marked_at TEXT NOT NULL,
    resolution TEXT,
    resolved_at TEXT,
    PRIMARY KEY(message_id, lease_token),
    FOREIGN KEY(message_id)
        REFERENCES outbox(message_id) ON DELETE RESTRICT,
    CHECK(reason_code IN (
        'callback_timeout', 'caller_cancelled',
        'ack_failed', 'lease_expired_after_accept'
    )),
    CHECK(resolution IS NULL OR resolution IN (
        'published', 'retry', 'dead_letter'
    )),
    CHECK(attempt_count > 0)
);

DROP INDEX IF EXISTS idx_outbox_ambiguities_open;
DROP INDEX IF EXISTS idx_outbox_ambiguities_one_open;
DROP INDEX IF EXISTS idx_outbox_ambiguities_opened;
ALTER TABLE outbox_ambiguities RENAME TO outbox_ambiguities_legacy_v3;

CREATE TABLE outbox_ambiguities (
    message_id TEXT NOT NULL,
    lease_token_digest TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    marked_at TEXT NOT NULL,
    resolution TEXT,
    resolved_at TEXT,
    PRIMARY KEY(message_id, lease_token_digest),
    FOREIGN KEY(message_id)
        REFERENCES outbox(message_id) ON DELETE RESTRICT,
    CHECK(
        length(lease_token_digest) = 64
        AND lease_token_digest NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK(reason_code IN (
        'callback_timeout', 'caller_cancelled',
        'ack_failed', 'lease_expired_after_accept'
    )),
    CHECK(resolution IS NULL OR resolution IN (
        'published', 'retry', 'dead_letter'
    )),
    CHECK(attempt_count > 0),
    CHECK(
        (resolution IS NULL AND resolved_at IS NULL)
        OR (resolution IS NOT NULL AND resolved_at IS NOT NULL)
    )
);

INSERT INTO outbox_ambiguities (
    message_id,
    lease_token_digest,
    reason_code,
    attempt_count,
    marked_at,
    resolution,
    resolved_at
)
SELECT
    message_id,
    qe_sha256(lease_token),
    reason_code,
    attempt_count,
    marked_at,
    resolution,
    resolved_at
FROM outbox_ambiguities_legacy_v3;

-- Terminal rows never need a live fencing capability. Older publisher builds
-- retained it after ACK/operator resolution, so scrub those plaintext values.
UPDATE outbox
SET lease_token = NULL
WHERE status IN ('published', 'dead_letter');

DROP TABLE outbox_ambiguities_legacy_v3;

CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_ambiguities_one_open
    ON outbox_ambiguities(message_id) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_outbox_ambiguities_opened
    ON outbox_ambiguities(resolved_at, marked_at, message_id);
