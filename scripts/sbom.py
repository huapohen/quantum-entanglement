#!/usr/bin/env python3
"""Generate and strictly verify source-bound CycloneDX runtime and build SBOMs."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast
from urllib.parse import quote

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts.distribution_manifest import (  # noqa: E402
    DistributionManifestError,
    load_distribution_manifest,
    verify_distribution_manifest_file,
)
from scripts.verify_dependency_locks import (  # noqa: E402
    DependencyLockError,
    LockTarget,
    verify_dependency_locks,
)

_SPEC_VERSION = "1.6"
_BOM_VERSION = 1
_RUNTIME_FILENAME = "quantum-entanglement-runtime.cdx.json"
_BUILD_FILENAME = "quantum-entanglement-build.cdx.json"
_FILENAMES = (_RUNTIME_FILENAME, _BUILD_FILENAME)
_MAX_SBOM_BYTES = 4 * 1024 * 1024
_MAX_SOURCE_BYTES = 1024 * 1024
_MAX_COMPONENTS = 512
_MAX_PROPERTIES = 128
_MAX_PROPERTY_VALUE_BYTES = 128 * 1024
_MAX_STRING_BYTES = 256 * 1024
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_HASH_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}$")
_WINDOWS_PATH_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
_TOP_KEYS = {
    "runtime": frozenset({"bomFormat", "dependencies", "metadata", "specVersion", "version"}),
    "build": frozenset(
        {"bomFormat", "components", "dependencies", "metadata", "specVersion", "version"}
    ),
}
_METADATA_KEYS = frozenset({"component", "tools"})
_TOOLS_KEYS = frozenset({"components"})
_COMPONENT_KEYS = frozenset(
    {"bom-ref", "licenses", "name", "properties", "purl", "type", "version"}
)
_PACKAGE_COMPONENT_KEYS = frozenset(
    {"bom-ref", "name", "properties", "purl", "type", "version"}
)
_TOOL_COMPONENT = {
    "bom-ref": "urn:quantum-entanglement:sbom-generator:1",
    "name": "quantum-entanglement-sbom-generator",
    "type": "application",
    "version": "1",
}


class SbomError(ValueError):
    """A fixed-code SBOM failure safe for release logs."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise SbomError(code)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True, separators=(",", ": "))
        + "\n"
    ).encode("ascii")


def _read_regular(path: Path, limit: int, code: str) -> bytes:
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


def _directory(path: Path, code: str) -> tuple[Path, tuple[int, int]]:
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
        after = path.lstat()
    except OSError:
        _fail(code)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        _fail(code)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        _fail(code)
    return resolved, (after.st_dev, after.st_ino)


def _outside_repository(
    directory: Path, repository_root: Path
) -> tuple[Path, tuple[int, int]]:
    resolved_directory, directory_identity = _directory(directory, "sbom_directory_invalid")
    resolved_root, _ = _directory(repository_root, "repository_root_invalid")
    try:
        resolved_directory.relative_to(resolved_root)
    except ValueError:
        return resolved_directory, directory_identity
    _fail("sbom_directory_inside_repository")


def _project_has_no_base_dependencies(repository_root: Path) -> None:
    source = _read_regular(
        repository_root / "pyproject.toml", _MAX_SOURCE_BYTES, "project_metadata_invalid"
    )
    try:
        lines = source.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        _fail("project_metadata_invalid")
    in_project = False
    candidates: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_project = line == "[project]"
            continue
        if in_project and line.startswith("dependencies ="):
            candidates.append(line.split("=", 1)[1].strip())
    if len(candidates) != 1:
        _fail("project_metadata_invalid")
    try:
        dependencies = ast.literal_eval(candidates[0])
    except (SyntaxError, ValueError):
        _fail("project_metadata_invalid")
    if type(dependencies) is not list or any(type(item) is not str for item in dependencies):
        _fail("project_metadata_invalid")
    if dependencies:
        _fail("runtime_dependencies_unlocked")


