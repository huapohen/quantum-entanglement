# Offline dependency-risk promotion contract

Status: versioned policy/result contracts and strict offline verifier implemented;
**promotion intentionally disabled and no real scan pass claimed**.

This stage defines how vulnerability and license evidence must be bound to one exact
Quantum Entanglement package candidate before a future release system may treat it as a
promotion gate. It does not choose a scanner, advisory database supplier, legal license
policy, or CI publication workflow.

The committed policy at `requirements/dependency-risk-policy.json` has:

- `promotionEnabled: false`;
- no approved scanner identity;
- no approved database snapshot;
- no legally approved SPDX expression.

Consequently, the repository's current configuration cannot return a promotion decision,
even if an arbitrary result claims zero vulnerabilities. Enabling the policy requires a
separate reviewed policy decision and real evidence described below.

## Purpose and non-claims

The verifier answers this narrow question:

> Does this canonical offline result completely cover the exact components and artifact
> hashes derived from the reverified source, packages, locks, and SBOMs; use the exact
> approved scanner and database snapshot; remain fresh at an independently supplied
> evaluation time; and satisfy the vulnerability, license, and exception policy?

It does not prove:

- that an unselected scanner is correct or complete;
- that an advisory database producer is trustworthy;
- that a database snapshot has not omitted an upstream advisory before approval;
- that a license expression or allowlist is legal advice;
- that dependency artifacts are free of malware;
- that optional `langgraph`, the Python interpreter, OS packages, or a deployment image are
  covered;
- that a result was produced by CI, signed, retained, or reviewed by a release approver.

Digest equality proves identity with approved bytes. It does not create trust in bytes that
were never independently reviewed or authenticated.

## Versioned promotion policy

The canonical JSON policy uses format
`quantum-entanglement.dependency-risk-policy`, schema version `1`. Unknown or missing
fields, duplicate keys, noncanonical JSON, unsafe types, oversized values, symlinks, and
unstable reads fail closed.

### Evidence policy

`evidence` defines:

- exact approved scanners by `name`, `version`, and lowercase SHA-256 of the scanner
  executable or reviewed immutable scanner artifact;
- exact approved database snapshots by canonical HTTPS source, revision, and snapshot
  SHA-256;
- maximum database age at promotion time;
- maximum validity interval a database producer may assert;
- maximum result age at promotion time.

Approving only a database URL is insufficient: an attacker could otherwise select an old
or deliberately reduced snapshot from that source. The policy therefore pins the exact
snapshot digest and revision. Scanner name/version alone is also insufficient, so the
scanner digest is mandatory.

### Vulnerability policy

The policy has two severity thresholds:

- `blockAtOrAbove`: findings at or above this severity are denied regardless of fix;
- `blockWhenFixAvailableAtOrAbove`: findings at or above this stricter threshold are denied
  when a fixed version is available.

The parser enforces a production minimum: the unconditional threshold cannot be weaker
than `high`, and the fix-available threshold cannot be weaker than `medium`. A policy may
be stricter.

Every finding records severity as `low | medium | high | critical | unknown` and fix status
as `available | none | unknown`. `available` requires a non-empty, sorted, unique fixed
version list; `none` and `unknown` require an empty list. The verifier always rejects
unknown severity or unknown fix status. It never collapses "not known" into "no fix".

### License policy

`licenses.allowedExpressions` is a sorted, unique, exact allowlist of canonical SPDX-style
expressions. The parser validates bounded identifiers, parentheses, `AND`, `OR`, and `WITH`
structure and rejects noncanonical spacing, `NONE`, and `NOASSERTION`.

Scanner output represents license state as exactly one of:

```json
{"expression":"MIT","status":"known"}
{"expression":null,"status":"unknown"}
```

An unknown or unlisted expression is denied unless one exact, current exception covers the
canonical license finding. Selecting the production allowlist requires qualified legal
review; this code does not supply that judgment.

### Exact exceptions

Each exception is a reviewed policy record, not scanner-controlled text. It binds:

- one exception ID;
- `vulnerability` or `license` kind;
- one exact versioned PyPI purl;
- one exact vulnerability ID or exact/null license expression;
- SHA-256 of the canonical finding, including purl, full artifact-hash set, severity, fix
  state, aliases/fixed versions, or license observation;
- exact approved database snapshot SHA-256;
- accountable owner and minimum-length rationale;
- second-resolution UTC issue and expiry times.

The maximum exception duration is policy-bounded. Wildcards, package-only scopes, version
ranges, duplicate scopes, future exceptions, expired exceptions, wrong-database
exceptions, mismatched fingerprints, and unused exceptions fail closed. Every denial must
consume exactly one current exception, one exception cannot be reused for a second denial,
and every committed exception must be consumed.

Exception owner, rationale, ID, component, and finding ID are never printed by the CLI.

## Versioned scanner result

The canonical result uses format `quantum-entanglement.dependency-risk-result`, schema
version `1`. Its maximum encoded size is 16 MiB. It binds all of the following:

1. project name/version and full source commit/tree SHA;
2. exact wheel and sdist filename, kind, byte size, and SHA-256;
3. exact distribution-manifest byte size and SHA-256;
4. lock-policy SHA-256, derived lock-inventory SHA-256, target/package counts, and every
   scope/Python/platform input/lock SHA-256;
5. exact runtime/build SBOM filename, kind, byte size, and SHA-256;
6. exact promotion-policy SHA-256;
7. scanner name/version/artifact SHA-256;
8. database source, revision, filename, byte size, SHA-256, fetched/expiry times, and
   integrity status;
9. scan completion time, top-level completion status, and the full sorted component list.

Each component record contains its exact versioned purl, the sorted set of artifact hashes
covered by the scan, component completion status, license observation, and sorted
vulnerability records. Finding IDs and aliases cannot collide within a component. Duplicate
components, duplicate findings, inconsistent fix status, noncanonical purls, unknown
fields, and unbounded collections are rejected.

`partial` and `error` are representable so external producers can retain honest failure
evidence, but they can never produce a promotion decision.

## Authoritative component coverage

The result is not trusted to declare its own universe. Before policy evaluation,
`scripts/verify_dependency_risk.py`:

1. strictly reverifies the distribution manifest against the clean checkout, packages, and
   independently supplied full commit SHA;
2. strictly reverifies the canonical lock policy and all four lock targets;
3. captures the exact committed-checkout promotion-policy bytes in the evidence context;
4. regenerates expected runtime/build SBOM bytes from that source/manifest/lock evidence;
5. byte-verifies the exact two-file SBOM directory;
6. derives the component universe and expected artifact hashes from those reverified SBOMs;
7. repeats manifest, lock, policy-byte, and SBOM verification after context collection.

The result's policy digest must match both that source context and the independently loaded
policy used for the decision. A policy file changed after source-context collection cannot
be substituted merely by placing its new digest in a scanner result.

The required universe currently consists of the runtime root component plus every build
SBOM component. Runtime artifact coverage is the exact wheel/sdist digest set. Each locked
component's coverage is the complete sorted artifact-hash set recorded in the build SBOM.
The scanner result must match every purl and every hash exactly. Missing, extra, duplicate,
same-name/different-version, or partial components fail closed.

This artifact-set claim is stronger than version-only metadata, but remains conditional on
the chosen scanner actually evaluating every listed artifact. A future scanner adapter must
document and test that behavior rather than copying hashes into a result without analysis.

## Offline snapshot and freshness

The database snapshot is a separate stable regular file outside the checkout, limited to
64 MiB by this contract. Its basename, byte size, and SHA-256 must match the result and the
policy-approved snapshot. Integrity status must be `verified`.

Freshness is evaluated against a required canonical UTC `--evaluation-time`. That time must
come from the promotion orchestrator or another trusted release authority; using the
snapshot's own timestamp as "now" would let stale databases and waivers remain valid
forever.

The verifier rejects:

- database or result timestamps from the future;
- evaluation at or after database expiry;
- database age beyond policy;
- database validity intervals longer than policy;
- result age beyond policy;
- scans completed before the database was fetched or at/after expiry.

