"""Canonical dependency-risk promotion policy and offline evidence primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from scripts.distribution_manifest import (
    DistributionManifestError,
    load_distribution_manifest,
    verify_distribution_manifest_file,
)
from scripts.sbom import (
    SbomError,
    generate_sbom_documents,
    validate_sbom_bytes,
    verify_sbom_directory,
)
from scripts.verify_dependency_locks import (
    DependencyLockError,
    LockTarget,
    verify_dependency_locks,
)

POLICY_FORMAT = "quantum-entanglement.dependency-risk-policy"
POLICY_SCHEMA_VERSION = 1
RESULT_FORMAT = "quantum-entanglement.dependency-risk-result"
RESULT_SCHEMA_VERSION = 1
DEFAULT_POLICY_PATH = Path("requirements/dependency-risk-policy.json")

_MAX_POLICY_BYTES = 1024 * 1024
_MAX_RESULT_BYTES = 16 * 1024 * 1024
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_DATABASE_BYTES = 64 * 1024 * 1024
_MAX_STRING_BYTES = 4096
_MAX_EXCEPTIONS = 256
_MAX_ALLOWED_IDENTITIES = 128
_MAX_ALLOWED_LICENSES = 256
_MAX_COMPONENTS = 512
_MAX_FINDINGS = 4096
_MAX_FINDINGS_PER_COMPONENT = 256
_MAX_HASHES_PER_COMPONENT = 512
_MAX_ALIASES = 64
_MAX_FIXED_VERSIONS = 64
_MAX_INTERVAL_SECONDS = 366 * 24 * 60 * 60
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_HASH_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.!+_-]{0,126}[A-Za-z0-9])?$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:@/+_-]{0,126}[A-Za-z0-9])?$")
_FINDING_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,126}[A-Za-z0-9])?$")
_TIMESTAMP_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z$")
_SPDX_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,126}$")
_SPDX_OPERATORS = frozenset({"AND", "OR", "WITH"})
_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
_RESULT_SEVERITIES = frozenset({*_SEVERITY_RANK, "unknown"})
_FIX_STATUSES = frozenset({"available", "none", "unknown"})
_SCAN_STATUSES = frozenset({"complete", "partial", "error"})
_INTEGRITY_STATUSES = frozenset({"verified", "unverified", "failed"})

_POLICY_KEYS = frozenset(
    {
        "evidence",
        "exceptions",
        "format",
        "licenses",
        "promotionEnabled",
        "schemaVersion",
        "vulnerabilities",
    }
)
_EVIDENCE_POLICY_KEYS = frozenset(
    {
        "approvedDatabases",
        "allowedScanners",
        "maximumDatabaseAgeSeconds",
        "maximumDatabaseValiditySeconds",
        "maximumResultAgeSeconds",
    }
)
_SCANNER_KEYS = frozenset({"name", "sha256", "version"})
_DATABASE_POLICY_KEYS = frozenset({"revision", "sha256", "source"})
_VULNERABILITY_POLICY_KEYS = frozenset({"blockAtOrAbove", "blockWhenFixAvailableAtOrAbove"})
_LICENSE_POLICY_KEYS = frozenset({"allowedExpressions"})
_EXCEPTION_POLICY_KEYS = frozenset({"maximumDurationSeconds", "minimumRationaleLength", "records"})
_VULNERABILITY_EXCEPTION_KEYS = frozenset(
    {
        "exceptionId",
        "databaseSha256",
        "expiresAt",
        "findingSha256",
        "findingId",
        "issuedAt",
        "kind",
        "owner",
        "purl",
        "rationale",
    }
)
_LICENSE_EXCEPTION_KEYS = frozenset(
    {
        "exceptionId",
        "databaseSha256",
        "expiresAt",
        "findingSha256",
        "issuedAt",
        "kind",
        "licenseExpression",
        "owner",
        "purl",
        "rationale",
    }
)
_RESULT_KEYS = frozenset(
    {
        "artifacts",
        "database",
        "distributionManifest",
        "format",
        "lockInventory",
        "project",
        "promotionPolicySha256",
        "sboms",
        "scan",
        "scanner",
        "schemaVersion",
        "source",
    }
)
_PROJECT_KEYS = frozenset({"name", "version"})
_SOURCE_KEYS = frozenset({"commitSha", "treeSha"})
_ARTIFACT_KEYS = frozenset({"byteSize", "filename", "kind", "sha256"})
_FILE_BINDING_KEYS = frozenset({"byteSize", "sha256"})
_LOCK_INVENTORY_KEYS = frozenset(
    {"inventorySha256", "lockPolicySha256", "packageRecordCount", "targetCount", "targets"}
)
_LOCK_TARGET_KEYS = frozenset({"inputSha256", "lockSha256", "platform", "pythonVersion", "scope"})
_SBOM_KEYS = frozenset({"byteSize", "filename", "kind", "sha256"})
_RESULT_SCANNER_KEYS = frozenset({"name", "sha256", "version"})
_DATABASE_KEYS = frozenset(
    {
        "byteSize",
        "expiresAt",
        "fetchedAt",
        "filename",
        "integrityStatus",
        "revision",
        "sha256",
        "source",
    }
)
_SCAN_KEYS = frozenset({"completedAt", "components", "status"})
_COMPONENT_SCAN_KEYS = frozenset(
    {"artifactSha256", "license", "purl", "scanStatus", "vulnerabilities"}
)
_LICENSE_OBSERVATION_KEYS = frozenset({"expression", "status"})
_VULNERABILITY_KEYS = frozenset({"aliases", "fixedVersions", "fixStatus", "id", "severity"})


class DependencyRiskError(ValueError):
    """A fixed-code dependency-risk failure safe for release logs."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise DependencyRiskError(code)


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase SHA-256 digest of exact evidence bytes."""

    return hashlib.sha256(value).hexdigest()


def canonical_json(value: object) -> bytes:
    """Encode the repository's bounded, deterministic JSON representation."""

    return (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True, separators=(",", ": "))
        + "\n"
    ).encode("ascii")


def _read_regular(path: Path, limit: int, code: str) -> bytes:
    try:
        before = path.lstat()
    except OSError:
        _fail(code)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        _fail(code)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail(code)
    try:
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode):
            _fail(code)
        if (opened_before.st_dev, opened_before.st_ino) != (before.st_dev, before.st_ino):
            _fail(code)
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65_536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                _fail(code)
        opened_after = os.fstat(descriptor)
    except OSError:
        _fail(code)
    finally:
        os.close(descriptor)
    identity_before = (
        opened_before.st_dev,
        opened_before.st_ino,
        opened_before.st_size,
        opened_before.st_mtime_ns,
        opened_before.st_ctime_ns,
    )
    identity_after = (
        opened_after.st_dev,
        opened_after.st_ino,
        opened_after.st_size,
        opened_after.st_mtime_ns,
        opened_after.st_ctime_ns,
    )
    if identity_before != identity_after or size != opened_after.st_size:
        _fail(code)
    try:
        after = path.lstat()
    except OSError:
        _fail(code)
    if (after.st_dev, after.st_ino) != (opened_after.st_dev, opened_after.st_ino):
        _fail(code)
    return b"".join(chunks)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("risk_json_invalid")
        result[key] = value
    return result