def _purl(name: str, version: str) -> str:
    return f"pkg:pypi/{quote(name, safe='-._~')}@{quote(version, safe='-._~')}"


def _property(name: str, value: object) -> dict[str, str]:
    return {"name": name, "value": str(value)}


def _manifest_identity(manifest: Mapping[str, object]) -> tuple[str, str, str, str]:
    raw_project = manifest.get("project")
    raw_source = manifest.get("source")
    if type(raw_project) is not dict or type(raw_source) is not dict:
        _fail("source_manifest_invalid")
    project = cast(Mapping[str, object], raw_project)
    source = cast(Mapping[str, object], raw_source)
    name = project.get("name")
    version = project.get("version")
    commit = source.get("commitSha")
    tree = source.get("treeSha")
    if (
        type(name) is not str
        or _NAME_PATTERN.fullmatch(name) is None
        or type(version) is not str
        or _VERSION_PATTERN.fullmatch(version) is None
        or type(commit) is not str
        or _GIT_HASH_PATTERN.fullmatch(commit) is None
        or type(tree) is not str
        or _GIT_HASH_PATTERN.fullmatch(tree) is None
    ):
        _fail("source_manifest_invalid")
    return name, version, commit, tree


def _artifact_properties(manifest: Mapping[str, object]) -> list[dict[str, str]]:
    raw_artifacts = manifest.get("artifacts")
    if type(raw_artifacts) is not list or len(raw_artifacts) != 2:
        _fail("source_manifest_invalid")
    properties: list[dict[str, str]] = []
    kinds: list[str] = []
    for value in raw_artifacts:
        if type(value) is not dict:
            _fail("source_manifest_invalid")
        artifact = cast(dict[str, object], value)
        kind = artifact.get("kind")
        filename = artifact.get("filename")
        digest = artifact.get("sha256")
        byte_size = artifact.get("byteSize")
        if (
            type(kind) is not str
            or kind not in ("sdist", "wheel")
            or type(filename) is not str
            or not filename.isascii()
            or PurePosixPath(filename).name != filename
            or type(digest) is not str
            or _HASH_PATTERN.fullmatch(digest) is None
            or type(byte_size) is not int
            or byte_size <= 0
        ):
            _fail("source_manifest_invalid")
        kinds.append(kind)
        prefix = f"quantum-entanglement:artifact:{kind}"
        properties.extend(
            (
                _property(f"{prefix}:byte-size", byte_size),
                _property(f"{prefix}:filename", filename),
                _property(f"{prefix}:sha256", digest),
            )
        )
    if kinds != ["sdist", "wheel"]:
        _fail("source_manifest_invalid")
    return properties


def _common_properties(manifest: Mapping[str, object]) -> list[dict[str, str]]:
    _, _, commit, tree = _manifest_identity(manifest)
    properties = _artifact_properties(manifest)
    properties.extend(
        (
            _property("quantum-entanglement:source:commit-sha", commit),
            _property("quantum-entanglement:source:tree-sha", tree),
        )
    )
    return sorted(properties, key=lambda item: (item["name"], item["value"]))


def _runtime_document(manifest: Mapping[str, object]) -> dict[str, object]:
    name, version, _, _ = _manifest_identity(manifest)
    root_ref = _purl(name, version)
    properties = _common_properties(manifest)
    properties.extend(
        (
            _property("quantum-entanglement:runtime:base-dependency-count", 0),
            _property(
                "quantum-entanglement:runtime:coverage",
                "base installation only; optional extras are excluded",
            ),
        )
    )
    properties.sort(key=lambda item: (item["name"], item["value"]))
    return {
        "bomFormat": "CycloneDX",
        "dependencies": [{"dependsOn": [], "ref": root_ref}],
        "metadata": {
            "component": {
                "bom-ref": root_ref,
                "licenses": [{"license": {"id": "MIT"}}],
                "name": name,
                "properties": properties,
                "purl": root_ref,
                "type": "library",
                "version": version,
            },
            "tools": {"components": [dict(_TOOL_COMPONENT)]},
        },
        "specVersion": _SPEC_VERSION,
        "version": _BOM_VERSION,
    }


