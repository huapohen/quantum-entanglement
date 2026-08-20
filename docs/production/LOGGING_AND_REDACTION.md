# Safe logging and redaction boundary

Operational logs are an output interface, not a debugging dump. Quantum Entanglement uses
fixed event schemas and typed fields so an exception, request, prompt, artifact, connector
payload or credential cannot become log text by convenience.

The current stage provides two layers:

1. `SafeLogger` admits only code-registered events and exact typed fields;
2. `Redactor` converts a bounded diagnostic structure to JSON-safe values as a final
   containment layer.

The outbox publisher has been migrated to `SafeLogger`. Other modules must be migrated and
covered by the canary gate before a service entry point may be promoted.

## Typed operational events

Every emitted event has a `LogEventSchema` fixed in source:

- event codes use a bounded `qe.<component>.<event>` grammar;
- severity is fixed by the schema, not request input;
- every field name, type and required/optional status is fixed;
- code-valued fields enumerate every permitted value in source;
- unknown events, missing fields, extra fields and wrong types produce only the constant
  `qe.logging.event_rejected` record;
- no API accepts a free-form message, exception, `exc_info`, payload or arbitrary `extra`.

Supported field kinds are intentionally narrow:

| Kind | Accepted value | Rendered value |
|---|---|---|
| `BOOLEAN` | exact `bool` | JSON boolean |
| `COUNT` | integer `0..2^63-1` | JSON integer |
| `DURATION_MS` | finite `0..86400000` | rounded JSON number |
| `CODE` | exact member of source allowlist | bounded code |
| `IDENTIFIER_HASH` | bounded non-control string | `sha256:<16 hex>` |
| `DIGEST` | canonical lower-case SHA-256 | complete digest |

Identifier hashing prevents accidental raw tenant/worker/message identifiers in logs. It is
not anonymization: a low-entropy identifier can be guessed, and the stable hash can correlate
records. Retention and access policy must still treat these logs as operational metadata.

Records are canonical single-line JSON passed to `logging.Logger.log()` with no formatting
arguments or exception info. A logging backend failure returns `False` and never changes a
business operation. Durable security audit is a separate future store and may deliberately
fail a protected action closed; ordinary operational logging must not be confused with that
audit guarantee.

## Diagnostic redactor

`Redactor` is for bounded diagnostics that cannot yet be expressed as a typed event. It:

- handles only exact JSON-like builtins; unknown objects are never `str`/`repr` rendered;
- renders exceptions by safe type name only, never by message or traceback;
- redacts authorization, cookie, password, secret, token, private key, lease token, headers,
  body, payload, prompt and artifact keys at every nested level;
- removes common inline Bearer/Basic credentials, URL userinfo, `sk-...` values and JWTs;
- replaces bytes/memoryviews rather than decoding them;
- replaces integers outside the signed 64-bit range rather than rendering unbounded text;
- detects cycles, reads only a bounded prefix of each container, and enforces depth, item and
  string bounds;
- returns the constant `<redaction-failed>` if containment itself raises;
- always produces values accepted by strict JSON serialization.

Redaction is defense in depth, not permission to log a request or prompt. A new operational
path should first add a typed schema with no text field.

## Publisher migration

Publisher error paths now emit fixed events such as:

- `qe.publisher.claim_failed` with hashed worker ID;
- `qe.publisher.ack_failed` and `ambiguity_persist_failed` with hashed message ID;
- fixed lease validation, retry clock/jitter and classifier failure events.

Publisher logs never include connector exception text or tracebacks. The public
`StoredOutboxMessage.to_dict()` and dataclass repr no longer expose the internal lease token.

Persisted outbox `last_error` is also constrained. The built-in classifier returns one of a
small source-defined code set. A custom classifier result is persisted only when it matches
an explicit constructor-time allowlist; it is never coerced with `str()`. A rejected or
throwing classifier falls back to `connector_failure` and emits a fixed operational event.

## Explicit prohibitions

Do not record any of the following in logs, traces, metrics, errors or release evidence:

- raw request/response bodies, prompt/context, artifact content or connector payload;
- Authorization, cookies, API keys, OIDC tokens, private keys or secret material;
- raw tenant, workspace, subject, worker, message, action or lease identifiers;
- internal lease/fencing tokens, approval proof or capability proof;
- connector, database, filesystem or provider exception messages;
- full filesystem paths, URLs with credentials, environment snapshots or config values;
- user-controlled text as event code, field name, code value, metric label or exception.

In particular, repository tests and diagnostics must never send, reply, comment, mention or
upload content to Feishu or WeCom. A log event cannot authorize an external action.

## Verification gate

For each migrated component retain tests that inject canaries into:

- nested keys and values;
- exception messages and custom exception/object rendering;
- identifiers, URLs, Authorization/cookie shapes, bytes and invalid Unicode/control input;
- cycles, depth, item count, string length and non-finite numbers;
- logger/redactor/classifier failure;
- missing, extra, incorrectly typed and unknown schema fields.

The gate must inspect captured log records, stderr, persisted error codes, serialized public
models and release evidence. A canary occurrence is P0 and blocks promotion.

Current deterministic commands are:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_redaction tests.test_safe_logging tests.test_publisher tests.test_delivery -v
ruff check src tests scripts
python3 -m compileall -q src tests scripts
git diff --check
```

## Remaining work

- migrate every module away from free-form `logger.*` calls;
- add request-context propagation containing hashed identifiers only;
- add a separate append-only durable security audit contract;
- scan all test/process output with nested canaries in clean release jobs;
- define log access, retention, deletion and incident-response policy;
- configure OpenTelemetry exporters with the same field/cardinality boundary;
- prove Agent/plugin/connector processes cannot access logging credentials or bypass the
  host logging port.

Until these are complete, safe publisher logs are a verified component, not a claim that the
whole service logging plane is production-ready.
