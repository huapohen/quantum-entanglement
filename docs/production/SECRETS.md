# Secret handles and file-provider operations

Quantum Entanglement configuration stores references, never credential values. The current
implementation provides `SecretRef`, `SecretMaterial`, the `SecretProvider` port and a
hardened local `FileSecretProvider`. It does not yet provide KMS/Vault adapters or authorize
any real external connector.

## Data-flow boundary

```text
allowlisted configuration
  → SecretRef (provider route + locator, no value)
  → SecretProvider.resolve(ref)
  → one bounded SecretMaterial lease
  → trusted adapter consumes read-only memoryview
  → lease closes and owned buffer is overwritten
```

`SecretRef` accepts a canonical `scheme://locator` syntax with no query, fragment,
credentials, empty segment, `.` or `..`. Normal string and repr rendering contain only the
scheme and a 12-hex SHA-256 locator fingerprint. A trusted provider uses `.canonical`,
`.scheme` and `.locator` explicitly for routing.

`SecretMaterial`:

- accepts non-empty bytes no larger than 65,536 bytes;
- exposes a read-only `memoryview` only while its context is open;
- overwrites its owned `bytearray` on normal exit, exceptional exit or explicit `close()`;
- rejects reopen, copy, deepcopy and pickle;
- has redacted `str`/`repr` and is not implicitly JSON serializable.

Python cannot erase an immutable copy deliberately created by a consumer or a third-party
SDK. A trusted adapter must keep the view lifetime short, avoid converting it to `str`, and
must never place it in an exception, request model, event, artifact, metric or log field.

## File provider layout

The provider supports only direct-child references such as:

```text
file://service-signing-key
```

Nested locators are rejected. The configured root and files must be prepared as follows:

```text
/absolute/secret-root/       owner service UID, mode 0700
└── service-signing-key      owner service UID, mode 0600, regular file, link count 1
```

On each resolution the provider:

1. opens the root with `O_DIRECTORY`, `O_NOFOLLOW` and close-on-exec;
2. verifies the opened root is an owner-only directory owned by the effective UID;
3. opens the direct child relative to that directory with `O_NOFOLLOW` and non-blocking
   protection against FIFO/device hangs;
4. verifies regular-file type, owner-only mode, effective UID and link count one;
5. reads at most the configured bound and compares device, inode, mode, UID, size, mtime and
   ctime before and after the read;
6. transfers the owned buffer into one `SecretMaterial` lease.

Platforms missing `O_NOFOLLOW`, `O_DIRECTORY` or descriptor-relative `open` fail at provider
construction. Provider exceptions contain only a stable code and reference fingerprint;
raw path, locator, secret value and underlying OS error are suppressed.

## Rotation

Rotate one file without editing configuration:

1. create a new owner-only regular file in the same secure root;
2. write and fsync the new value without logging or printing it;
3. set mode 0600 and confirm owner/link count;
4. atomically rename it over the configured direct-child name;
5. resolve a fresh lease and perform the provider-specific health check;
6. revoke the old credential at its issuer after the overlap window;
7. record only provider, reference fingerprint, rotation time and outcome.

An already open lease continues to hold the old bytes until it closes. Every new resolution
reopens the path and sees the atomically replaced file. Never update a credential in place;
the before/after metadata guard is designed to reject concurrent mutation.

## Error handling

Stable provider codes include:

- `secret_scheme_unsupported` / `secret_locator_unsafe`;
- `secret_root_unsafe` / `secret_file_unsafe`;
- `secret_empty` / `secret_too_large`;
- `secret_changed_during_read` / `secret_unavailable`.

Do not retry `*_unsafe`, `*_empty` or `*_too_large` automatically. `secret_unavailable` may
be retried only with bounded backoff before readiness; a protected external effect must fail
closed if its required secret cannot be resolved.

## Prohibited handling

- no complete API key, token, cookie, OIDC credential or private key in environment-backed
  `QE_*` configuration, source, fixture, report, release evidence or ordinary reply;
- no credential value in event payloads, prompts, artifacts, action receipts or database
  columns;
- no raw `Authorization`, cookie, connector exception or `SecretMaterial` in logs/traces;
- no secret root inside the data/backup directory;
- no symlink, hard link, permissive mode, nested locator or ambient host-secret lookup;
- no committed example value that resembles a live credential.

When identifying an operator-provided credential, record only its provider, short prefix,
length and a short SHA-256 fingerprint. Never reproduce the complete value.

## Remaining production work

The file provider is a local single-node boundary, not a full secret-management system.
Production promotion still requires:

- a composition root that resolves only allowlisted references at the point of use;
- safe structured logging and canary scans covering errors, events and release evidence;
- provider timeout, readiness and rotation-overlap behavior;
- an independently reviewed KMS/Vault adapter when remote secret storage is selected;
- process isolation so Agent/plugin/connector code has no ambient access to the secret root;
- incident response and revocation rehearsal.

Real provider credentials, KMS/Vault access, network egress and irreversible rotation require
deployment-specific authority and are not exercised by repository tests.
