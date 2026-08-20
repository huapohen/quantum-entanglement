"""Canonical dependency-risk promotion policy and offline evidence primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn, cast
from urllib.parse import quote, unquote, urlsplit, urlunsplit

POLICY_FORMAT = "quantum-entanglement.dependency-risk-policy"
POLICY_SCHEMA_VERSION = 1
DEFAULT_POLICY_PATH = Path("requirements/dependency-risk-policy.json")

_MAX_POLICY_BYTES = 1024 * 1024
_MAX_STRING_BYTES = 4096
_MAX_EXCEPTIONS = 256
_MAX_ALLOWED_IDENTITIES = 128
_MAX_ALLOWED_LICENSES = 256
_MAX_INTERVAL_SECONDS = 366 * 24 * 60 * 60
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_VERSION_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9.!+_-]{0,126}[A-Za-z0-9])?$"
)
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:@/+_-]{0,126}[A-Za-z0-9])?$")
_FINDING_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,126}[A-Za-z0-9])?$"
)
_TIMESTAMP_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z$")
_SPDX_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,126}$")
_SPDX_OPERATORS = frozenset({"AND", "OR", "WITH"})
_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}

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
_VULNERABILITY_POLICY_KEYS = frozenset(
    {"blockAtOrAbove", "blockWhenFixAvailableAtOrAbove"}
)
_LICENSE_POLICY_KEYS = frozenset({"allowedExpressions"})
_EXCEPTION_POLICY_KEYS = frozenset(
    {"maximumDurationSeconds", "minimumRationaleLength", "records"}
)
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
    result = cast(str, value)
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
    return cast(int, value)


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
    if identities != sorted(
        identities, key=lambda item: (item.name, item.version, item.sha256)
    ):
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
    if databases != sorted(
        databases, key=lambda item: (item.source, item.revision, item.sha256)
    ):
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
                else canonical_license_expression(
                    raw_expression, "risk_policy_exception_invalid"
                )
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
    exact_scopes = {
        (item.database_sha256, item.finding_sha256, *item.scope) for item in exceptions
    }
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

    promotion_enabled = cast(bool, document["promotionEnabled"])
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