def _target_label(target: LockTarget) -> str:
    return f"{target.scope}|cp{target.python_version}|{target.platform}"


def _build_components(targets: Sequence[LockTarget]) -> list[dict[str, object]]:
    aggregate: dict[tuple[str, str], dict[str, object]] = {}
    for target in targets:
        label = _target_label(target)
        for package in target.packages:
            key = (package.name, package.version)
            current = aggregate.setdefault(
                key,
                {"hashes": package.sha256, "targets": set()},
            )
            if current["hashes"] != package.sha256:
                _fail("lock_component_hash_mismatch")
            cast(set[str], current["targets"]).add(label)

    components: list[dict[str, object]] = []
    for (name, version), value in sorted(aggregate.items()):
        digests = cast(tuple[str, ...], value["hashes"])
        labels = sorted(cast(set[str], value["targets"]))
        reference = _purl(name, version)
        components.append(
            {
                "bom-ref": reference,
                "name": name,
                "properties": [
                    _property("quantum-entanglement:lock:artifact-sha256", ",".join(digests)),
                    _property("quantum-entanglement:lock:artifact-sha256-count", len(digests)),
                    _property("quantum-entanglement:lock:targets", ",".join(labels)),
                ],
                "purl": reference,
                "type": "library",
                "version": version,
            }
        )
    components.sort(key=lambda component: cast(str, component["bom-ref"]))
    if not components or len(components) > _MAX_COMPONENTS:
        _fail("lock_component_invalid")
    return components


def _build_document(
    manifest: Mapping[str, object], targets: Sequence[LockTarget]
) -> dict[str, object]:
    _, version, _, _ = _manifest_identity(manifest)
    root_ref = f"urn:quantum-entanglement:build-toolchain:{version}"
    components = _build_components(targets)
    properties = _common_properties(manifest)
    for target in targets:
        prefix = (
            "quantum-entanglement:lock:"
            f"{target.scope}:cp{target.python_version}:{target.platform}"
        )
        properties.extend(
            (
                _property(f"{prefix}:input-sha256", target.input_sha256),
                _property(f"{prefix}:lock-sha256", target.lock_sha256),
            )
        )
    properties.append(_property("quantum-entanglement:lock:target-count", len(targets)))
    properties.sort(key=lambda item: (item["name"], item["value"]))
    component_refs = [cast(str, component["bom-ref"]) for component in components]
    dependencies = [{"dependsOn": component_refs, "ref": root_ref}]
    dependencies.extend({"dependsOn": [], "ref": reference} for reference in component_refs)
    return {
        "bomFormat": "CycloneDX",
        "components": components,
        "dependencies": dependencies,
        "metadata": {
            "component": {
                "bom-ref": root_ref,
                "name": "quantum-entanglement-build-toolchain",
                "properties": properties,
                "type": "application",
                "version": version,
            },
            "tools": {"components": [dict(_TOOL_COMPONENT)]},
        },
        "specVersion": _SPEC_VERSION,
        "version": _BOM_VERSION,
    }


def generate_sbom_documents(
    repository_root: Path,
    manifest: Mapping[str, object],
    targets: Sequence[LockTarget],
) -> dict[str, bytes]:
    """Create deterministic runtime and build-toolchain CycloneDX documents."""

    _project_has_no_base_dependencies(repository_root)
    documents = {
        _RUNTIME_FILENAME: _canonical_json(_runtime_document(manifest)),
        _BUILD_FILENAME: _canonical_json(_build_document(manifest, targets)),
    }
    for filename, value in documents.items():
        kind = "runtime" if filename == _RUNTIME_FILENAME else "build"
        validate_sbom_bytes(value, kind=kind)
    return documents


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("sbom_json_invalid")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> NoReturn:
    _fail("sbom_json_invalid")