## CLI contract

The result, SBOM directory, distribution manifest, and database snapshot must be created by
separate tooling outside the source checkout. The committed policy is always loaded from
the repository; the CLI does not accept an alternate policy path.

```bash
python scripts/verify_dependency_risk.py \
  --repository-root . \
  --distribution-directory dist \
  --distribution-manifest "$QE_DISTRIBUTION_MANIFEST_PATH" \
  --sbom-directory "$QE_SBOM_DIRECTORY" \
  --result "$QE_DEPENDENCY_RISK_RESULT" \
  --database-snapshot "$QE_RISK_DATABASE_SNAPSHOT" \
  --expected-commit "$(git rev-parse HEAD)" \
  --evaluation-time "2026-08-20T13:00:00Z"
```

Exit status:

- `0`: all evidence is exact, complete, current, policy-allowed, and promotion is enabled;
- `1`: fixed verification failure code;
- `2`: redacted command-line syntax failure.

Success emits one compact sorted JSON object containing only decision, evaluation time,
counts, and evidence digests. It omits filesystem paths, scanner/database names, purls,
vulnerability IDs, aliases, licenses, exception IDs, owners, and rationales. Verification
failure prints only `dependency risk verification failed: <fixed_code>`. Argument failure
does not echo supplied paths or values. Only an exact allowlist of registered failure codes
may cross the CLI boundary; an unregistered code or unexpected implementation exception is
collapsed to `risk_internal_error` without a traceback or exception text.

With the currently committed disabled policy, exit `0` is impossible by design.

## Tested adversarial cases

The deterministic unit suite covers:

- duplicate-key, noncanonical, unknown-field, invalid-type, unsafe purl, and bounded-input
  failures, including oversized integer literals that fail inside the JSON decoder;
- stale/overlong database windows and stale/future results;
- missing components and same-purl-name/different-version substitution;
- source, distribution manifest, lock inventory, SBOM, policy, scanner, database, and raw
  snapshot drift;
- partial top-level/component scans;
- unknown severity, unknown fix state, unknown/unlisted license;
- inconsistent fixed-version state and finding/alias collisions;
- expired, mismatched-fingerprint, overly broad, duplicate, reused, and unused exceptions;
- source-context policy mutation after collection;
- fixed CLI `0/1/2` behavior, exact failure-code allowlisting, and redacted unexpected
  failures.

These tests validate the contract and decision logic with synthetic evidence. They are not
a real dependency scan.

## Activation checklist and remaining blockers

Do not enable `promotionEnabled` until all boxes are satisfied in a reviewed commit:

- [ ] Select and security-review one scanner; pin its exact name, version, and immutable
      artifact digest.
- [ ] Select an advisory/license database producer and acquisition path; verify and approve
      one exact snapshot source, revision, bytes, and digest.
- [ ] Define snapshot authenticity, signature, update frequency, outage, rollback,
      compromise, and emergency-revocation procedures.
- [ ] Obtain qualified legal approval for the exact SPDX expression allowlist and exception
      workflow.
- [ ] Implement a scanner adapter that emits this canonical result and proves exact
      artifact-set coverage.
- [ ] Add optional runtime, Python, OS, container, and deployment dependency coverage for
      every supported topology.
- [ ] Add malware/maintainer-risk controls separately; absence of a known CVE is not proof
      of artifact safety.
- [ ] Connect generation and verification to CI in a separate commit, retain failed and
      successful evidence appropriately, and bind promotion to the verified result digest.
- [ ] Add signed provenance and deployment-time verification.
- [ ] Run a real candidate scan and retain immutable evidence and human promotion review.

The file reader defends against leaf symlinks, special files, size abuse, and mutation
during bounded reads. A stronger future evidence-packaging layer should open all related
files relative to stable directory file descriptors and atomically seal the exact bytes
that are uploaded, reducing parent-directory replacement and post-verification upload
races.

Until the checklist is complete, this stage is an executable fail-closed promotion
contract—not a vulnerability clearance, license approval, CI gate pass, or production
supply-chain completion claim.
