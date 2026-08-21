# ruff: noqa: UP006, UP035, UP045
"""Trusted exact SQLite schema topology for backup manifest evidence.

The registry in this module is data only.  It neither opens SQLite nor changes schema.
It freezes the exact catalog objects currently created by pre-registry components, the
legacy migration ledger, each packaged legacy migration, and the bridge sidecar so a
future backup verifier can classify one catalog without a table-name allowlist.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from itertools import islice
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple, TypeVar

from .domain_migrations import DOMAIN_MIGRATION_REGISTRY

BACKUP_TOPOLOGY_PROFILE = "qe.sqlite-topology/bridge-v1"
BACKUP_TOPOLOGY_REGISTRY_FORMAT = "qe.sqlite-topology-registry/1"
BACKUP_TOPOLOGY_PROFILE_FORMAT = "qe.sqlite-topology-profile/1"

EVENT_STORE_CORE_PROFILE = "qe.event-store-core/1"
PROJECTION_STORE_PROFILE = "qe.projection-store/1"
REVOCATION_GUARD_PROFILE = "qe.revocation-guard/1"
LEGACY_MIGRATION_LEDGER_PROFILE = "qe.legacy-migration-ledger/1"
DOMAIN_MIGRATION_SIDECAR_PROFILE = "qe.domain-migration-sidecar/1"

_DOMAIN_MIGRATION_PROFILE_NAMES = (
    "qe.domain-migration-0001/1",
    "qe.domain-migration-0002/1",
    "qe.domain-migration-0003/1",
)
_PRESENCE_MODE = "atomic"
_MAX_PROFILE_COUNT = 64
_MAX_PROFILE_OBJECTS = 256
_MAX_PROFILE_DEPENDENCIES = 32
_MAX_SCHEMA_SQL_LENGTH = 64 * 1024
_MAX_NAME_LENGTH = 128
_MAX_PROFILE_NAME_LENGTH = 128
_MAX_OWNER_LENGTH = 64

_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_PROFILE_PATTERN = re.compile(r"[a-z][a-z0-9._/-]{0,127}\Z")
_OWNER_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_OBJECT_TYPES = frozenset(("index", "table", "trigger", "view"))
_SQLITE_TOKEN_WHITESPACE = frozenset((" ", "\t", "\n", "\f", "\r"))

_T = TypeVar("_T")


def _bounded_tuple(values: Iterable[_T], *, maximum: int, label: str) -> Tuple[_T, ...]:
    try:
        iterator = iter(values)
    except TypeError as error:
        raise TypeError(f"{label} must be iterable") from error
    snapshot = tuple(islice(iterator, maximum + 1))
    if len(snapshot) > maximum:
        raise ValueError(f"{label} exceeds the hard limit of {maximum}")
    return snapshot


def _plain_string(value: object, label: str, *, maximum: int) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a plain string")
    if not value or len(value) > maximum:
        raise ValueError(f"{label} must contain between 1 and {maximum} characters")
    return value


def _name(value: object, label: str) -> str:
    name = _plain_string(value, label, maximum=_MAX_NAME_LENGTH)
    if _NAME_PATTERN.fullmatch(name) is None:
        raise ValueError(f"{label} must be a bounded ASCII SQLite identifier")
    return name


def _profile_name(value: object, label: str) -> str:
    name = _plain_string(value, label, maximum=_MAX_PROFILE_NAME_LENGTH)
    if _PROFILE_PATTERN.fullmatch(name) is None:
        raise ValueError(f"{label} must be a canonical profile identifier")
    return name


def _owner(value: object) -> str:
    owner = _plain_string(value, "schema object owner", maximum=_MAX_OWNER_LENGTH)
    if _OWNER_PATTERN.fullmatch(owner) is None:
        raise ValueError("schema object owner must be a canonical lower-snake-case identifier")
    return owner


def _sha256(value: object, label: str) -> str:
    digest = _plain_string(value, label, maximum=64)
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError(f"{label} must be lowercase SHA-256 hex")
    return digest


def _canonical_json_sha256(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonicalize_backup_schema_sql(value: object) -> str:
    """Normalize only SQLite-token whitespace outside quotes and comments.

    Quoted content is copied byte-for-byte.  A leading ``IF NOT EXISTS`` is removed
    because SQLite may omit it from ``sqlite_master.sql``. Comments are copied
    byte-for-byte, including the LF that terminates a ``--`` comment.
    """

    sql = _plain_string(value, "SQLite schema SQL", maximum=_MAX_SCHEMA_SQL_LENGTH)
    output: List[str] = []
    plain_flags: List[bool] = []
    pending_whitespace = False

    def emit(text: str, *, plain: bool) -> None:
        output.extend(text)
        plain_flags.extend((plain,) * len(text))

    def flush_whitespace() -> None:
        nonlocal pending_whitespace
        if pending_whitespace and output:
            emit(" ", plain=True)
        pending_whitespace = False

    index = 0
    while index < len(sql):
        character = sql[index]
        if character in _SQLITE_TOKEN_WHITESPACE:
            pending_whitespace = bool(output)
            index += 1
            continue
        flush_whitespace()
        if sql.startswith("--", index):
            line_end = sql.find("\n", index + 2)
            if line_end < 0:
                emit(sql[index:], plain=False)
                index = len(sql)
            else:
                emit(sql[index : line_end + 1], plain=False)
                index = line_end + 1
            continue
        if sql.startswith("/*", index):
            comment_end = sql.find("*/", index + 2)
            if comment_end < 0:
                raise ValueError("SQLite schema SQL contains an unterminated block comment")
            emit(sql[index : comment_end + 2], plain=False)
            index = comment_end + 2
            continue
        if character in {"'", '"', "`", "["}:
            quote_end = "]" if character == "[" else character
            quote_start = index
            index += 1
            while index < len(sql):
                if sql[index] != quote_end:
                    index += 1
                    continue
                if character != "[" and index + 1 < len(sql) and sql[index + 1] == quote_end:
                    index += 2
                    continue
                index += 1
                emit(sql[quote_start:index], plain=False)
                break
            else:
                raise ValueError("SQLite schema SQL contains an unterminated quoted region")
            continue
        emit(character, plain=True)
        index += 1

    while output and plain_flags[-1] and output[-1] in _SQLITE_TOKEN_WHITESPACE:
        output.pop()
        plain_flags.pop()
    if output and plain_flags[-1] and output[-1] == ";":
        output.pop()
        plain_flags.pop()
        while output and plain_flags[-1] and output[-1] in _SQLITE_TOKEN_WHITESPACE:
            output.pop()
            plain_flags.pop()
    canonical = "".join(output)
    if not canonical:
        raise ValueError("SQLite schema SQL has no canonical token content")
    canonical, _replacements = re.subn(
        r"\A(CREATE(?: UNIQUE)? (?:TABLE|INDEX|TRIGGER|VIEW)) IF NOT EXISTS\b",
        r"\1",
        canonical,
        count=1,
        flags=re.IGNORECASE,
    )
    return canonical


def backup_schema_ddl_sha256(value: object) -> str:
    """Hash one canonical explicit SQLite catalog definition."""

    return hashlib.sha256(canonicalize_backup_schema_sql(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, order=True)
class TrustedBackupSchemaObject:
    """One exact catalog coordinate owned by a trusted topology profile.

    ``ddl_sha256`` is ``None`` only for SQLite-created autoindexes whose catalog SQL is
    required to be SQL NULL.  The profile digest still binds their type, name, table,
    owner, and required-null definition.
    """

    profile: str
    owner: str
    object_type: str
    name: str
    table_name: str
    ddl_sha256: Optional[str]

    def __post_init__(self) -> None:
        _profile_name(self.profile, "schema object profile")
        _owner(self.owner)
        object_type = _plain_string(
            self.object_type,
            "schema object type",
            maximum=len("trigger"),
        )
        if object_type not in _OBJECT_TYPES:
            raise ValueError("schema object type is unsupported")
        _name(self.name, "schema object name")
        _name(self.table_name, "schema object table name")
        if object_type == "table" and self.name != self.table_name:
            raise ValueError("table schema object name must equal table name")
        if self.ddl_sha256 is None:
            if object_type != "index" or not self.name.startswith("sqlite_autoindex_"):
                raise ValueError("only SQLite autoindexes may have a null DDL digest")
        else:
            _sha256(self.ddl_sha256, "schema object DDL sha256")

    def to_dict(self) -> Dict[str, object]:
        return {
            "ddlSha256": self.ddl_sha256,
            "name": self.name,
            "objectType": self.object_type,
            "owner": self.owner,
            "profile": self.profile,
            "tableName": self.table_name,
        }


@dataclass(frozen=True)
class TrustedBackupTopologyProfile:
    name: str
    migration_id: Optional[int]
    dependencies: Tuple[str, ...]
    objects: Tuple[TrustedBackupSchemaObject, ...]
    profile_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        name = _profile_name(self.name, "topology profile name")
        if self.migration_id is not None:
            if type(self.migration_id) is not int or self.migration_id <= 0:
                raise ValueError("topology profile migration ID must be a positive exact integer")
        dependencies = _bounded_tuple(
            self.dependencies,
            maximum=_MAX_PROFILE_DEPENDENCIES,
            label="topology profile dependencies",
        )
        objects = _bounded_tuple(
            self.objects,
            maximum=_MAX_PROFILE_OBJECTS,
            label="topology profile objects",
        )
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(self, "objects", objects)
        for dependency in dependencies:
            _profile_name(dependency, "topology profile dependency")
            if dependency == name:
                raise ValueError("topology profile cannot depend on itself")
        if len(set(dependencies)) != len(dependencies):
            raise ValueError("topology profile dependencies must not contain duplicates")
        if dependencies != tuple(sorted(dependencies, key=lambda item: item.encode("utf-8"))):
            raise ValueError("topology profile dependencies must use canonical order")
        for item in objects:
            if type(item) is not TrustedBackupSchemaObject:
                raise TypeError("topology profile objects must be exact trusted schema objects")
            if item.profile != name:
                raise ValueError("schema object profile differs from its containing profile")
        coordinates = tuple((item.object_type, item.name) for item in objects)
        if len(set(coordinates)) != len(coordinates):
            raise ValueError("topology profile schema objects must not contain duplicates")
        expected_order = tuple(
            sorted(
                objects,
                key=lambda item: (
                    item.object_type.encode("utf-8"),
                    item.name.encode("utf-8"),
                    item.table_name.encode("utf-8"),
                ),
            )
        )
        if objects != expected_order:
            raise ValueError("topology profile schema objects must use canonical order")
        object.__setattr__(self, "profile_sha256", _canonical_json_sha256(self._digest_dict()))

    def _digest_dict(self) -> Dict[str, object]:
        return {
            "dependencies": list(self.dependencies),
            "format": BACKUP_TOPOLOGY_PROFILE_FORMAT,
            "migrationId": self.migration_id,
            "name": self.name,
            "objects": [item.to_dict() for item in self.objects],
            "presenceMode": _PRESENCE_MODE,
        }


@dataclass(frozen=True)
class TrustedBackupTopologyRegistry:
    profiles: Tuple[TrustedBackupTopologyProfile, ...]
    registry_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        profiles = _bounded_tuple(
            self.profiles,
            maximum=_MAX_PROFILE_COUNT,
            label="backup topology profiles",
        )
        object.__setattr__(self, "profiles", profiles)
        for profile in profiles:
            if type(profile) is not TrustedBackupTopologyProfile:
                raise TypeError("backup topology profiles must be exact trusted profiles")
        names = tuple(profile.name for profile in profiles)
        if len(set(names)) != len(names):
            raise ValueError("backup topology profile names must be unique")
        if names != tuple(sorted(names, key=lambda item: item.encode("utf-8"))):
            raise ValueError("backup topology profiles must use canonical order")
        known = set(names)
        global_coordinates: Set[Tuple[str, str]] = set()
        migration_ids: Set[int] = set()
        for profile in profiles:
            unknown = set(profile.dependencies) - known
            if unknown:
                raise ValueError("backup topology profile has an unknown dependency")
            if profile.migration_id is not None:
                if profile.migration_id in migration_ids:
                    raise ValueError("backup topology migration profile IDs must be unique")
                migration_ids.add(profile.migration_id)
            for item in profile.objects:
                coordinate = (item.object_type, item.name)
                if coordinate in global_coordinates:
                    raise ValueError("backup topology schema coordinates must be globally unique")
                global_coordinates.add(coordinate)
        profiles_by_name = {profile.name: profile for profile in profiles}
        visiting: Set[str] = set()
        visited: Set[str] = set()

        def visit(profile_name: str) -> None:
            if profile_name in visited:
                return
            if profile_name in visiting:
                raise ValueError("backup topology profile dependencies must be acyclic")
            visiting.add(profile_name)
            for dependency in profiles_by_name[profile_name].dependencies:
                visit(dependency)
            visiting.remove(profile_name)
            visited.add(profile_name)

        for profile_name in names:
            visit(profile_name)
        object.__setattr__(self, "registry_sha256", _canonical_json_sha256(self._digest_dict()))

    def _digest_dict(self) -> Dict[str, object]:
        return {
            "format": BACKUP_TOPOLOGY_REGISTRY_FORMAT,
            "profiles": [
                {"name": profile.name, "profileSha256": profile.profile_sha256}
                for profile in self.profiles
            ],
            "topologyProfile": BACKUP_TOPOLOGY_PROFILE,
        }

    def profile(self, name: object) -> TrustedBackupTopologyProfile:
        exact_name = _profile_name(name, "topology profile name")
        for profile in self.profiles:
            if profile.name == exact_name:
                return profile
        raise ValueError("topology profile is not present in the trusted registry")

    def migration_profile(self, migration_id: object) -> TrustedBackupTopologyProfile:
        if type(migration_id) is not int or migration_id <= 0:
            raise ValueError("migration profile ID must be a positive exact integer")
        for profile in self.profiles:
            if profile.migration_id == migration_id:
                return profile
        raise ValueError("migration profile ID is not present in the trusted registry")

    def objects_for_profiles(
        self,
        names: Iterable[str],
    ) -> Tuple[TrustedBackupSchemaObject, ...]:
        snapshot = _bounded_tuple(
            names,
            maximum=_MAX_PROFILE_COUNT,
            label="present topology profiles",
        )
        normalized_names = tuple(
            _profile_name(name, "present topology profile") for name in snapshot
        )
        if len(set(normalized_names)) != len(normalized_names):
            raise ValueError("present topology profiles must not contain duplicates")
        profiles = tuple(self.profile(name) for name in normalized_names)
        return tuple(
            sorted(
                (item for profile in profiles for item in profile.objects),
                key=lambda item: (
                    item.object_type.encode("utf-8"),
                    item.name.encode("utf-8"),
                    item.table_name.encode("utf-8"),
                    item.profile.encode("utf-8"),
                ),
            )
        )


def _schema_object(
    profile: str,
    owner: str,
    object_type: str,
    name: str,
    table_name: str,
    ddl_sha256: Optional[str],
) -> TrustedBackupSchemaObject:
    return TrustedBackupSchemaObject(
        profile=profile,
        owner=owner,
        object_type=object_type,
        name=name,
        table_name=table_name,
        ddl_sha256=ddl_sha256,
    )


def _profile(
    name: str,
    owner: str,
    objects: Iterable[Tuple[str, str, str, Optional[str]]],
    *,
    migration_id: Optional[int] = None,
    dependencies: Tuple[str, ...] = (),
) -> TrustedBackupTopologyProfile:
    normalized = tuple(
        sorted(
            (
                _schema_object(name, owner, object_type, object_name, table_name, ddl_sha256)
                for object_type, object_name, table_name, ddl_sha256 in objects
            ),
            key=lambda item: (
                item.object_type.encode("utf-8"),
                item.name.encode("utf-8"),
                item.table_name.encode("utf-8"),
            ),
        )
    )
    return TrustedBackupTopologyProfile(
        name=name,
        migration_id=migration_id,
        dependencies=tuple(sorted(dependencies, key=lambda item: item.encode("utf-8"))),
        objects=normalized,
    )


_EVENT_STORE_CORE = _profile(
    EVENT_STORE_CORE_PROFILE,
    "event_store",
    (
        (
            "index",
            "idx_events_correlation",
            "events",
            "9b35e84424a46c1cf7ab1be323b603927b71f1d20f87a98a29e695f5aac1dff0",
        ),
        (
            "index",
            "idx_events_stream",
            "events",
            "0370bfd8cb15889d99f8c5d1c6369ab4f3c1caec38631f9d3f8ab965336d14b3",
        ),
        (
            "index",
            "idx_inbox_event",
            "inbox_receipts",
            "a60f7476f70ff5113019360676cddf017502c33c395991fcfc2a40240fd0de4f",
        ),
        (
            "index",
            "idx_outbox_delivery",
            "outbox",
            "95ecc56b3016b7d99e54bf9dd897981d90e21cd184ccdbe0109dd8f220de0614",
        ),
        (
            "index",
            "idx_outbox_trigger",
            "outbox",
            "f5bbc8795f219395946a77c8403692ca936b8ace79a1d79a51f5673ce6e0810e",
        ),
        ("index", "sqlite_autoindex_events_1", "events", None),
        ("index", "sqlite_autoindex_events_2", "events", None),
        ("index", "sqlite_autoindex_events_3", "events", None),
        ("index", "sqlite_autoindex_inbox_receipts_1", "inbox_receipts", None),
        ("index", "sqlite_autoindex_outbox_1", "outbox", None),
        ("index", "sqlite_autoindex_outbox_2", "outbox", None),
        ("index", "sqlite_autoindex_snapshots_1", "snapshots", None),
        (
            "table",
            "events",
            "events",
            "99ee301502047f78fe562df08b5ac77916a3a6dece3d4b8121e726386f2c1dc0",
        ),
        (
            "table",
            "inbox_receipts",
            "inbox_receipts",
            "16bcaefd6f92578a00514f2593960f67c5d174dcb41b94d57aa574edc5a1db16",
        ),
        (
            "table",
            "outbox",
            "outbox",
            "1cf57dc4603eea1300ca9f49e86a864cc1c50716b60e20c071399e5e2089f39f",
        ),
        (
            "table",
            "snapshots",
            "snapshots",
            "d039ae50820b358ce6004a327c88c335b5e1a88fa56c9e8e364fa83d080108fb",
        ),
        (
            "table",
            "sqlite_sequence",
            "sqlite_sequence",
            "4cb1eaf14467f226196148cb5688569660cb290d414bae4c1c450b149b62befd",
        ),
    ),
)

_PROJECTION_STORE = _profile(
    PROJECTION_STORE_PROFILE,
    "projection_store",
    (
        (
            "index",
            "idx_projection_receipts_position",
            "projection_receipts",
            "1ea5180ba735652ccce10095114a40e612d9c627e2544084dbc8d4ca401b7958",
        ),
        ("index", "sqlite_autoindex_projection_offsets_1", "projection_offsets", None),
        ("index", "sqlite_autoindex_projection_receipts_1", "projection_receipts", None),
        ("index", "sqlite_autoindex_projection_receipts_2", "projection_receipts", None),
        (
            "table",
            "projection_offsets",
            "projection_offsets",
            "bf05e8632e54c85487312367ea4fa587df8372cb97658ecfc46d38f530496b2f",
        ),
        (
            "table",
            "projection_receipts",
            "projection_receipts",
            "e0e99e96c4663f3e1c1b228c4eb119edff129ad85da41f7d6f263f06ae55cc24",
        ),
    ),
)

_REVOCATION_GUARD = _profile(
    REVOCATION_GUARD_PROFILE,
    "revocation_guard",
    (
        ("index", "sqlite_autoindex_qe_revocation_high_water_1", "qe_revocation_high_water", None),
        (
            "table",
            "qe_revocation_high_water",
            "qe_revocation_high_water",
            "646c5544e116ac7a529a58184ff638d6561a5ade3bf0abd415cb0b1607e738a1",
        ),
    ),
)

_LEGACY_MIGRATION_LEDGER = _profile(
    LEGACY_MIGRATION_LEDGER_PROFILE,
    "migration_ledger",
    (
        ("index", "sqlite_autoindex_qe_schema_migrations_1", "qe_schema_migrations", None),
        (
            "table",
            "qe_schema_migrations",
            "qe_schema_migrations",
            "5069ee29ea2116d4b6a81ab1d1557fde07c7f4b538c59dcf3890d2ec4a73cbfc",
        ),
    ),
)

_DOMAIN_MIGRATION_1 = _profile(
    _DOMAIN_MIGRATION_PROFILE_NAMES[0],
    "attempts",
    (
        (
            "index",
            "idx_invocation_attempts_job",
            "invocation_attempts",
            "5fac47cc7f038759ebecf0b47d9d2eb749f76459fe34f2c09d6a53504fa04d74",
        ),
        (
            "index",
            "idx_invocation_attempts_status",
            "invocation_attempts",
            "b2a7fce664294eca50deafef6eb6973fccf290bd6fe0aadc57c301e8a3edb161",
        ),
        (
            "index",
            "idx_invocation_jobs_claim",
            "invocation_jobs",
            "645ff6d8f76113ec8d8339a5c1793e72297a98fd87adc3d32aa774394b22f4ad",
        ),
        (
            "index",
            "idx_invocation_jobs_lease_expiry",
            "invocation_jobs",
            "482b8bca8237e03d842e20ca03f2bc0cbcfdf70e1452888b7960741b5bb2a5f7",
        ),
        (
            "index",
            "idx_invocation_jobs_session",
            "invocation_jobs",
            "fe381b176f9f46410a6f7975592eed2fe78dae7ce1dd3ae6758f8bb1591a9176",
        ),
        ("index", "sqlite_autoindex_invocation_attempts_1", "invocation_attempts", None),
        ("index", "sqlite_autoindex_invocation_attempts_2", "invocation_attempts", None),
        ("index", "sqlite_autoindex_invocation_attempts_3", "invocation_attempts", None),
        ("index", "sqlite_autoindex_invocation_jobs_1", "invocation_jobs", None),
        ("index", "sqlite_autoindex_invocation_jobs_2", "invocation_jobs", None),
        ("index", "sqlite_autoindex_invocation_jobs_3", "invocation_jobs", None),
        ("index", "sqlite_autoindex_invocation_jobs_4", "invocation_jobs", None),
        (
            "table",
            "invocation_attempts",
            "invocation_attempts",
            "5606618188d69f34cc092909dab26e9166ebc95df2d2467dac00bf26944b0ba4",
        ),
        (
            "table",
            "invocation_jobs",
            "invocation_jobs",
            "36273db6dbec193faf37ca8f23b68efc1471a6fe51c74679a646e8253fe21ae8",
        ),
    ),
    migration_id=1,
)

_DOMAIN_MIGRATION_2 = _profile(
    _DOMAIN_MIGRATION_PROFILE_NAMES[1],
    "artifacts",
    (
        (
            "index",
            "idx_artifact_versions_digest",
            "artifact_versions",
            "6f96c49420ce234a4f3f93a757647613040e9432430e1e0aa0152f47ef34a6a7",
        ),
        (
            "index",
            "idx_artifact_versions_head",
            "artifact_versions",
            "cb903e3efc219003501022cf9e03bc7527c09d0040878758f3a9de5caff78995",
        ),
        (
            "index",
            "idx_artifact_versions_task",
            "artifact_versions",
            "56b79bb782d84f96b26087766b823a007dd04b6b2c0c521330fdc3e4aef82efb",
        ),
        ("index", "sqlite_autoindex_artifact_blobs_1", "artifact_blobs", None),
        ("index", "sqlite_autoindex_artifact_versions_1", "artifact_versions", None),
        ("index", "sqlite_autoindex_artifact_versions_2", "artifact_versions", None),
        ("index", "sqlite_autoindex_artifact_versions_3", "artifact_versions", None),
        (
            "table",
            "artifact_blobs",
            "artifact_blobs",
            "2c32324870b0be6b8f5ea524575912ff0eb08be9be10cc3cae28e96069cc35e9",
        ),
        (
            "table",
            "artifact_versions",
            "artifact_versions",
            "5fdaf59eed765b0f5b9ebddf1140e78d79a87803cf4d2a847a9dd596e447f9d6",
        ),
    ),
    migration_id=2,
)

_DOMAIN_MIGRATION_3 = _profile(
    _DOMAIN_MIGRATION_PROFILE_NAMES[2],
    "delivery",
    (
        (
            "index",
            "idx_outbox_ambiguities_one_open",
            "outbox_ambiguities",
            "252c5aa2e457c9da3372fb61a852e090f41772354b1de7d2d9af5516b6c6a907",
        ),
        (
            "index",
            "idx_outbox_ambiguities_opened",
            "outbox_ambiguities",
            "122b2f52d304a314989b57231fd0fffb07c25bd11a192aec26e02595c27131ea",
        ),
        ("index", "sqlite_autoindex_outbox_ambiguities_1", "outbox_ambiguities", None),
        (
            "table",
            "outbox_ambiguities",
            "outbox_ambiguities",
            "59f6e65b66ba31d65ffc6bc61a3c773b51695e84916e1fa55a9178065ce5dc5e",
        ),
    ),
    migration_id=3,
    dependencies=(EVENT_STORE_CORE_PROFILE,),
)

_DOMAIN_MIGRATION_SIDECAR = _profile(
    DOMAIN_MIGRATION_SIDECAR_PROFILE,
    "domain_migration_sidecar",
    (
        (
            "index",
            "sqlite_autoindex_qe_schema_migration_dependencies_1",
            "qe_schema_migration_dependencies",
            None,
        ),
        (
            "index",
            "sqlite_autoindex_qe_schema_migration_metadata_1",
            "qe_schema_migration_metadata",
            None,
        ),
        (
            "table",
            "qe_schema_migration_dependencies",
            "qe_schema_migration_dependencies",
            "8ffc91e7a06761cfada80fbbdc48c3aeef79bc308ad5a75a3bea661764c2be03",
        ),
        (
            "table",
            "qe_schema_migration_metadata",
            "qe_schema_migration_metadata",
            "cb3521ae73c56f4eaeed571d1a7ed062d40c51d27ccd0ab9a38c630409ed1506",
        ),
    ),
)

BACKUP_TOPOLOGY_REGISTRY = TrustedBackupTopologyRegistry(
    profiles=tuple(
        sorted(
            (
                _EVENT_STORE_CORE,
                _PROJECTION_STORE,
                _REVOCATION_GUARD,
                _LEGACY_MIGRATION_LEDGER,
                _DOMAIN_MIGRATION_1,
                _DOMAIN_MIGRATION_2,
                _DOMAIN_MIGRATION_3,
                _DOMAIN_MIGRATION_SIDECAR,
            ),
            key=lambda item: item.name.encode("utf-8"),
        )
    )
)


def _validate_domain_migration_profiles() -> None:
    descriptors = DOMAIN_MIGRATION_REGISTRY.descriptors
    registry_ids = {
        profile.migration_id
        for profile in BACKUP_TOPOLOGY_REGISTRY.profiles
        if profile.migration_id is not None
    }
    descriptor_ids = {descriptor.migration_id for descriptor in descriptors}
    if registry_ids != descriptor_ids:
        raise RuntimeError("backup topology migration profiles differ from the domain registry")
    for descriptor in descriptors:
        profile = BACKUP_TOPOLOGY_REGISTRY.migration_profile(descriptor.migration_id)
        explicit_objects = {
            (item.object_type, item.name, item.ddl_sha256)
            for item in profile.objects
            if item.ddl_sha256 is not None
        }
        owned_objects = {
            (item.object_type, item.name, item.ddl_sha256) for item in descriptor.owned_objects
        }
        if explicit_objects != owned_objects:
            raise RuntimeError("backup topology migration objects differ from the domain registry")


_validate_domain_migration_profiles()


__all__ = [
    "BACKUP_TOPOLOGY_PROFILE",
    "BACKUP_TOPOLOGY_REGISTRY",
    "BACKUP_TOPOLOGY_REGISTRY_FORMAT",
    "DOMAIN_MIGRATION_SIDECAR_PROFILE",
    "EVENT_STORE_CORE_PROFILE",
    "LEGACY_MIGRATION_LEDGER_PROFILE",
    "PROJECTION_STORE_PROFILE",
    "REVOCATION_GUARD_PROFILE",
    "TrustedBackupSchemaObject",
    "TrustedBackupTopologyProfile",
    "TrustedBackupTopologyRegistry",
    "backup_schema_ddl_sha256",
    "canonicalize_backup_schema_sql",
]