def _safe_string(value: object, code: str) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > _MAX_STRING_BYTES:
        _fail(code)
    if (
        value.startswith(("/", "file://"))
        or _WINDOWS_PATH_PATTERN.match(value) is not None
        or "\\" in value
    ):
        _fail("sbom_path_leak")
    return value


def _validate_properties(value: object) -> None:
    if type(value) is not list or len(value) > _MAX_PROPERTIES:
        _fail("sbom_property_invalid")
    properties = cast(list[object], value)
    normalized: list[tuple[str, str]] = []
    for raw_property in properties:
        if type(raw_property) is not dict or frozenset(raw_property) != {"name", "value"}:
            _fail("sbom_property_invalid")
        item = cast(dict[str, object], raw_property)
        name = _safe_string(item["name"], "sbom_property_invalid")
        property_value = _safe_string(item["value"], "sbom_property_invalid")
        if len(property_value.encode("utf-8")) > _MAX_PROPERTY_VALUE_BYTES:
            _fail("sbom_property_invalid")
        normalized.append((name, property_value))
    if normalized != sorted(normalized) or len({name for name, _ in normalized}) != len(normalized):
        _fail("sbom_property_invalid")


def _validate_component(value: object, *, package: bool) -> str:
    expected_keys = _PACKAGE_COMPONENT_KEYS if package else _COMPONENT_KEYS
    if type(value) is not dict or frozenset(value) != expected_keys:
        _fail("sbom_component_invalid")
    component = cast(dict[str, object], value)
    reference = _safe_string(component["bom-ref"], "sbom_component_invalid")
    name = _safe_string(component["name"], "sbom_component_invalid")
    version = _safe_string(component["version"], "sbom_component_invalid")
    component_type = component["type"]
    if component_type not in ("application", "library"):
        _fail("sbom_component_invalid")
    if package:
        purl = _safe_string(component["purl"], "sbom_component_invalid")
        if purl != reference or purl != _purl(name, version) or component_type != "library":
            _fail("sbom_component_invalid")
        _validate_properties(component["properties"])
    else:
        if "purl" in component:
            purl = _safe_string(component["purl"], "sbom_component_invalid")
            if purl != reference or purl != _purl(name, version):
                _fail("sbom_component_invalid")
        _validate_properties(component["properties"])
        licenses = component["licenses"]
        if licenses != [{"license": {"id": "MIT"}}]:
            _fail("sbom_component_invalid")
    return reference


def _validate_metadata(value: object, *, kind: str) -> str:
    if type(value) is not dict or frozenset(value) != _METADATA_KEYS:
        _fail("sbom_metadata_invalid")
    metadata = cast(dict[str, object], value)
    tools = metadata["tools"]
    if type(tools) is not dict or frozenset(tools) != _TOOLS_KEYS:
        _fail("sbom_metadata_invalid")
    tool_components = cast(dict[str, object], tools)["components"]
    if tool_components != [_TOOL_COMPONENT]:
        _fail("sbom_metadata_invalid")
    component = metadata["component"]
    if kind == "runtime":
        return _validate_component(component, package=False)
    if type(component) is not dict:
        _fail("sbom_component_invalid")
    root = cast(dict[str, object], component)
    expected = frozenset({"bom-ref", "name", "properties", "type", "version"})
    if frozenset(root) != expected:
        _fail("sbom_component_invalid")
    reference = _safe_string(root["bom-ref"], "sbom_component_invalid")
    name = _safe_string(root["name"], "sbom_component_invalid")
    version = _safe_string(root["version"], "sbom_component_invalid")
    if (
        name != "quantum-entanglement-build-toolchain"
        or root["type"] != "application"
        or reference != f"urn:quantum-entanglement:build-toolchain:{version}"
    ):
        _fail("sbom_component_invalid")
    _validate_properties(root["properties"])
    return reference


