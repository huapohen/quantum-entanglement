#!/usr/bin/env python3
# ruff: noqa: UP006, UP035, UP045
"""Generate and verify source-bound wheel/sdist integrity manifests."""

from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import io
import os
import platform
import re
import stat
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Mapping, NoReturn, Optional, Sequence, Tuple, cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts.generate_release_evidence import (  # noqa: E402
    canonical_json,
    capture_git_snapshot,
)
from scripts.verify_release_evidence import (  # noqa: E402
    EvidenceVerificationError,
    load_canonical_evidence,
)

_FORMAT = "quantum-entanglement.distribution-manifest"
_SCHEMA_VERSION = 1
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_FILE_BYTES = 32 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 10_000
_MAX_SOURCE_BYTES = 256 * 1024 * 1024
_MAX_SOURCE_FILES = 10_000
_MAX_PATH_BYTES = 1_024
_MAX_COMPRESSION_RATIO = 200
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_HASH_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_TOP_KEYS = frozenset(
    {
        "artifacts",
        "format",
        "generatedAt",
        "inspectionRuntime",
        "project",
        "schemaVersion",
        "source",
    }
)
_PROJECT_KEYS = frozenset({"name", "version"})
_SOURCE_KEYS = frozenset(
    {
        "commitSha",
        "commitShaAfterInspection",
        "dirty",
        "identityStable",
        "treeSha",
        "treeShaAfterInspection",
    }
)
_RUNTIME_KEYS = frozenset({"pythonImplementation", "pythonVersion"})
_ARTIFACT_KEYS = frozenset(
    {"byteSize", "contentSha256", "fileCount", "filename", "kind", "memberCount", "sha256"}
)
_EGG_INFO_FILES = frozenset(
    {
        "PKG-INFO",
        "SOURCES.txt",
        "dependency_links.txt",
        "entry_points.txt",
        "requires.txt",
        "top_level.txt",
    }
)


