# Security policy

## Supported versions

Quantum Entanglement is currently a pre-production project. No `0.1.x` build is supported
for untrusted, multi-tenant, or internet-facing workloads. Security fixes are made on
`main`; a formal support window starts only when a release publishes one in its release
evidence.

## Reporting a vulnerability

Use the private repository's GitHub Security Advisory flow. Do not open a public issue
containing exploit details, credentials, customer data, private endpoints, or message
history.

Include only the minimum evidence needed to reproduce the issue:

- affected commit or version;
- threat actor prerequisites and trust boundary crossed;
- deterministic reproduction steps using synthetic data;
- confidentiality, integrity, availability, tenant, and external-action impact;
- suggested containment, if known.

Never attach a live API key. Identify a credential only by provider, short prefix, length,
and a truncated SHA-256 fingerprint. Rotate any credential that may have been exposed
before sharing a redacted reproduction.

## Severity and response

Severity and release blocking follow `docs/production/RELEASE_GATES.md`. In particular,
credential exposure, tenant escape, data loss, and unauthorized irreversible effects are
P0 and block every release. A report is not considered resolved until an exploit
regression test, the fix, affected-version analysis, and operator guidance are recorded.

## Security boundaries

The current repository does not yet provide a production authentication perimeter,
tenant-complete storage isolation, or a fully verified capability chain. The exact gaps
are tracked in `docs/production/READINESS_AUDIT.md`.

Repository tests and demos must use synthetic identities, fake connectors, and disposable
data. They must not send, reply, comment, mention, or upload content to Feishu or WeCom.
Introducing a real outbound connector requires a separate explicit authorization,
action-time policy enforcement, idempotency receipts, an allowlist, and an audit trail.

## Disclosure and retained evidence

Fix commits should not contain weaponized secrets or private production data. Release
evidence may record sanitized commands, test identifiers, versions, pass/fail counts, and
artifact digests; it must not record raw tokens, cookies, authorization headers, or secret
environment values.