def _validate_dependencies(value: object, root_ref: str, component_refs: Sequence[str]) -> None:
    if type(value) is not list or len(value) != len(component_refs) + 1:
        _fail("sbom_dependency_invalid")
    dependencies = cast(list[object], value)
    expected_order = [root_ref, *component_refs]
    seen: set[str] = set()
    for index, raw_dependency in enumerate(dependencies):
        if type(raw_dependency) is not dict or frozenset(raw_dependency) != {"dependsOn", "ref"}:
            _fail("sbom_dependency_invalid")
        dependency = cast(dict[str, object], raw_dependency)
        reference = _safe_string(dependency["ref"], "sbom_dependency_invalid")
        depends_on = dependency["dependsOn"]
        if type(depends_on) is not list or any(type(item) is not str for item in depends_on):
            _fail("sbom_dependency_invalid")
        if reference != expected_order[index] or reference in seen:
            _fail("sbom_dependency_invalid")
        seen.add(reference)
        expected_dependencies = list(component_refs) if index == 0 else []
        if depends_on != expected_dependencies:
            _fail("sbom_dependency_invalid")


def validate_sbom_bytes(value: bytes, *, kind: str) -> dict[str, object]:
    """Reject noncanonical, oversized, structurally invalid, or graph-inconsistent SBOM bytes."""

    if kind not in _TOP_KEYS or not value or len(value) > _MAX_SBOM_BYTES:
        _fail("sbom_file_invalid")
    try:
        document = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (SbomError, json.JSONDecodeError, RecursionError, UnicodeDecodeError):
        _fail("sbom_json_invalid")
    if type(document) is not dict:
        _fail("sbom_shape_invalid")
    result = cast(dict[str, object], document)
    if _canonical_json(result) != value:
        _fail("sbom_noncanonical")
    if frozenset(result) != _TOP_KEYS[kind]:
        _fail("sbom_shape_invalid")
    if (
        result["bomFormat"] != "CycloneDX"
        or result["specVersion"] != _SPEC_VERSION
        or type(result["version"]) is not int
        or result["version"] != _BOM_VERSION
    ):
        _fail("sbom_shape_invalid")
    root_ref = _validate_metadata(result["metadata"], kind=kind)
    component_refs: list[str] = []
    if kind == "build":
        components = result["components"]
        if type(components) is not list or not components or len(components) > _MAX_COMPONENTS:
            _fail("sbom_component_invalid")
        for component in cast(list[object], components):
            component_refs.append(_validate_component(component, package=True))
        if component_refs != sorted(component_refs) or len(component_refs) != len(
            set(component_refs)
        ):
            _fail("sbom_component_invalid")
    _validate_dependencies(result["dependencies"], root_ref, component_refs)
    return result


def _write_exclusive(path: Path, value: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o644)
    except OSError:
        _fail("sbom_write_failed")
    try:
        offset = 0
        while offset < len(value):
            written = os.write(descriptor, value[offset:])
            if written <= 0:
                _fail("sbom_write_failed")
            offset += written
        os.fsync(descriptor)
    except OSError:
        _fail("sbom_write_failed")
    finally:
        os.close(descriptor)


def write_sbom_documents(
    output_directory: Path,
    documents: Mapping[str, bytes],
    *,
    repository_root: Path,
) -> None:
    """Write the exact document set once into an empty out-of-tree directory."""

    directory, directory_identity = _outside_repository(output_directory, repository_root)
    if tuple(sorted(documents)) != tuple(sorted(_FILENAMES)):
        _fail("sbom_document_set_invalid")
    try:
        children = tuple(directory.iterdir())
    except OSError:
        _fail("sbom_directory_invalid")
    if children:
        _fail("sbom_directory_not_empty")
    for filename in _FILENAMES:
        kind = "runtime" if filename == _RUNTIME_FILENAME else "build"
        validate_sbom_bytes(documents[filename], kind=kind)
        _write_exclusive(directory / filename, documents[filename])
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        _fail("sbom_write_failed")
    try:
        os.fsync(descriptor)
    except OSError:
        _fail("sbom_write_failed")
    finally:
        os.close(descriptor)
    resolved_after, identity_after = _directory(directory, "sbom_directory_invalid")
    try:
        names_after = {child.name for child in directory.iterdir()}
    except OSError:
        _fail("sbom_directory_invalid")
    if (
        resolved_after != directory
        or identity_after != directory_identity
        or names_after != set(_FILENAMES)
    ):
        _fail("sbom_directory_changed")