class DistributionManifestError(ValueError):
    """A fixed-code distribution validation failure safe for logs."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProjectIdentity:
    name: str
    version: str
    normalized_name: str


@dataclass(frozen=True)
class ArchiveInspection:
    evidence: Dict[str, object]
    files: Mapping[str, bytes]
    metadata: bytes
    entry_points: bytes
    top_level: bytes


def _fail(code: str) -> NoReturn:
    raise DistributionManifestError(code)


def _canonical_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        _fail("clock_invalid")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _content_digest(files: Mapping[str, bytes]) -> str:
    records = [
        {"name": name, "sha256": _sha256(value), "size": len(value)}
        for name, value in sorted(files.items())
    ]
    return _sha256(canonical_json({"files": records}).encode("utf-8"))


def _safe_archive_name(value: object, code: str) -> str:
    if type(value) is not str:
        _fail(code)
    name = value
    if (
        not name
        or not name.isascii()
        or len(name.encode("ascii")) > _MAX_PATH_BYTES
        or "\\" in name
        or "\x00" in name
        or name.startswith("/")
    ):
        _fail(code)
    normalized = name[:-1] if name.endswith("/") else name
    path = PurePosixPath(normalized)
    if not normalized or any(part in ("", ".", "..") for part in path.parts):
        _fail(code)
    if str(path) != normalized:
        _fail(code)
    return name


def _read_bounded_regular(path: Path, limit: int, code: str) -> bytes:
    try:
        before = path.lstat()
    except OSError:
        _fail(code)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        _fail(code)
    if before.st_size > limit:
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


def _source_project_identity(repository_root: Path) -> ProjectIdentity:
    pyproject = _read_bounded_regular(
        repository_root / "pyproject.toml", 1024 * 1024, "project_metadata_invalid"
    )
    try:
        lines = pyproject.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        _fail("project_metadata_invalid")
    in_project = False
    names: list[str] = []
    versions: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_project = line == "[project]"
            continue
        if not in_project:
            continue
        name_match = re.fullmatch(r'name\s*=\s*"([A-Za-z0-9._-]+)"', line)
        version_match = re.fullmatch(r'version\s*=\s*"([A-Za-z0-9._+-]+)"', line)
        if name_match is not None:
            names.append(name_match.group(1))
        if version_match is not None:
            versions.append(version_match.group(1))
    if len(names) != 1 or len(versions) != 1:
        _fail("project_metadata_invalid")
    name = names[0]
    version = versions[0]
    if _PROJECT_NAME_PATTERN.fullmatch(name) is None or _VERSION_PATTERN.fullmatch(version) is None:
        _fail("project_metadata_invalid")
    normalized_name = re.sub(r"[-_.]+", "_", name).lower()

    init_source = _read_bounded_regular(
        repository_root / "src" / normalized_name / "__init__.py",
        _MAX_ARCHIVE_FILE_BYTES,
        "project_version_invalid",
    )
    try:
        tree = ast.parse(init_source, filename="<package-init>")
    except (SyntaxError, ValueError):
        _fail("project_version_invalid")
    declared_versions: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "__version__":
            if not isinstance(node.value, ast.Constant) or type(node.value.value) is not str:
                _fail("project_version_invalid")
            declared_versions.append(node.value.value)
    if declared_versions != [version]:
        _fail("project_version_invalid")
    return ProjectIdentity(name=name, version=version, normalized_name=normalized_name)


def _git_tracked_source_files(repository_root: Path, identity: ProjectIdentity) -> Dict[str, bytes]:
    try:
        completed = subprocess.run(
            (
                "git",
                "-C",
                str(repository_root),
                "ls-files",
                "-z",
                "--",
                f"src/{identity.normalized_name}",
                "tests",
            ),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        _fail("source_inventory_unavailable")
    if completed.returncode != 0 or len(completed.stdout) > 8 * 1024 * 1024:
        _fail("source_inventory_unavailable")
    raw_names = completed.stdout.split(b"\x00")
    if raw_names and raw_names[-1] == b"":
        raw_names.pop()
    names: list[str] = []
    for raw_name in raw_names:
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError:
            _fail("source_inventory_invalid")
        _safe_archive_name(name, "source_inventory_invalid")
        if not (name.startswith(f"src/{identity.normalized_name}/") or name.startswith("tests/")):
            _fail("source_inventory_invalid")
        names.append(name)
    names.extend(("LICENSE", "README.md", "pyproject.toml"))
    if len(names) != len(set(names)) or len(names) > _MAX_SOURCE_FILES:
        _fail("source_inventory_invalid")

    result: Dict[str, bytes] = {}
    total = 0
    for name in sorted(names):
        value = _read_bounded_regular(
            repository_root / Path(*PurePosixPath(name).parts),
            _MAX_ARCHIVE_FILE_BYTES,
            "source_file_invalid",
        )
        total += len(value)
        if total > _MAX_SOURCE_BYTES:
            _fail("source_inventory_invalid")
        result[name] = value
    return result


def _metadata_identity(value: bytes, expected: ProjectIdentity, code: str) -> None:
    try:
        message = BytesParser(policy=policy.compat32).parsebytes(value)
    except (TypeError, ValueError):
        _fail(code)
    if message.defects:
        _fail(code)
    if message.get_all("Name") != [expected.name] or message.get_all("Version") != [
        expected.version
    ]:
        _fail(code)


def _artifact_evidence(
    *,
    filename: str,
    kind: str,
    raw: bytes,
    files: Mapping[str, bytes],
    member_count: int,
) -> Dict[str, object]:
    return {
        "byteSize": len(raw),
        "contentSha256": _content_digest(files),
        "fileCount": len(files),
        "filename": filename,
        "kind": kind,
        "memberCount": member_count,
        "sha256": _sha256(raw),
    }


def _validate_record(files: Mapping[str, bytes], record_name: str) -> None:
    try:
        text = files[record_name].decode("utf-8")
        rows = tuple(csv.reader(io.StringIO(text, newline="")))
    except (UnicodeDecodeError, csv.Error):
        _fail("wheel_record_invalid")
    records: Dict[str, Tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3:
            _fail("wheel_record_invalid")
        name = _safe_archive_name(row[0], "wheel_record_invalid")
        if name in records:
            _fail("wheel_record_invalid")
        records[name] = (row[1], row[2])
    if frozenset(records) != frozenset(files):
        _fail("wheel_record_invalid")
    for name, value in files.items():
        digest, size = records[name]
        if name == record_name:
            if digest or size:
                _fail("wheel_record_invalid")
            continue
        encoded = (
            base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=").decode("ascii")
        )
        if digest != f"sha256={encoded}" or size != str(len(value)):
            _fail("wheel_record_invalid")


def _inspect_wheel(
    path: Path,
    identity: ProjectIdentity,
    source_files: Mapping[str, bytes],
) -> ArchiveInspection:
    raw = _read_bounded_regular(path, _MAX_ARTIFACT_BYTES, "wheel_unreadable")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw), mode="r")
    except (OSError, ValueError, zipfile.BadZipFile):
        _fail("wheel_invalid")
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > _MAX_ARCHIVE_MEMBERS:
            _fail("wheel_invalid")
        files: Dict[str, bytes] = {}
        total = 0
        for info in infos:
            name = _safe_archive_name(info.filename, "wheel_path_invalid")
            if name in files or info.is_dir():
                _fail("wheel_members_invalid")
            if info.flag_bits & 0x1:
                _fail("wheel_encrypted")
            mode = info.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                _fail("wheel_link_invalid")
            if info.file_size > _MAX_ARCHIVE_FILE_BYTES:
                _fail("wheel_expansion_limit")
            total += info.file_size
            if total > _MAX_ARCHIVE_TOTAL_BYTES:
                _fail("wheel_expansion_limit")
            if info.file_size > max(1, info.compress_size) * _MAX_COMPRESSION_RATIO:
                _fail("wheel_compression_ratio")
            try:
                value = archive.read(info)
            except (OSError, RuntimeError, zipfile.BadZipFile):
                _fail("wheel_invalid")
            if len(value) != info.file_size:
                _fail("wheel_invalid")
            files[name] = value

    expected_package = {
        name[len("src/") :]: value
        for name, value in source_files.items()
        if name.startswith(f"src/{identity.normalized_name}/")
    }
    dist_info = f"{identity.normalized_name}-{identity.version}.dist-info"
    extras = {
        f"{dist_info}/licenses/LICENSE",
        f"{dist_info}/METADATA",
        f"{dist_info}/WHEEL",
        f"{dist_info}/entry_points.txt",
        f"{dist_info}/top_level.txt",
        f"{dist_info}/RECORD",
    }
    if frozenset(files) != frozenset(expected_package) | extras:
        _fail("wheel_members_invalid")
    for name, value in expected_package.items():
        if files[name] != value:
            _fail("wheel_source_mismatch")
    if files[f"{dist_info}/licenses/LICENSE"] != source_files["LICENSE"]:
        _fail("wheel_source_mismatch")
    metadata = files[f"{dist_info}/METADATA"]
    _metadata_identity(metadata, identity, "wheel_metadata_invalid")
    wheel_metadata = files[f"{dist_info}/WHEEL"]
    try:
        wheel_message = BytesParser(policy=policy.compat32).parsebytes(wheel_metadata)
    except (TypeError, ValueError):
        _fail("wheel_metadata_invalid")
    if wheel_message.defects or wheel_message.get_all("Tag") != ["py3-none-any"]:
        _fail("wheel_metadata_invalid")
    entry_points = files[f"{dist_info}/entry_points.txt"]
    if entry_points != (
        b"[console_scripts]\n"
        b"qe-admin = quantum_entanglement.admin_cli:main\n"
        b"qe-demo = quantum_entanglement.cli:main\n"
    ):
        _fail("wheel_entry_points_invalid")
    top_level = files[f"{dist_info}/top_level.txt"]
    if top_level != f"{identity.normalized_name}\n".encode("ascii"):
        _fail("wheel_top_level_invalid")
    _validate_record(files, f"{dist_info}/RECORD")
    return ArchiveInspection(
        evidence=_artifact_evidence(
            filename=path.name,
            kind="wheel",
            raw=raw,
            files=files,
            member_count=len(infos),
        ),
        files=files,
        metadata=metadata,
        entry_points=entry_points,
        top_level=top_level,
    )


def _inspect_sdist(
    path: Path,
    identity: ProjectIdentity,
    source_files: Mapping[str, bytes],
) -> ArchiveInspection:
    raw = _read_bounded_regular(path, _MAX_ARTIFACT_BYTES, "sdist_unreadable")
    try:
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    except (OSError, EOFError, tarfile.TarError):
        _fail("sdist_invalid")
    root = f"{identity.normalized_name}-{identity.version}"
    with archive:
        try:
            members = archive.getmembers()
        except (OSError, EOFError, tarfile.TarError):
            _fail("sdist_invalid")
        if not members or len(members) > _MAX_ARCHIVE_MEMBERS:
            _fail("sdist_invalid")
        files: Dict[str, bytes] = {}
        directories: set[str] = set()
        total = 0
        seen: set[str] = set()
        for member in members:
            name = _safe_archive_name(member.name, "sdist_path_invalid")
            if name in seen or not (name == root or name.startswith(root + "/")):
                _fail("sdist_members_invalid")
            seen.add(name)
            if member.isdir():
                directories.add(name)
                continue
            if not member.isfile() or member.issparse():
                _fail("sdist_link_or_special_file")
            if member.size > _MAX_ARCHIVE_FILE_BYTES:
                _fail("sdist_expansion_limit")
            total += member.size
            if total > _MAX_ARCHIVE_TOTAL_BYTES:
                _fail("sdist_expansion_limit")
            extracted = archive.extractfile(member)
            if extracted is None:
                _fail("sdist_invalid")
            try:
                value = extracted.read(_MAX_ARCHIVE_FILE_BYTES + 1)
            except (OSError, EOFError, tarfile.TarError):
                _fail("sdist_invalid")
            if len(value) != member.size:
                _fail("sdist_invalid")
            files[name] = value

    egg_info = f"src/{identity.normalized_name}.egg-info"
    source_archive_files = {f"{root}/{name}": value for name, value in source_files.items()}
    generated_relative = {f"{egg_info}/{name}" for name in _EGG_INFO_FILES}
    generated_relative.update(("PKG-INFO", "setup.cfg"))
    expected_files = frozenset(source_archive_files) | {
        f"{root}/{name}" for name in generated_relative
    }
    if frozenset(files) != expected_files:
        _fail("sdist_members_invalid")
    expected_directories = {root}
    for name in expected_files:
        path_parts = PurePosixPath(name).parts
        for index in range(1, len(path_parts)):
            expected_directories.add("/".join(path_parts[:index]))
    if directories != expected_directories:
        _fail("sdist_directories_invalid")
    for name, value in source_archive_files.items():
        if files[name] != value:
            _fail("sdist_source_mismatch")

    metadata = files[f"{root}/PKG-INFO"]
    if metadata != files[f"{root}/{egg_info}/PKG-INFO"]:
        _fail("sdist_metadata_invalid")
    _metadata_identity(metadata, identity, "sdist_metadata_invalid")
    if files[f"{root}/setup.cfg"] != b"[egg_info]\ntag_build = \ntag_date = 0\n\n":
        _fail("sdist_setup_invalid")
    entry_points = files[f"{root}/{egg_info}/entry_points.txt"]
    top_level = files[f"{root}/{egg_info}/top_level.txt"]
    sources = files[f"{root}/{egg_info}/SOURCES.txt"]
    try:
        source_lines = sources.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        _fail("sdist_sources_invalid")
    expected_source_lines = frozenset(source_files) | {
        f"{egg_info}/{name}" for name in _EGG_INFO_FILES
    }
    if (
        len(source_lines) != len(set(source_lines))
        or frozenset(source_lines) != expected_source_lines
    ):
        _fail("sdist_sources_invalid")
    return ArchiveInspection(
        evidence=_artifact_evidence(
            filename=path.name,
            kind="sdist",
            raw=raw,
            files=files,
            member_count=len(members),
        ),
        files=files,
        metadata=metadata,
        entry_points=entry_points,
        top_level=top_level,
    )


def _inspect_distributions(
    repository_root: Path,
    distribution_directory: Path,
) -> Tuple[ProjectIdentity, Tuple[Dict[str, object], ...]]:
    identity = _source_project_identity(repository_root)
    source_files = _git_tracked_source_files(repository_root, identity)
    try:
        directory = distribution_directory.resolve(strict=True)
        directory_stat = directory.lstat()
    except OSError:
        _fail("distribution_directory_unavailable")
    if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_ISLNK(directory_stat.st_mode):
        _fail("distribution_directory_invalid")
    expected_wheel = f"{identity.normalized_name}-{identity.version}-py3-none-any.whl"
    expected_sdist = f"{identity.normalized_name}-{identity.version}.tar.gz"
    try:
        children = tuple(directory.iterdir())
    except OSError:
        _fail("distribution_directory_unavailable")
    if {item.name for item in children} != {expected_wheel, expected_sdist}:
        _fail("distribution_set_invalid")

    wheel = _inspect_wheel(directory / expected_wheel, identity, source_files)
    sdist = _inspect_sdist(directory / expected_sdist, identity, source_files)
    if (
        wheel.metadata != sdist.metadata
        or wheel.entry_points != sdist.entry_points
        or wheel.top_level != sdist.top_level
    ):
        _fail("distribution_metadata_mismatch")
    return identity, (sdist.evidence, wheel.evidence)


def generate_distribution_manifest(
    repository_root: Path,
    distribution_directory: Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Dict[str, object]:
    """Inspect exactly one wheel and sdist and bind them to a clean source tree."""

    try:
        root = repository_root.resolve(strict=True)
    except OSError:
        _fail("repository_unavailable")
    before = capture_git_snapshot(root)
    if before.commit_sha is None or before.tree_sha is None:
        _fail("repository_identity_unavailable")
    if before.dirty is not False:
        _fail("repository_not_clean")
    identity, artifacts = _inspect_distributions(root, distribution_directory)
    after = capture_git_snapshot(root)
    if after.commit_sha is None or after.tree_sha is None:
        _fail("repository_identity_unavailable")
    if after.dirty is not False:
        _fail("repository_changed_during_inspection")
    stable = before.commit_sha == after.commit_sha and before.tree_sha == after.tree_sha
    if not stable:
        _fail("repository_changed_during_inspection")
    generated_at = _canonical_utc(clock())
    return {
        "artifacts": list(artifacts),
        "format": _FORMAT,
        "generatedAt": generated_at,
        "inspectionRuntime": {
            "pythonImplementation": platform.python_implementation(),
            "pythonVersion": platform.python_version(),
        },
        "project": {"name": identity.name, "version": identity.version},
        "schemaVersion": _SCHEMA_VERSION,
        "source": {
            "commitSha": before.commit_sha,
            "commitShaAfterInspection": after.commit_sha,
            "dirty": False,
            "identityStable": True,
            "treeSha": before.tree_sha,
            "treeShaAfterInspection": after.tree_sha,
        },
    }


def _object(value: object, keys: frozenset[str], code: str) -> Dict[str, object]:
    if type(value) is not dict:
        _fail(code)
    result = cast(Dict[str, object], value)
    if frozenset(result) != keys:
        _fail(code)
    return result


def _validate_manifest_shape(manifest: Mapping[str, object]) -> None:
    root = _object(dict(manifest), _TOP_KEYS, "manifest_shape_invalid")
    if root["format"] != _FORMAT or type(root["format"]) is not str:
        _fail("manifest_format_invalid")
    if type(root["schemaVersion"]) is not int or root["schemaVersion"] != _SCHEMA_VERSION:
        _fail("manifest_schema_invalid")
    generated_at = root["generatedAt"]
    if type(generated_at) is not str or _UTC_PATTERN.fullmatch(generated_at) is None:
        _fail("manifest_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        _fail("manifest_timestamp_invalid")
    if _canonical_utc(parsed) != generated_at:
        _fail("manifest_timestamp_invalid")
    project = _object(root["project"], _PROJECT_KEYS, "manifest_project_invalid")
    project_name = project["name"]
    project_version = project["version"]
    if type(project_name) is not str or _PROJECT_NAME_PATTERN.fullmatch(project_name) is None:
        _fail("manifest_project_invalid")
    if type(project_version) is not str or _VERSION_PATTERN.fullmatch(project_version) is None:
        _fail("manifest_project_invalid")
    runtime = _object(root["inspectionRuntime"], _RUNTIME_KEYS, "manifest_runtime_invalid")
    if any(type(value) is not str or not value for value in runtime.values()):
        _fail("manifest_runtime_invalid")
    source = _object(root["source"], _SOURCE_KEYS, "manifest_source_invalid")
    for key in ("commitSha", "commitShaAfterInspection", "treeSha", "treeShaAfterInspection"):
        source_hash = source[key]
        if type(source_hash) is not str or _GIT_HASH_PATTERN.fullmatch(source_hash) is None:
            _fail("manifest_source_invalid")
    if source["dirty"] is not False or source["identityStable"] is not True:
        _fail("manifest_source_invalid")
    artifacts = root["artifacts"]
    if type(artifacts) is not list or len(cast(list[object], artifacts)) != 2:
        _fail("manifest_artifacts_invalid")
    kinds: list[str] = []
    for raw_artifact in cast(list[object], artifacts):
        artifact = _object(raw_artifact, _ARTIFACT_KEYS, "manifest_artifact_invalid")
        if type(artifact["kind"]) is not str or artifact["kind"] not in ("sdist", "wheel"):
            _fail("manifest_artifact_invalid")
        kinds.append(artifact["kind"])
        _safe_archive_name(artifact["filename"], "manifest_artifact_invalid")
        for key in ("byteSize", "fileCount", "memberCount"):
            integer_value = artifact[key]
            if type(integer_value) is not int or integer_value <= 0:
                _fail("manifest_artifact_invalid")
        for key in ("contentSha256", "sha256"):
            digest = artifact[key]
            if type(digest) is not str or _HASH_PATTERN.fullmatch(digest) is None:
                _fail("manifest_artifact_invalid")
    if kinds != ["sdist", "wheel"]:
        _fail("manifest_artifacts_invalid")


def load_distribution_manifest(path: Path) -> Dict[str, object]:
    try:
        manifest = load_canonical_evidence(path)
    except EvidenceVerificationError:
        _fail("manifest_file_invalid")
    _validate_manifest_shape(manifest)
    return manifest


def verify_distribution_manifest(
    manifest: Mapping[str, object],
    repository_root: Path,
    distribution_directory: Path,
    *,
    expected_commit_sha: Optional[str] = None,
) -> None:
    """Reinspect distributions and require exact manifest/source equivalence."""

    _validate_manifest_shape(manifest)
    try:
        root = repository_root.resolve(strict=True)
    except OSError:
        _fail("repository_unavailable")
    before = capture_git_snapshot(root)
    if before.commit_sha is None or before.tree_sha is None:
        _fail("repository_identity_unavailable")
    if before.dirty is not False:
        _fail("repository_not_clean")
    if expected_commit_sha is not None:
        if _GIT_HASH_PATTERN.fullmatch(expected_commit_sha) is None:
            _fail("expected_commit_invalid")
        if before.commit_sha != expected_commit_sha:
            _fail("expected_commit_mismatch")
    identity, artifacts = _inspect_distributions(root, distribution_directory)
    source = cast(Mapping[str, object], manifest["source"])
    if (
        source["commitSha"] != before.commit_sha
        or source["commitShaAfterInspection"] != before.commit_sha
        or source["treeSha"] != before.tree_sha
        or source["treeShaAfterInspection"] != before.tree_sha
    ):
        _fail("manifest_source_mismatch")
    project = cast(Mapping[str, object], manifest["project"])
    if project != {"name": identity.name, "version": identity.version}:
        _fail("manifest_project_mismatch")
    runtime = cast(Mapping[str, object], manifest["inspectionRuntime"])
    if runtime != {
        "pythonImplementation": platform.python_implementation(),
        "pythonVersion": platform.python_version(),
    }:
        _fail("manifest_runtime_mismatch")
    if manifest["artifacts"] != list(artifacts):
        _fail("manifest_artifact_mismatch")
    after = capture_git_snapshot(root)
    if (
        after.commit_sha != before.commit_sha
        or after.tree_sha != before.tree_sha
        or after.dirty is not False
    ):
        _fail("repository_changed_during_verification")


def verify_distribution_manifest_file(
    manifest_path: Path,
    repository_root: Path,
    distribution_directory: Path,
    *,
    expected_commit_sha: Optional[str] = None,
) -> None:
    """Load an out-of-tree canonical manifest and verify it against source and dist."""

    try:
        root = repository_root.resolve(strict=True)
        resolved_manifest = manifest_path.resolve(strict=True)
    except OSError:
        _fail("manifest_file_invalid")
    try:
        resolved_manifest.relative_to(root)
    except ValueError:
        pass
    else:
        _fail("manifest_inside_repository")
    manifest = load_distribution_manifest(manifest_path)
    verify_distribution_manifest(
        manifest,
        root,
        distribution_directory,
        expected_commit_sha=expected_commit_sha,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate or verify canonical distribution manifests."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate", help="inspect dist and emit canonical JSON")
    generate.add_argument("--repository-root", type=Path, default=_REPOSITORY_ROOT)
    generate.add_argument("--distribution-directory", type=Path, default=Path("dist"))
    verify = subparsers.add_parser("verify", help="verify canonical JSON and distributions")
    verify.add_argument("manifest", type=Path)
    verify.add_argument("--repository-root", type=Path, default=_REPOSITORY_ROOT)
    verify.add_argument("--distribution-directory", type=Path, default=Path("dist"))
    verify.add_argument("--expected-commit")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "generate":
            manifest = generate_distribution_manifest(
                arguments.repository_root,
                arguments.distribution_directory,
            )
            sys.stdout.write(canonical_json(manifest))
            return 0
        verify_distribution_manifest_file(
            arguments.manifest,
            arguments.repository_root,
            arguments.distribution_directory,
            expected_commit_sha=arguments.expected_commit,
        )
    except DistributionManifestError as exc:
        print(f"distribution manifest failed: {exc.code}", file=sys.stderr)
        return 1
    print("distribution manifest verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
