# Strict service configuration

`quantum_entanglement.service.ServiceConfig` is the fail-closed configuration contract for
the future service composition root. It is usable and tested now, but it does not create a
network listener or turn the kernel into a production service.

## Loading contract

The caller supplies an explicit immutable snapshot-like mapping to
`ServiceConfig.from_environment(...)`. The parser does not read `os.environ` itself and
does not retain the supplied mapping. It enumerates at most 4,096 keys, rejects missing or
unknown `QE_*` names before value access, then reads each allowlisted value exactly once
into a private snapshot. It never reads the values of unrelated host names or unknown
configuration names, so ambient credential values are not copied merely because the
caller supplied a full host mapping.

- every supported `QE_*` field is required; there are no production defaults;
- an unknown `QE_*` name fails closed without rendering its name or value;
- environment keys are bounded to 256 characters and reject empty or control-character
  forms before hashing, comparison, or value access;
- unrelated host variables are ignored without reading their values and never become
  configuration attributes;
- keys and values must be strings; values are bounded and reject whitespace, NUL and
  line breaks;
- booleans accept only lower-case `true` or `false`;
- integers accept canonical non-negative decimal without signs or leading zeroes;
- errors expose only a stable code and an allowlisted field name.

Passing a full host environment is discouraged even though unrelated names are ignored.
The service composition root must construct an allowlisted snapshot and must never inherit
ambient credentials into a child Agent or connector process.

## Version 1 fields

| Field | Required value or range | Security purpose |
|---|---|---|
| `QE_CONFIG_VERSION` | `1` | reject unknown configuration semantics |
| `QE_RUNTIME_MODE` | `development`, `test`, or `production` | select strict runtime policy; no implicit mode |
| `QE_DATA_DIR` | canonical absolute owner-only directory | bound durable service state |
| `QE_DATABASE_PATH` | canonical absolute direct child of `QE_DATA_DIR` | prevent memory/relative/out-of-tree databases |
| `QE_SECRET_ROOT` | canonical absolute owner-only directory, disjoint from data | prevent secrets entering backup/data scope |
| `QE_CONNECTOR` | `fake` | real outbound connectors are not permitted |
| `QE_BIND_HOST` | literal loopback IPv4 or IPv6 | DNS names and non-loopback listeners fail |
| `QE_BIND_PORT` | `1`–`65535` | bounded explicit listener intent |
| `QE_DEBUG` | `false` in production | prevent debug disclosure |
| `QE_MAX_REQUEST_BYTES` | `1024`–`16777216` | bound future request memory |
| `QE_MAX_CONCURRENCY` | `1`–`1024` | bound future admission |
| `QE_SHUTDOWN_GRACE_SECONDS` | `1`–`300` | bound future drain time |

The connector restriction is absolute for the current repository. `feishu`, `wecom`, a
plugin name, an empty value, or any unregistered value is rejected. This configuration
does not authorize sending, replying, commenting, mentioning, uploading or creating
content in any external messaging platform.

Version 1 deliberately has no authenticator, issuer, JWKS, OIDC, JWT, mTLS, session, or
membership-source fields. `RequestContextIssuer` receives an explicitly injected adapter,
authenticator identifier and audience in the current process-local foundation; that is a
testable composition boundary, not an operator-ready identity configuration. Do not
smuggle a token, cookie, client secret, key, or identity assertion into an existing `QE_*`
field. Adding a real identity provider requires a new reviewed configuration version,
opaque secret references, compatibility/rollback notes, and authenticated service tests.

## Filesystem preflight

The three configured paths must be canonical absolute paths. `:memory:`, relative paths,
`.`/`..`, redundant separators and missing directories are rejected.

At preflight time the implementation verifies:

- no existing path component is a symbolic link;
- no ancestor directory is attacker-replaceable: on POSIX every ancestor is owned by root or
  the effective service user, and group- or world-writable ancestors are rejected except when
  sticky semantics protect their entries (for example, root-owned `/tmp`);
- data and secret roots are owner-only directories owned by the effective service user;
- the secret root and data directory neither overlap nor contain each other;
- the database is a direct child of the data directory;
- an existing database is one owner-only regular file with link count one.

This check does not remove time-of-check/time-of-use risk. The component that opens the
database or secret must repeat identity, type, permission and ownership checks on its open
file descriptor. `FileSecretProvider` already does this for secret files. A future service
composition root must not declare readiness until the database open path provides the
same guarantee.

## Failure codes

Configuration failures are intentionally low-information. Representative codes include:

- `configuration_missing_field` / `configuration_unknown_field`;
- `configuration_snapshot_failed` / `configuration_snapshot_too_large`;
- `configuration_value_invalid` / `configuration_type_invalid`;
- `configuration_path_not_canonical` / `configuration_path_symlink`;
- `configuration_path_permissions` / `configuration_path_ancestor_permissions` /
  `configuration_path_ancestor_owner`;
- `database_outside_data_directory` / `database_link_count_unsafe`;
- `bind_host_not_literal_loopback` / `connector_not_permitted`;
- `production_debug_forbidden`.

Do not log the original environment value after a failure. An operator may record the code,
the allowlisted field returned by the exception, and the `ServiceConfig.fingerprint` only
after a configuration has validated.

## Promotion checklist

Before this contract can be used by a promoted service:

1. compose it into an entry point without adding lenient fallback values;
2. revalidate and securely open the database before any migration or business write;
3. load credential material only through a `SecretProvider`;
4. keep loopback binding until an independently reviewed TLS ingress is available;
5. retain negative tests for unknown fields, plaintext secret names, unsafe paths, broad
   listeners, debug and real connector names;
6. record the exact config version and redacted fingerprint in release evidence.

Changing a field meaning requires a new config version and a compatibility/migration note.
