CREATE TABLE IF NOT EXISTS invocation_jobs (
    invocation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50,
    status TEXT NOT NULL,
    max_attempts INTEGER NOT NULL,
    attempts_started INTEGER NOT NULL DEFAULT 0,
    lease_epoch INTEGER NOT NULL DEFAULT 0,
    requested_available_at TEXT,
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_token_digest TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    result_ref TEXT,
    last_error TEXT,
    finished_at TEXT,
    UNIQUE(session_id, task_id),
    UNIQUE(session_id, idempotency_key),
    UNIQUE(lease_token_digest),
    CHECK(priority BETWEEN 0 AND 100),
    CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'canceled')),
    CHECK(max_attempts > 0),
    CHECK(attempts_started >= 0 AND attempts_started <= max_attempts),
    CHECK(lease_epoch >= attempts_started),
    CHECK(
        (status = 'running'
            AND lease_owner IS NOT NULL
            AND lease_token_digest IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND heartbeat_at IS NOT NULL)
        OR
        (status <> 'running'
            AND lease_owner IS NULL
            AND lease_token_digest IS NULL
            AND lease_expires_at IS NULL
            AND heartbeat_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_invocation_jobs_claim
    ON invocation_jobs(status, available_at, priority DESC, created_at, invocation_id);
CREATE INDEX IF NOT EXISTS idx_invocation_jobs_session
    ON invocation_jobs(session_id, status, task_id);
CREATE INDEX IF NOT EXISTS idx_invocation_jobs_lease_expiry
    ON invocation_jobs(status, lease_expires_at, invocation_id);

CREATE TABLE IF NOT EXISTS invocation_attempts (
    attempt_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    lease_epoch INTEGER NOT NULL,
    worker_id TEXT NOT NULL,
    lease_token_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT,
    result_ref TEXT,
    UNIQUE(invocation_id, attempt_number),
    UNIQUE(invocation_id, lease_epoch),
    FOREIGN KEY(invocation_id)
        REFERENCES invocation_jobs(invocation_id) ON DELETE RESTRICT,
    CHECK(attempt_number > 0),
    CHECK(lease_epoch > 0),
    CHECK(status IN ('running', 'succeeded', 'failed', 'expired', 'canceled')),
    CHECK(
        (status = 'running' AND finished_at IS NULL)
        OR (status <> 'running' AND finished_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_invocation_attempts_job
    ON invocation_attempts(invocation_id, attempt_number);
CREATE INDEX IF NOT EXISTS idx_invocation_attempts_status
    ON invocation_attempts(status, lease_expires_at, attempt_id);