def _parse_canonical_json(value: bytes, *, limit: int, code: str) -> dict[str, object]:
    if not value or len(value) > limit:
        _fail(code)
    try:
        document = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _: _fail("risk_json_invalid"),
        )
    except (DependencyRiskError, json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        _fail("risk_json_invalid")
    if type(document) is not dict:
        _fail(code)
    result = cast(dict[str, object], document)
    if canonical_json(result) != value:
        _fail("risk_json_noncanonical")
    return result


def _safe_string(
    value: object,
    code: str,
    *,
    minimum: int = 1,
    maximum: int = _MAX_STRING_BYTES,
) -> str:
    if type(value) is not str:
        _fail(code)
    result = value
    try:
        encoded = result.encode("ascii")
    except UnicodeEncodeError:
        _fail(code)
    if len(encoded) < minimum or len(encoded) > maximum:
        _fail(code)
    if any(character < " " or character == "\x7f" for character in result):
        _fail(code)
    return result


def parse_timestamp(value: object, code: str) -> datetime:
    """Parse the contract's exact second-resolution UTC timestamp."""

    text = _safe_string(value, code, maximum=20)
    match = _TIMESTAMP_PATTERN.fullmatch(text)
    if match is None:
        _fail(code)
    try:
        parsed = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        _fail(code)
    return parsed.replace(tzinfo=timezone.utc)


def _positive_integer(value: object, code: str, *, maximum: int) -> int:
    if type(value) is not int or value <= 0 or value > maximum:
        _fail(code)
    return value


def _canonical_https_url(value: object, code: str) -> str:
    text = _safe_string(value, code, maximum=2048)
    try:
        parsed = urlsplit(text)
    except ValueError:
        _fail(code)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        _fail(code)
    hostname = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError:
        _fail(code)
    netloc = hostname if port in (None, 443) else f"{hostname}:{port}"
    normalized = urlunsplit(("https", netloc, parsed.path or "/", "", ""))
    if normalized != text:
        _fail(code)
    return text


def canonical_pypi_purl(value: object, code: str) -> str:
    """Validate the exact canonical purl shape emitted by the SBOM generator."""

    text = _safe_string(value, code, maximum=512)
    prefix = "pkg:pypi/"
    if not text.startswith(prefix) or text.count("@") != 1:
        _fail(code)
    encoded_name, encoded_version = text[len(prefix) :].split("@", 1)
    try:
        name = unquote(encoded_name, errors="strict")
        version = unquote(encoded_version, errors="strict")
    except (UnicodeDecodeError, ValueError):
        _fail(code)
    if _NAME_PATTERN.fullmatch(name) is None or _VERSION_PATTERN.fullmatch(version) is None:
        _fail(code)
    canonical = f"{prefix}{quote(name, safe='-._~')}@{quote(version, safe='-._~')}"
    if canonical != text:
        _fail(code)
    return text


def _tokenize_spdx(expression: str) -> tuple[str, ...]:
    tokens: list[str] = []
    offset = 0
    while offset < len(expression):
        character = expression[offset]
        if character == " ":
            offset += 1
            continue
        if character in "()":
            tokens.append(character)
            offset += 1
            continue
        end = offset
        while end < len(expression) and expression[end] not in " ()":
            end += 1
        token = expression[offset:end]
        if token not in _SPDX_OPERATORS and _SPDX_IDENTIFIER_PATTERN.fullmatch(token) is None:
            _fail("license_expression_invalid")
        tokens.append(token)
        offset = end
    if not tokens:
        _fail("license_expression_invalid")
    return tuple(tokens)


class _SpdxParser:
    def __init__(self, tokens: Sequence[str]) -> None:
        self._tokens = tokens
        self._offset = 0

    def _peek(self) -> str | None:
        if self._offset == len(self._tokens):
            return None
        return self._tokens[self._offset]

    def _take(self) -> str:
        token = self._peek()
        if token is None:
            _fail("license_expression_invalid")
        self._offset += 1
        return token

    def parse(self) -> None:
        self._parse_or()
        if self._peek() is not None:
            _fail("license_expression_invalid")

    def _parse_or(self) -> None:
        self._parse_and()
        while self._peek() == "OR":
            self._take()
            self._parse_and()

    def _parse_and(self) -> None:
        self._parse_with()
        while self._peek() == "AND":
            self._take()
            self._parse_with()

    def _parse_with(self) -> None:
        self._parse_primary()
        if self._peek() == "WITH":
            self._take()
            exception = self._take()
            if exception in _SPDX_OPERATORS or exception in ("(", ")"):
                _fail("license_expression_invalid")

    def _parse_primary(self) -> None:
        token = self._take()
        if token == "(":
            self._parse_or()
            if self._take() != ")":
                _fail("license_expression_invalid")
            return
        if token in _SPDX_OPERATORS or token == ")":
            _fail("license_expression_invalid")
        if token in ("NONE", "NOASSERTION"):
            _fail("license_expression_unknown")


def _render_spdx(tokens: Sequence[str]) -> str:
    rendered = ""
    for token in tokens:
        if token == "(":
            if rendered and not rendered.endswith((" ", "(")):
                rendered += " "
            rendered += token
        elif token == ")":
            rendered = rendered.rstrip() + token
        else:
            if rendered and not rendered.endswith("("):
                rendered += " "
            rendered += token
    return rendered


def canonical_license_expression(value: object, code: str) -> str:
    """Validate the supported canonical SPDX-expression grammar."""

    expression = _safe_string(value, code, maximum=1024)
    tokens = _tokenize_spdx(expression)
    _SpdxParser(tokens).parse()
    if _render_spdx(tokens) != expression:
        _fail("license_expression_noncanonical")
    return expression


@dataclass(frozen=True)
class ScannerIdentity:
    name: str
    version: str
    sha256: str


@dataclass(frozen=True)
class ApprovedDatabase:
    source: str
    revision: str
    sha256: str


@dataclass(frozen=True)
class RiskException:
    exception_id: str
    kind: str
    purl: str
    subject: str | None
    finding_sha256: str
    database_sha256: str
    owner: str
    rationale: str
    issued_at: datetime
    expires_at: datetime

    @property
    def scope(self) -> tuple[str, str, str | None]:
        return self.kind, self.purl, self.subject


@dataclass(frozen=True)
class DependencyRiskPolicy:
    promotion_enabled: bool
    allowed_scanners: tuple[ScannerIdentity, ...]
    approved_databases: tuple[ApprovedDatabase, ...]
    maximum_database_age_seconds: int
    maximum_database_validity_seconds: int
    maximum_result_age_seconds: int
    block_at_or_above: str
    block_when_fix_available_at_or_above: str
    allowed_license_expressions: tuple[str, ...]
    maximum_exception_duration_seconds: int
    minimum_rationale_length: int
    exceptions: tuple[RiskException, ...]
    sha256: str


def _scanner_identities(value: object) -> tuple[ScannerIdentity, ...]:
    if type(value) is not list or len(cast(list[object], value)) > _MAX_ALLOWED_IDENTITIES:
        _fail("risk_policy_scanner_invalid")
    identities: list[ScannerIdentity] = []
    for item in cast(list[object], value):
        if type(item) is not dict or frozenset(cast(dict[str, object], item)) != _SCANNER_KEYS:
            _fail("risk_policy_scanner_invalid")
        record = cast(dict[str, object], item)
        name = _safe_string(record["name"], "risk_policy_scanner_invalid", maximum=128)
        version = _safe_string(record["version"], "risk_policy_scanner_invalid", maximum=128)
        digest = _safe_string(record["sha256"], "risk_policy_scanner_invalid", maximum=64)
        if (
            _IDENTIFIER_PATTERN.fullmatch(name) is None
            or _VERSION_PATTERN.fullmatch(version) is None
            or _HASH_PATTERN.fullmatch(digest) is None
        ):
            _fail("risk_policy_scanner_invalid")
        identities.append(ScannerIdentity(name=name, version=version, sha256=digest))
    if identities != sorted(identities, key=lambda item: (item.name, item.version, item.sha256)):
        _fail("risk_policy_scanner_invalid")
    if len({(item.name, item.version, item.sha256) for item in identities}) != len(identities):
        _fail("risk_policy_scanner_invalid")
    return tuple(identities)


def _approved_databases(value: object) -> tuple[ApprovedDatabase, ...]:
    if type(value) is not list or len(cast(list[object], value)) > _MAX_ALLOWED_IDENTITIES:
        _fail("risk_policy_database_invalid")
    databases: list[ApprovedDatabase] = []
    for item in cast(list[object], value):
        if (
            type(item) is not dict
            or frozenset(cast(dict[str, object], item)) != _DATABASE_POLICY_KEYS
        ):
            _fail("risk_policy_database_invalid")
        record = cast(dict[str, object], item)
        source = _canonical_https_url(record["source"], "risk_policy_database_invalid")
        revision = _identifier(record["revision"], "risk_policy_database_invalid")
        digest = _safe_string(record["sha256"], "risk_policy_database_invalid", maximum=64)
        if _HASH_PATTERN.fullmatch(digest) is None:
            _fail("risk_policy_database_invalid")
        databases.append(ApprovedDatabase(source=source, revision=revision, sha256=digest))
    if databases != sorted(databases, key=lambda item: (item.source, item.revision, item.sha256)):
        _fail("risk_policy_database_invalid")
    if len({(item.source, item.revision, item.sha256) for item in databases}) != len(databases):
        _fail("risk_policy_database_invalid")
    return tuple(databases)


def _license_expressions(value: object) -> tuple[str, ...]:
    if type(value) is not list or len(cast(list[object], value)) > _MAX_ALLOWED_LICENSES:
        _fail("risk_policy_license_invalid")
    expressions = tuple(
        canonical_license_expression(item, "risk_policy_license_invalid")
        for item in cast(list[object], value)
    )
    if expressions != tuple(sorted(expressions)) or len(set(expressions)) != len(expressions):
        _fail("risk_policy_license_invalid")
    return expressions


def _identifier(value: object, code: str, *, finding: bool = False) -> str:
    text = _safe_string(value, code, maximum=128)
    pattern = _FINDING_ID_PATTERN if finding else _IDENTIFIER_PATTERN
    if pattern.fullmatch(text) is None:
        _fail(code)
    return text


def _risk_exceptions(
    value: object,
    *,
    maximum_duration_seconds: int,
    minimum_rationale_length: int,
) -> tuple[RiskException, ...]:
    if type(value) is not list or len(cast(list[object], value)) > _MAX_EXCEPTIONS:
        _fail("risk_policy_exception_invalid")
    exceptions: list[RiskException] = []
    for item in cast(list[object], value):
        if type(item) is not dict:
            _fail("risk_policy_exception_invalid")
        record = cast(dict[str, object], item)
        kind = record.get("kind")
        expected = (
            _VULNERABILITY_EXCEPTION_KEYS
            if kind == "vulnerability"
            else _LICENSE_EXCEPTION_KEYS
            if kind == "license"
            else frozenset()
        )
        if frozenset(record) != expected:
            _fail("risk_policy_exception_invalid")
        exception_id = _identifier(record["exceptionId"], "risk_policy_exception_invalid")
        purl = canonical_pypi_purl(record["purl"], "risk_policy_exception_invalid")
        finding_sha256 = _safe_string(
            record["findingSha256"], "risk_policy_exception_invalid", maximum=64
        )
        database_sha256 = _safe_string(
            record["databaseSha256"], "risk_policy_exception_invalid", maximum=64
        )
        if (
            _HASH_PATTERN.fullmatch(finding_sha256) is None
            or _HASH_PATTERN.fullmatch(database_sha256) is None
        ):
            _fail("risk_policy_exception_invalid")
        owner = _safe_string(
            record["owner"], "risk_policy_exception_owner_invalid", minimum=3, maximum=128
        )
        if _IDENTIFIER_PATTERN.fullmatch(owner) is None:
            _fail("risk_policy_exception_owner_invalid")
        rationale = _safe_string(
            record["rationale"],
            "risk_policy_exception_rationale_invalid",
            minimum=minimum_rationale_length,
            maximum=2048,
        )
        issued_at = parse_timestamp(record["issuedAt"], "risk_policy_exception_time_invalid")
        expires_at = parse_timestamp(record["expiresAt"], "risk_policy_exception_time_invalid")
        duration = int((expires_at - issued_at).total_seconds())
        if duration <= 0 or duration > maximum_duration_seconds:
            _fail("risk_policy_exception_time_invalid")
        if kind == "vulnerability":
            subject: str | None = _identifier(
                record["findingId"], "risk_policy_exception_invalid", finding=True
            )
        else:
            raw_expression = record["licenseExpression"]
            subject = (
                None
                if raw_expression is None
                else canonical_license_expression(raw_expression, "risk_policy_exception_invalid")
            )
        exceptions.append(
            RiskException(
                exception_id=exception_id,
                kind=cast(str, kind),
                purl=purl,
                subject=subject,
                finding_sha256=finding_sha256,
                database_sha256=database_sha256,
                owner=owner,
                rationale=rationale,
                issued_at=issued_at,
                expires_at=expires_at,
            )
        )
    if exceptions != sorted(exceptions, key=lambda item: item.exception_id):
        _fail("risk_policy_exception_invalid")
    if len({item.exception_id for item in exceptions}) != len(exceptions):
        _fail("risk_policy_exception_invalid")
    exact_scopes = {(item.database_sha256, item.finding_sha256, *item.scope) for item in exceptions}
    if len(exact_scopes) != len(exceptions):
        _fail("risk_policy_exception_scope_invalid")
    return tuple(exceptions)


def load_dependency_risk_policy_bytes(value: bytes) -> DependencyRiskPolicy:
    """Strictly parse and validate one canonical versioned promotion policy."""

    document = _parse_canonical_json(value, limit=_MAX_POLICY_BYTES, code="risk_policy_invalid")
    if frozenset(document) != _POLICY_KEYS:
        _fail("risk_policy_invalid")
    if (
        document["format"] != POLICY_FORMAT
        or type(document["schemaVersion"]) is not int
        or document["schemaVersion"] != POLICY_SCHEMA_VERSION
        or type(document["promotionEnabled"]) is not bool
    ):
        _fail("risk_policy_invalid")

    raw_evidence = document["evidence"]
    if type(raw_evidence) is not dict or frozenset(raw_evidence) != _EVIDENCE_POLICY_KEYS:
        _fail("risk_policy_evidence_invalid")
    evidence = cast(dict[str, object], raw_evidence)
    allowed_scanners = _scanner_identities(evidence["allowedScanners"])
    approved_databases = _approved_databases(evidence["approvedDatabases"])
    maximum_database_age = _positive_integer(
        evidence["maximumDatabaseAgeSeconds"],
        "risk_policy_evidence_invalid",
        maximum=_MAX_INTERVAL_SECONDS,
    )
    maximum_database_validity = _positive_integer(
        evidence["maximumDatabaseValiditySeconds"],
        "risk_policy_evidence_invalid",
        maximum=_MAX_INTERVAL_SECONDS,
    )
    maximum_result_age = _positive_integer(
        evidence["maximumResultAgeSeconds"],
        "risk_policy_evidence_invalid",
        maximum=_MAX_INTERVAL_SECONDS,
    )

    raw_vulnerabilities = document["vulnerabilities"]
    if (
        type(raw_vulnerabilities) is not dict
        or frozenset(raw_vulnerabilities) != _VULNERABILITY_POLICY_KEYS
    ):
        _fail("risk_policy_vulnerability_invalid")
    vulnerabilities = cast(dict[str, object], raw_vulnerabilities)
    block_at = vulnerabilities["blockAtOrAbove"]
    block_with_fix = vulnerabilities["blockWhenFixAvailableAtOrAbove"]
    if block_at not in _SEVERITY_RANK or block_with_fix not in _SEVERITY_RANK:
        _fail("risk_policy_vulnerability_invalid")
    block_at_text = cast(str, block_at)
    block_with_fix_text = cast(str, block_with_fix)
    if _SEVERITY_RANK[block_with_fix_text] > _SEVERITY_RANK[block_at_text]:
        _fail("risk_policy_vulnerability_invalid")
    if (
        _SEVERITY_RANK[block_at_text] > _SEVERITY_RANK["high"]
        or _SEVERITY_RANK[block_with_fix_text] > _SEVERITY_RANK["medium"]
    ):
        _fail("risk_policy_vulnerability_invalid")

    raw_licenses = document["licenses"]
    if type(raw_licenses) is not dict or frozenset(raw_licenses) != _LICENSE_POLICY_KEYS:
        _fail("risk_policy_license_invalid")
    allowed_licenses = _license_expressions(
        cast(dict[str, object], raw_licenses)["allowedExpressions"]
    )

    raw_exceptions = document["exceptions"]
    if type(raw_exceptions) is not dict or frozenset(raw_exceptions) != _EXCEPTION_POLICY_KEYS:
        _fail("risk_policy_exception_invalid")
    exception_policy = cast(dict[str, object], raw_exceptions)
    maximum_exception_duration = _positive_integer(
        exception_policy["maximumDurationSeconds"],
        "risk_policy_exception_invalid",
        maximum=_MAX_INTERVAL_SECONDS,
    )
    minimum_rationale_length = _positive_integer(
        exception_policy["minimumRationaleLength"],
        "risk_policy_exception_invalid",
        maximum=1024,
    )
    exceptions = _risk_exceptions(
        exception_policy["records"],
        maximum_duration_seconds=maximum_exception_duration,
        minimum_rationale_length=minimum_rationale_length,
    )

    promotion_enabled = document["promotionEnabled"]
    if promotion_enabled and (
        not allowed_scanners or not approved_databases or not allowed_licenses
    ):
        _fail("risk_policy_incomplete")

    return DependencyRiskPolicy(
        promotion_enabled=promotion_enabled,
        allowed_scanners=allowed_scanners,
        approved_databases=approved_databases,
        maximum_database_age_seconds=maximum_database_age,
        maximum_database_validity_seconds=maximum_database_validity,
        maximum_result_age_seconds=maximum_result_age,
        block_at_or_above=block_at_text,
        block_when_fix_available_at_or_above=block_with_fix_text,
        allowed_license_expressions=allowed_licenses,
        maximum_exception_duration_seconds=maximum_exception_duration,
        minimum_rationale_length=minimum_rationale_length,
        exceptions=exceptions,
        sha256=sha256_bytes(value),
    )


def load_dependency_risk_policy(path: Path) -> DependencyRiskPolicy:
    """Read and strictly validate one stable regular policy file."""

    return load_dependency_risk_policy_bytes(
        _read_regular(path, _MAX_POLICY_BYTES, "risk_policy_file_invalid")
    )


def policy_summary(policy: DependencyRiskPolicy) -> dict[str, object]:
    """Return a canonical-safe summary that never exposes exception text or identities."""

    return {
        "approvedDatabaseCount": len(policy.approved_databases),
        "allowedLicenseExpressionCount": len(policy.allowed_license_expressions),
        "allowedScannerCount": len(policy.allowed_scanners),
        "exceptionCount": len(policy.exceptions),
        "policySha256": policy.sha256,
        "promotionEnabled": policy.promotion_enabled,
        "schemaVersion": POLICY_SCHEMA_VERSION,
        "verified": True,
    }


@dataclass(frozen=True)
class ArtifactBinding:
    kind: str
    filename: str
    byte_size: int
    sha256: str


@dataclass(frozen=True)
class FileBinding:
    byte_size: int
    sha256: str


@dataclass(frozen=True)
class LockTargetBinding:
    scope: str
    python_version: str
    platform: str
    input_sha256: str
    lock_sha256: str


@dataclass(frozen=True)
class LockInventoryBinding:
    inventory_sha256: str
    lock_policy_sha256: str
    package_record_count: int
    target_count: int
    targets: tuple[LockTargetBinding, ...]


@dataclass(frozen=True)
class SbomBinding:
    kind: str
    filename: str
    byte_size: int
    sha256: str


@dataclass(frozen=True)
class DatabaseEvidence:
    filename: str
    byte_size: int
    sha256: str
    source: str
    revision: str
    fetched_at: datetime
    expires_at: datetime
    integrity_status: str


@dataclass(frozen=True)
class LicenseObservation:
    status: str
    expression: str | None


@dataclass(frozen=True)
class VulnerabilityFinding:
    finding_id: str
    aliases: tuple[str, ...]
    severity: str
    fix_status: str
    fixed_versions: tuple[str, ...]


@dataclass(frozen=True)
class ComponentScan:
    purl: str
    artifact_sha256: tuple[str, ...]
    scan_status: str
    license: LicenseObservation
    vulnerabilities: tuple[VulnerabilityFinding, ...]


@dataclass(frozen=True)
class DependencyRiskResult:
    project_name: str
    project_version: str
    commit_sha: str
    tree_sha: str
    artifacts: tuple[ArtifactBinding, ...]
    distribution_manifest: FileBinding
    lock_inventory: LockInventoryBinding
    sboms: tuple[SbomBinding, ...]
    scanner: ScannerIdentity
    database: DatabaseEvidence
    completed_at: datetime
    scan_status: str
    components: tuple[ComponentScan, ...]
    promotion_policy_sha256: str
    sha256: str


def _exact_keys(value: object, expected: frozenset[str], code: str) -> dict[str, object]:
    if type(value) is not dict or frozenset(cast(dict[str, object], value)) != expected:
        _fail(code)
    return cast(dict[str, object], value)


def _digest(value: object, code: str) -> str:
    digest = _safe_string(value, code, maximum=64)
    if _HASH_PATTERN.fullmatch(digest) is None:
        _fail(code)
    return digest


def _git_digest(value: object, code: str) -> str:
    digest = _safe_string(value, code, maximum=64)
    if _GIT_HASH_PATTERN.fullmatch(digest) is None:
        _fail(code)
    return digest


def _safe_filename(value: object, code: str) -> str:
    filename = _safe_string(value, code, maximum=256)
    if (
        not filename.isascii()
        or PurePosixPath(filename).name != filename
        or filename in (".", "..")
        or "\\" in filename
    ):
        _fail(code)
    return filename


def _bounded_nonnegative(value: object, code: str, *, maximum: int) -> int:
    if type(value) is not int or value < 0 or value > maximum:
        _fail(code)
    return value


def _binding(value: object, code: str) -> FileBinding:
    record = _exact_keys(value, _FILE_BINDING_KEYS, code)
    return FileBinding(
        byte_size=_positive_integer(record["byteSize"], code, maximum=2**63 - 1),
        sha256=_digest(record["sha256"], code),
    )


def _artifact_bindings(value: object) -> tuple[ArtifactBinding, ...]:
    if type(value) is not list or len(cast(list[object], value)) != 2:
        _fail("risk_result_artifact_invalid")
    records: list[ArtifactBinding] = []
    for item in cast(list[object], value):
        record = _exact_keys(item, _ARTIFACT_KEYS, "risk_result_artifact_invalid")
        kind = record["kind"]
        if kind not in ("sdist", "wheel"):
            _fail("risk_result_artifact_invalid")
        records.append(
            ArtifactBinding(
                kind=cast(str, kind),
                filename=_safe_filename(record["filename"], "risk_result_artifact_invalid"),
                byte_size=_positive_integer(
                    record["byteSize"], "risk_result_artifact_invalid", maximum=2**63 - 1
                ),
                sha256=_digest(record["sha256"], "risk_result_artifact_invalid"),
            )
        )
    if [record.kind for record in records] != ["sdist", "wheel"]:
        _fail("risk_result_artifact_invalid")
    if len({record.filename for record in records}) != len(records):
        _fail("risk_result_artifact_invalid")
    return tuple(records)


def _python_version_key(value: str) -> tuple[int, ...]:
    try:
        parts = tuple(int(part) for part in value.split("."))
    except ValueError:
        _fail("risk_result_lock_invalid")
    if not parts or len(parts) > 3 or any(part < 0 or part > 999 for part in parts):
        _fail("risk_result_lock_invalid")
    return parts


def _lock_target_bindings(value: object) -> tuple[LockTargetBinding, ...]:
    if type(value) is not list or not 1 <= len(cast(list[object], value)) <= 32:
        _fail("risk_result_lock_invalid")
    targets: list[LockTargetBinding] = []
    for item in cast(list[object], value):
        record = _exact_keys(item, _LOCK_TARGET_KEYS, "risk_result_lock_invalid")
        scope = _identifier(record["scope"], "risk_result_lock_invalid")
        python_version = _safe_string(
            record["pythonVersion"], "risk_result_lock_invalid", maximum=16
        )
        _python_version_key(python_version)
        platform = _identifier(record["platform"], "risk_result_lock_invalid")
        targets.append(
            LockTargetBinding(
                scope=scope,
                python_version=python_version,
                platform=platform,
                input_sha256=_digest(record["inputSha256"], "risk_result_lock_invalid"),
                lock_sha256=_digest(record["lockSha256"], "risk_result_lock_invalid"),
            )
        )
    ordered = sorted(
        targets,
        key=lambda item: (item.scope, _python_version_key(item.python_version), item.platform),
    )
    if targets != ordered:
        _fail("risk_result_lock_invalid")
    identities = {(item.scope, item.python_version, item.platform) for item in targets}
    if len(identities) != len(targets):
        _fail("risk_result_lock_invalid")
    return tuple(targets)


def _lock_inventory_binding(value: object) -> LockInventoryBinding:
    record = _exact_keys(value, _LOCK_INVENTORY_KEYS, "risk_result_lock_invalid")
    targets = _lock_target_bindings(record["targets"])
    target_count = _positive_integer(record["targetCount"], "risk_result_lock_invalid", maximum=32)
    if target_count != len(targets):
        _fail("risk_result_lock_invalid")
    return LockInventoryBinding(
        inventory_sha256=_digest(record["inventorySha256"], "risk_result_lock_invalid"),
        lock_policy_sha256=_digest(record["lockPolicySha256"], "risk_result_lock_invalid"),
        package_record_count=_positive_integer(
            record["packageRecordCount"], "risk_result_lock_invalid", maximum=8192
        ),
        target_count=target_count,
        targets=targets,
    )


def _sbom_bindings(value: object) -> tuple[SbomBinding, ...]:
    if type(value) is not list or len(cast(list[object], value)) != 2:
        _fail("risk_result_sbom_invalid")
    records: list[SbomBinding] = []
    for item in cast(list[object], value):
        record = _exact_keys(item, _SBOM_KEYS, "risk_result_sbom_invalid")
        kind = record["kind"]
        if kind not in ("runtime", "build"):
            _fail("risk_result_sbom_invalid")
        records.append(
            SbomBinding(
                kind=cast(str, kind),
                filename=_safe_filename(record["filename"], "risk_result_sbom_invalid"),
                byte_size=_positive_integer(
                    record["byteSize"], "risk_result_sbom_invalid", maximum=16 * 1024 * 1024
                ),
                sha256=_digest(record["sha256"], "risk_result_sbom_invalid"),
            )
        )
    if [record.kind for record in records] != ["runtime", "build"]:
        _fail("risk_result_sbom_invalid")
    if len({record.filename for record in records}) != len(records):
        _fail("risk_result_sbom_invalid")
    return tuple(records)


def _result_scanner(value: object) -> ScannerIdentity:
    record = _exact_keys(value, _RESULT_SCANNER_KEYS, "risk_result_scanner_invalid")
    name = _identifier(record["name"], "risk_result_scanner_invalid")
    version = _safe_string(record["version"], "risk_result_scanner_invalid", maximum=128)
    if _VERSION_PATTERN.fullmatch(version) is None:
        _fail("risk_result_scanner_invalid")
    return ScannerIdentity(
        name=name,
        version=version,
        sha256=_digest(record["sha256"], "risk_result_scanner_invalid"),
    )


def _database_evidence(value: object) -> DatabaseEvidence:
    record = _exact_keys(value, _DATABASE_KEYS, "risk_result_database_invalid")
    integrity_status = record["integrityStatus"]
    if integrity_status not in _INTEGRITY_STATUSES:
        _fail("risk_result_database_invalid")
    fetched_at = parse_timestamp(record["fetchedAt"], "risk_result_database_invalid")
    expires_at = parse_timestamp(record["expiresAt"], "risk_result_database_invalid")
    if expires_at <= fetched_at:
        _fail("risk_result_database_invalid")
    return DatabaseEvidence(
        filename=_safe_filename(record["filename"], "risk_result_database_invalid"),
        byte_size=_positive_integer(
            record["byteSize"], "risk_result_database_invalid", maximum=2**63 - 1
        ),
        sha256=_digest(record["sha256"], "risk_result_database_invalid"),
        source=_canonical_https_url(record["source"], "risk_result_database_invalid"),
        revision=_identifier(record["revision"], "risk_result_database_invalid"),
        fetched_at=fetched_at,
        expires_at=expires_at,
        integrity_status=cast(str, integrity_status),
    )


def _license_observation(value: object) -> LicenseObservation:
    record = _exact_keys(value, _LICENSE_OBSERVATION_KEYS, "risk_result_license_invalid")
    status = record["status"]
    expression = record["expression"]
    if status == "known":
        if expression is None:
            _fail("risk_result_license_invalid")
        normalized = canonical_license_expression(expression, "risk_result_license_invalid")
    elif status == "unknown":
        if expression is not None:
            _fail("risk_result_license_invalid")
        normalized = None
    else:
        _fail("risk_result_license_invalid")
    return LicenseObservation(status=cast(str, status), expression=normalized)


def _identifier_list(value: object, code: str, *, maximum: int) -> tuple[str, ...]:
    if type(value) is not list or len(cast(list[object], value)) > maximum:
        _fail(code)
    identifiers = tuple(_identifier(item, code, finding=True) for item in cast(list[object], value))
    if identifiers != tuple(sorted(identifiers)) or len(set(identifiers)) != len(identifiers):
        _fail(code)
    return identifiers


def _version_list(value: object, code: str) -> tuple[str, ...]:
    if type(value) is not list or len(cast(list[object], value)) > _MAX_FIXED_VERSIONS:
        _fail(code)
    versions: list[str] = []
    for item in cast(list[object], value):
        version = _safe_string(item, code, maximum=128)
        if _VERSION_PATTERN.fullmatch(version) is None:
            _fail(code)
        versions.append(version)
    if versions != sorted(versions) or len(set(versions)) != len(versions):
        _fail(code)
    return tuple(versions)


def _vulnerability_findings(value: object) -> tuple[VulnerabilityFinding, ...]:
    if type(value) is not list or len(cast(list[object], value)) > _MAX_FINDINGS_PER_COMPONENT:
        _fail("risk_result_finding_invalid")
    findings: list[VulnerabilityFinding] = []
    identities: set[str] = set()
    for item in cast(list[object], value):
        record = _exact_keys(item, _VULNERABILITY_KEYS, "risk_result_finding_invalid")
        finding_id = _identifier(record["id"], "risk_result_finding_invalid", finding=True)
        aliases = _identifier_list(
            record["aliases"], "risk_result_finding_invalid", maximum=_MAX_ALIASES
        )
        if finding_id in aliases or finding_id in identities or identities.intersection(aliases):
            _fail("risk_result_finding_duplicate")
        identities.add(finding_id)
        identities.update(aliases)
        severity = record["severity"]
        fix_status = record["fixStatus"]
        if severity not in _RESULT_SEVERITIES or fix_status not in _FIX_STATUSES:
            _fail("risk_result_finding_invalid")
        fixed_versions = _version_list(record["fixedVersions"], "risk_result_finding_invalid")
        if (fix_status == "available") != bool(fixed_versions):
            _fail("risk_result_finding_invalid")
        findings.append(
            VulnerabilityFinding(
                finding_id=finding_id,
                aliases=aliases,
                severity=cast(str, severity),
                fix_status=cast(str, fix_status),
                fixed_versions=fixed_versions,
            )
        )
    if findings != sorted(findings, key=lambda item: item.finding_id):
        _fail("risk_result_finding_invalid")
    return tuple(findings)


def _component_scans(value: object) -> tuple[ComponentScan, ...]:
    if type(value) is not list or not 1 <= len(cast(list[object], value)) <= _MAX_COMPONENTS:
        _fail("risk_result_component_invalid")
    components: list[ComponentScan] = []
    total_findings = 0
    for item in cast(list[object], value):
        record = _exact_keys(item, _COMPONENT_SCAN_KEYS, "risk_result_component_invalid")
        raw_hashes = record["artifactSha256"]
        if (
            type(raw_hashes) is not list
            or not 1 <= len(cast(list[object], raw_hashes)) <= _MAX_HASHES_PER_COMPONENT
        ):
            _fail("risk_result_component_invalid")
        hashes = tuple(
            _digest(digest, "risk_result_component_invalid")
            for digest in cast(list[object], raw_hashes)
        )
        if hashes != tuple(sorted(hashes)) or len(set(hashes)) != len(hashes):
            _fail("risk_result_component_invalid")
        scan_status = record["scanStatus"]
        if scan_status not in _SCAN_STATUSES:
            _fail("risk_result_component_invalid")
        findings = _vulnerability_findings(record["vulnerabilities"])
        total_findings += len(findings)
        if total_findings > _MAX_FINDINGS:
            _fail("risk_result_finding_invalid")
        components.append(
            ComponentScan(
                purl=canonical_pypi_purl(record["purl"], "risk_result_component_invalid"),
                artifact_sha256=hashes,
                scan_status=cast(str, scan_status),
                license=_license_observation(record["license"]),
                vulnerabilities=findings,
            )
        )
    if components != sorted(components, key=lambda item: item.purl):
        _fail("risk_result_component_invalid")
    if len({component.purl for component in components}) != len(components):
        _fail("risk_result_component_duplicate")
    return tuple(components)


def load_dependency_risk_result_bytes(value: bytes) -> DependencyRiskResult:
    """Strictly parse one canonical, versioned, offline scanner result."""

    document = _parse_canonical_json(value, limit=_MAX_RESULT_BYTES, code="risk_result_invalid")
    if frozenset(document) != _RESULT_KEYS:
        _fail("risk_result_invalid")
    if (
        document["format"] != RESULT_FORMAT
        or type(document["schemaVersion"]) is not int
        or document["schemaVersion"] != RESULT_SCHEMA_VERSION
    ):
        _fail("risk_result_invalid")

    project = _exact_keys(document["project"], _PROJECT_KEYS, "risk_result_project_invalid")
    project_name = _safe_string(project["name"], "risk_result_project_invalid", maximum=128)
    project_version = _safe_string(project["version"], "risk_result_project_invalid", maximum=128)
    if (
        _NAME_PATTERN.fullmatch(project_name) is None
        or _VERSION_PATTERN.fullmatch(project_version) is None
    ):
        _fail("risk_result_project_invalid")

    source = _exact_keys(document["source"], _SOURCE_KEYS, "risk_result_source_invalid")
    scanner = _result_scanner(document["scanner"])
    database = _database_evidence(document["database"])
    scan = _exact_keys(document["scan"], _SCAN_KEYS, "risk_result_scan_invalid")
    scan_status = scan["status"]
    if scan_status not in _SCAN_STATUSES:
        _fail("risk_result_scan_invalid")
    completed_at = parse_timestamp(scan["completedAt"], "risk_result_scan_invalid")
    if completed_at < database.fetched_at:
        _fail("risk_result_scan_invalid")

    return DependencyRiskResult(
        project_name=project_name,
        project_version=project_version,
        commit_sha=_git_digest(source["commitSha"], "risk_result_source_invalid"),
        tree_sha=_git_digest(source["treeSha"], "risk_result_source_invalid"),
        artifacts=_artifact_bindings(document["artifacts"]),
        distribution_manifest=_binding(
            document["distributionManifest"], "risk_result_manifest_invalid"
        ),
        lock_inventory=_lock_inventory_binding(document["lockInventory"]),
        sboms=_sbom_bindings(document["sboms"]),
        scanner=scanner,
        database=database,
        completed_at=completed_at,
        scan_status=cast(str, scan_status),
        components=_component_scans(scan["components"]),
        promotion_policy_sha256=_digest(
            document["promotionPolicySha256"], "risk_result_policy_invalid"
        ),
        sha256=sha256_bytes(value),
    )


def load_dependency_risk_result(path: Path) -> DependencyRiskResult:
    """Read and strictly validate one stable regular scanner result file."""

    return load_dependency_risk_result_bytes(
        _read_regular(path, _MAX_RESULT_BYTES, "risk_result_file_invalid")
    )


def _finding_document(
    component: ComponentScan,
    *,
    kind: str,
    subject: str | None,
    finding: VulnerabilityFinding | None = None,
) -> dict[str, object]:
    document: dict[str, object] = {
        "artifactSha256": list(component.artifact_sha256),
        "kind": kind,
        "purl": component.purl,
        "subject": subject,
    }
    if finding is not None:
        document["vulnerability"] = {
            "aliases": list(finding.aliases),
            "fixedVersions": list(finding.fixed_versions),
            "fixStatus": finding.fix_status,
            "id": finding.finding_id,
            "severity": finding.severity,
        }
    return document


def vulnerability_finding_sha256(component: ComponentScan, finding: VulnerabilityFinding) -> str:
    """Fingerprint one exact component/artifact/vulnerability observation."""

    return sha256_bytes(
        canonical_json(
            _finding_document(
                component,
                kind="vulnerability",
                subject=finding.finding_id,
                finding=finding,
            )
        )
    )


def license_finding_sha256(component: ComponentScan) -> str:
    """Fingerprint one exact component/artifact/license observation."""

    return sha256_bytes(
        canonical_json(
            _finding_document(
                component,
                kind="license",
                subject=component.license.expression,
            )
        )
    )


@dataclass(frozen=True)
class ExpectedComponent:
    purl: str
    artifact_sha256: tuple[str, ...]


@dataclass(frozen=True)
class RiskEvidenceContext:
    project_name: str
    project_version: str
    commit_sha: str
    tree_sha: str
    artifacts: tuple[ArtifactBinding, ...]
    distribution_manifest: FileBinding
    lock_inventory: LockInventoryBinding
    sboms: tuple[SbomBinding, ...]
    components: tuple[ExpectedComponent, ...]


@dataclass(frozen=True)
class RiskVerificationSummary:
    component_count: int
    finding_count: int
    applied_exception_count: int
    policy_sha256: str
    result_sha256: str
    database_sha256: str
    lock_inventory_sha256: str
    evaluation_time: str


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _manifest_artifacts(manifest: Mapping[str, object]) -> tuple[ArtifactBinding, ...]:
    raw_artifacts = manifest.get("artifacts")
    if type(raw_artifacts) is not list:
        _fail("risk_source_evidence_invalid")
    records: list[ArtifactBinding] = []
    for item in cast(list[object], raw_artifacts):
        if type(item) is not dict:
            _fail("risk_source_evidence_invalid")
        record = cast(dict[str, object], item)
        kind = record.get("kind")
        filename = record.get("filename")
        byte_size = record.get("byteSize")
        digest = record.get("sha256")
        if kind not in ("sdist", "wheel"):
            _fail("risk_source_evidence_invalid")
        records.append(
            ArtifactBinding(
                kind=cast(str, kind),
                filename=_safe_filename(filename, "risk_source_evidence_invalid"),
                byte_size=_positive_integer(
                    byte_size, "risk_source_evidence_invalid", maximum=2**63 - 1
                ),
                sha256=_digest(digest, "risk_source_evidence_invalid"),
            )
        )
    if [record.kind for record in records] != ["sdist", "wheel"]:
        _fail("risk_source_evidence_invalid")
    return tuple(records)


def _target_binding(target: LockTarget) -> LockTargetBinding:
    return LockTargetBinding(
        scope=target.scope,
        python_version=target.python_version,
        platform=target.platform,
        input_sha256=target.input_sha256,
        lock_sha256=target.lock_sha256,
    )


def _lock_inventory(repository_root: Path, targets: Sequence[LockTarget]) -> LockInventoryBinding:
    policy_bytes = _read_regular(
        repository_root / "requirements" / "lock-policy.json",
        1024 * 1024,
        "risk_lock_evidence_invalid",
    )
    policy_digest = sha256_bytes(policy_bytes)
    bindings = tuple(_target_binding(target) for target in targets)
    document = {
        "format": "quantum-entanglement.dependency-lock-inventory-binding",
        "lockPolicySha256": policy_digest,
        "schemaVersion": 1,
        "targets": [
            {
                "inputSha256": target.input_sha256,
                "lockSha256": target.lock_sha256,
                "platform": target.platform,
                "pythonVersion": target.python_version,
                "scope": target.scope,
            }
            for target in bindings
        ],
    }
    return LockInventoryBinding(
        inventory_sha256=sha256_bytes(canonical_json(document)),
        lock_policy_sha256=policy_digest,
        package_record_count=sum(len(target.packages) for target in targets),
        target_count=len(targets),
        targets=bindings,
    )


def _component_property(component: Mapping[str, object], name: str) -> str:
    raw_properties = component.get("properties")
    if type(raw_properties) is not list:
        _fail("risk_sbom_component_invalid")
    matches: list[str] = []
    for item in cast(list[object], raw_properties):
        if type(item) is not dict:
            _fail("risk_sbom_component_invalid")
        record = cast(dict[str, object], item)
        if record.get("name") == name and type(record.get("value")) is str:
            matches.append(cast(str, record["value"]))
    if len(matches) != 1:
        _fail("risk_sbom_component_invalid")
    return matches[0]


def _expected_components(
    documents: Mapping[str, bytes], artifacts: Sequence[ArtifactBinding]
) -> tuple[ExpectedComponent, ...]:
    runtime_bytes = documents.get("quantum-entanglement-runtime.cdx.json")
    build_bytes = documents.get("quantum-entanglement-build.cdx.json")
    if runtime_bytes is None or build_bytes is None:
        _fail("risk_sbom_component_invalid")
    runtime = validate_sbom_bytes(runtime_bytes, kind="runtime")
    build = validate_sbom_bytes(build_bytes, kind="build")
    runtime_metadata = cast(dict[str, object], runtime["metadata"])
    runtime_root = cast(dict[str, object], runtime_metadata["component"])
    runtime_purl = canonical_pypi_purl(runtime_root.get("purl"), "risk_sbom_component_invalid")
    components: list[ExpectedComponent] = [
        ExpectedComponent(
            purl=runtime_purl,
            artifact_sha256=tuple(sorted(artifact.sha256 for artifact in artifacts)),
        )
    ]
    raw_components = build.get("components")
    if type(raw_components) is not list:
        _fail("risk_sbom_component_invalid")
    for item in cast(list[object], raw_components):
        if type(item) is not dict:
            _fail("risk_sbom_component_invalid")
        component = cast(dict[str, object], item)
        purl = canonical_pypi_purl(component.get("purl"), "risk_sbom_component_invalid")
        raw_hashes = _component_property(
            component, "quantum-entanglement:lock:artifact-sha256"
        ).split(",")
        hashes = tuple(_digest(value, "risk_sbom_component_invalid") for value in raw_hashes)
        if hashes != tuple(sorted(hashes)) or len(set(hashes)) != len(hashes):
            _fail("risk_sbom_component_invalid")
        components.append(ExpectedComponent(purl=purl, artifact_sha256=hashes))
    components.sort(key=lambda item: item.purl)
    if len({component.purl for component in components}) != len(components):
        _fail("risk_sbom_component_invalid")
    return tuple(components)


def collect_risk_evidence_context(
    repository_root: Path,
    distribution_directory: Path,
    distribution_manifest: Path,
    sbom_directory: Path,
    *,
    expected_commit: str,
) -> RiskEvidenceContext:
    """Rebuild the authoritative source/lock/SBOM subject for offline risk verification."""

    try:
        verify_distribution_manifest_file(
            distribution_manifest,
            repository_root,
            distribution_directory,
            expected_commit_sha=expected_commit,
        )
        manifest_bytes = _read_regular(
            distribution_manifest, _MAX_MANIFEST_BYTES, "risk_manifest_evidence_invalid"
        )
        manifest = load_distribution_manifest(distribution_manifest)
        targets = verify_dependency_locks(repository_root)
        documents = generate_sbom_documents(repository_root, manifest, targets)
        verified_documents = verify_sbom_directory(
            sbom_directory, documents, repository_root=repository_root
        )
    except (DistributionManifestError, DependencyLockError, SbomError):
        _fail("risk_source_evidence_invalid")

    raw_project = manifest.get("project")
    raw_source = manifest.get("source")
    if type(raw_project) is not dict or type(raw_source) is not dict:
        _fail("risk_source_evidence_invalid")
    project = cast(dict[str, object], raw_project)
    source = cast(dict[str, object], raw_source)
    project_name = _safe_string(project.get("name"), "risk_source_evidence_invalid")
    project_version = _safe_string(project.get("version"), "risk_source_evidence_invalid")
    commit_sha = _git_digest(source.get("commitSha"), "risk_source_evidence_invalid")
    tree_sha = _git_digest(source.get("treeSha"), "risk_source_evidence_invalid")
    artifacts = _manifest_artifacts(manifest)
    sboms = tuple(
        SbomBinding(
            kind="runtime" if filename.endswith("-runtime.cdx.json") else "build",
            filename=filename,
            byte_size=len(verified_documents[filename]),
            sha256=sha256_bytes(verified_documents[filename]),
        )
        for filename in (
            "quantum-entanglement-runtime.cdx.json",
            "quantum-entanglement-build.cdx.json",
        )
    )
    context = RiskEvidenceContext(
        project_name=project_name,
        project_version=project_version,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        artifacts=artifacts,
        distribution_manifest=FileBinding(
            byte_size=len(manifest_bytes), sha256=sha256_bytes(manifest_bytes)
        ),
        lock_inventory=_lock_inventory(repository_root, targets),
        sboms=sboms,
        components=_expected_components(documents, artifacts),
    )

    try:
        verify_distribution_manifest_file(
            distribution_manifest,
            repository_root,
            distribution_directory,
            expected_commit_sha=expected_commit,
        )
        if (
            _read_regular(
                distribution_manifest,
                _MAX_MANIFEST_BYTES,
                "risk_manifest_evidence_invalid",
            )
            != manifest_bytes
            or verify_dependency_locks(repository_root) != targets
        ):
            _fail("risk_source_evidence_changed")
        verify_sbom_directory(sbom_directory, documents, repository_root=repository_root)
    except (DistributionManifestError, DependencyLockError, SbomError):
        _fail("risk_source_evidence_changed")
    return context


def require_outside_repository_file(path: Path, repository_root: Path, code: str) -> None:
    """Require one existing evidence file to resolve outside the source checkout."""

    try:
        resolved_path = path.resolve(strict=True)
        resolved_root = repository_root.resolve(strict=True)
    except OSError:
        _fail(code)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return
    _fail(code)


def read_database_snapshot(
    path: Path, *, repository_root: Path, expected: DatabaseEvidence
) -> bytes:
    """Read and bind the approved offline database snapshot without trusting its path."""

    require_outside_repository_file(path, repository_root, "risk_database_file_invalid")
    value = _read_regular(path, _MAX_DATABASE_BYTES, "risk_database_file_invalid")
    if (
        path.name != expected.filename
        or len(value) != expected.byte_size
        or sha256_bytes(value) != expected.sha256
    ):
        _fail("risk_database_drift")
    return value


def _context_matches(result: DependencyRiskResult, context: RiskEvidenceContext) -> None:
    if (
        result.project_name != context.project_name
        or result.project_version != context.project_version
        or result.commit_sha != context.commit_sha
        or result.tree_sha != context.tree_sha
    ):
        _fail("risk_source_drift")
    if (
        result.artifacts != context.artifacts
        or result.distribution_manifest != context.distribution_manifest
    ):
        _fail("risk_manifest_drift")
    if result.lock_inventory != context.lock_inventory:
        _fail("risk_lock_drift")
    if result.sboms != context.sboms:
        _fail("risk_sbom_drift")


def _approved_identity(policy: DependencyRiskPolicy, result: DependencyRiskResult) -> None:
    if result.scanner not in policy.allowed_scanners:
        _fail("risk_scanner_unapproved")
    approved = ApprovedDatabase(
        source=result.database.source,
        revision=result.database.revision,
        sha256=result.database.sha256,
    )
    if approved not in policy.approved_databases:
        _fail("risk_database_unapproved")
    if result.database.integrity_status != "verified":
        _fail("risk_database_integrity_unverified")


def _verify_time_window(
    policy: DependencyRiskPolicy,
    result: DependencyRiskResult,
    evaluation_time: datetime,
) -> None:
    if result.completed_at > evaluation_time:
        _fail("risk_result_from_future")
    if result.completed_at >= result.database.expires_at:
        _fail("risk_result_stale")
    database_age = int((evaluation_time - result.database.fetched_at).total_seconds())
    result_age = int((evaluation_time - result.completed_at).total_seconds())
    validity = int((result.database.expires_at - result.database.fetched_at).total_seconds())
    if (
        database_age < 0
        or evaluation_time >= result.database.expires_at
        or database_age > policy.maximum_database_age_seconds
        or validity > policy.maximum_database_validity_seconds
    ):
        _fail("risk_database_stale")
    if result_age < 0 or result_age > policy.maximum_result_age_seconds:
        _fail("risk_result_stale")


def _verify_component_coverage(result: DependencyRiskResult, context: RiskEvidenceContext) -> None:
    observed = tuple(
        ExpectedComponent(purl=component.purl, artifact_sha256=component.artifact_sha256)
        for component in result.components
    )
    if observed != context.components:
        _fail("risk_component_coverage_mismatch")
    if result.scan_status != "complete" or any(
        component.scan_status != "complete" for component in result.components
    ):
        _fail("risk_scan_incomplete")


def _exception_key(exception: RiskException) -> tuple[str, str, str | None, str]:
    return exception.kind, exception.purl, exception.subject, exception.finding_sha256


def verify_dependency_risk(
    policy: DependencyRiskPolicy,
    result: DependencyRiskResult,
    context: RiskEvidenceContext,
    *,
    database_snapshot: bytes,
    evaluation_time: datetime,
) -> RiskVerificationSummary:
    """Apply the exact offline promotion policy to source-bound scanner evidence."""

    _context_matches(result, context)
    if result.promotion_policy_sha256 != policy.sha256:
        _fail("risk_policy_drift")
    if (
        len(database_snapshot) != result.database.byte_size
        or sha256_bytes(database_snapshot) != result.database.sha256
    ):
        _fail("risk_database_drift")
    if not policy.promotion_enabled:
        _fail("risk_promotion_disabled")
    _approved_identity(policy, result)
    _verify_time_window(policy, result, evaluation_time)
    _verify_component_coverage(result, context)

    exception_by_key: dict[tuple[str, str, str | None, str], RiskException] = {}
    for exception in policy.exceptions:
        if exception.database_sha256 != result.database.sha256:
            _fail("risk_exception_database_mismatch")
        if evaluation_time < exception.issued_at:
            _fail("risk_exception_inactive")
        if evaluation_time >= exception.expires_at:
            _fail("risk_exception_expired")
        exception_by_key[_exception_key(exception)] = exception

    violations: list[tuple[str, str, str | None, str]] = []
    finding_count = 0
    for component in result.components:
        if component.license.status == "unknown":
            violations.append(("license", component.purl, None, license_finding_sha256(component)))
        elif component.license.expression not in policy.allowed_license_expressions:
            violations.append(
                (
                    "license",
                    component.purl,
                    component.license.expression,
                    license_finding_sha256(component),
                )
            )
        for finding in component.vulnerabilities:
            finding_count += 1
            if finding.severity == "unknown":
                _fail("risk_severity_unknown")
            if finding.fix_status == "unknown":
                _fail("risk_fix_status_unknown")
            severity_rank = _SEVERITY_RANK[finding.severity]
            denied = severity_rank >= _SEVERITY_RANK[policy.block_at_or_above]
            denied_with_fix = (
                finding.fix_status == "available"
                and severity_rank >= _SEVERITY_RANK[policy.block_when_fix_available_at_or_above]
            )
            if denied or denied_with_fix:
                violations.append(
                    (
                        "vulnerability",
                        component.purl,
                        finding.finding_id,
                        vulnerability_finding_sha256(component, finding),
                    )
                )

    used_exception_ids: set[str] = set()
    for violation in violations:
        matched_exception = exception_by_key.get(violation)
        if matched_exception is None:
            _fail("risk_policy_denied")
        used_exception_ids.add(matched_exception.exception_id)
    if used_exception_ids != {exception.exception_id for exception in policy.exceptions}:
        _fail("risk_exception_unused")

    return RiskVerificationSummary(
        component_count=len(result.components),
        finding_count=finding_count,
        applied_exception_count=len(used_exception_ids),
        policy_sha256=policy.sha256,
        result_sha256=result.sha256,
        database_sha256=result.database.sha256,
        lock_inventory_sha256=result.lock_inventory.inventory_sha256,
        evaluation_time=_timestamp_text(evaluation_time),
    )


def verification_summary_document(summary: RiskVerificationSummary) -> dict[str, object]:
    """Return the fixed redacted success record emitted by the promotion CLI."""

    return {
        "appliedExceptionCount": summary.applied_exception_count,
        "componentCount": summary.component_count,
        "databaseSha256": summary.database_sha256,
        "decision": "promote",
        "evaluationTime": summary.evaluation_time,
        "findingCount": summary.finding_count,
        "lockInventorySha256": summary.lock_inventory_sha256,
        "policySha256": summary.policy_sha256,
        "resultSha256": summary.result_sha256,
        "verified": True,
    }