def verify_sbom_directory(
    sbom_directory: Path,
    expected_documents: Mapping[str, bytes],
    *,
    repository_root: Path,
) -> dict[str, bytes]:
    """Read exactly two SBOMs, validate them strictly, and compare canonical bytes."""

    directory, directory_identity = _outside_repository(sbom_directory, repository_root)
    try:
        children = tuple(directory.iterdir())
    except OSError:
        _fail("sbom_directory_invalid")
    if {child.name for child in children} != set(_FILENAMES):
        _fail("sbom_document_set_invalid")
    actual: dict[str, bytes] = {}
    for filename in _FILENAMES:
        value = _read_regular(directory / filename, _MAX_SBOM_BYTES, "sbom_file_invalid")
        kind = "runtime" if filename == _RUNTIME_FILENAME else "build"
        validate_sbom_bytes(value, kind=kind)
        if expected_documents.get(filename) != value:
            _fail("sbom_drift")
        actual[filename] = value
    resolved_after, identity_after = _directory(directory, "sbom_directory_invalid")
    try:
        names_after = {child.name for child in directory.iterdir()}
    except OSError:
        _fail("sbom_directory_invalid")
    if (
        resolved_after != directory
        or identity_after != directory_identity
        or names_after != set(_FILENAMES)
    ):
        _fail("sbom_directory_changed")
    return actual


def _source_bound_documents(
    repository_root: Path,
    distribution_directory: Path,
    distribution_manifest: Path,
    expected_commit: str,
) -> dict[str, bytes]:
    try:
        verify_distribution_manifest_file(
            distribution_manifest,
            repository_root,
            distribution_directory,
            expected_commit_sha=expected_commit,
        )
        manifest = load_distribution_manifest(distribution_manifest)
        targets = verify_dependency_locks(repository_root)
    except (DistributionManifestError, DependencyLockError):
        _fail("source_evidence_invalid")
    documents = generate_sbom_documents(repository_root, manifest, targets)
    try:
        verify_distribution_manifest_file(
            distribution_manifest,
            repository_root,
            distribution_directory,
            expected_commit_sha=expected_commit,
        )
    except DistributionManifestError:
        _fail("source_evidence_changed")
    return documents


def _summary(documents: Mapping[str, bytes]) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for filename in _FILENAMES:
        kind = "runtime" if filename == _RUNTIME_FILENAME else "build"
        document = validate_sbom_bytes(documents[filename], kind=kind)
        components = document.get("components", [])
        records.append(
            {
                "byteSize": len(documents[filename]),
                "componentCount": len(cast(list[object], components)),
                "filename": filename,
                "sha256": _sha256(documents[filename]),
            }
        )
    return {"documents": records, "verified": True}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("generate", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--repository-root", type=Path, default=_REPOSITORY_ROOT)
        subparser.add_argument("--distribution-directory", type=Path, default=Path("dist"))
        subparser.add_argument("--distribution-manifest", type=Path, required=True)
        subparser.add_argument("--sbom-directory", type=Path, required=True)
        subparser.add_argument("--expected-commit", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        documents = _source_bound_documents(
            args.repository_root,
            args.distribution_directory,
            args.distribution_manifest,
            args.expected_commit,
        )
        if args.command == "generate":
            write_sbom_documents(
                args.sbom_directory,
                documents,
                repository_root=args.repository_root,
            )
            verified = documents
        else:
            verified = verify_sbom_directory(
                args.sbom_directory,
                documents,
                repository_root=args.repository_root,
            )
    except SbomError as exc:
        print(f"SBOM operation failed: {exc.code}", file=sys.stderr)
        return 1
    print(json.dumps(_summary(verified), separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
