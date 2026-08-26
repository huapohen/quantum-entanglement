from __future__ import annotations

import hashlib
import json
import unicodedata
import unittest
from dataclasses import replace

from quantum_entanglement.invocation_execution import EffectClass
from quantum_entanglement.invocation_results import (
    EMPTY_ACTION_RECEIPT_SET_DIGEST,
    SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION,
    ScopedInvocationResultArtifactV2,
    ScopedInvocationResultManifestV2,
)


def valid_artifact(
    *,
    artifact_id: str = "artifact-result-1",
    name: str = "analysis.md",
    version: int = 1,
    parent_version: int | None = None,
    idempotency_key: str = "result-artifact:invocation-result-1:1",
) -> ScopedInvocationResultArtifactV2:
    return ScopedInvocationResultArtifactV2(
        artifact_id=artifact_id,
        name=name,
        version=version,
        parent_version=parent_version,
        media_type="text/markdown",
        blob_digest="sha256:" + ("a" * 64),
        byte_size=23,
        metadata_digest="b" * 64,
        created_by="agent-result-1",
        idempotency_key=idempotency_key,
        request_digest="c" * 64,
    )


def valid_manifest(
    *,
    artifacts: tuple[ScopedInvocationResultArtifactV2, ...] | None = None,
    effect_class: EffectClass = EffectClass.PURE,
    action_receipt_set_digest: str = EMPTY_ACTION_RECEIPT_SET_DIGEST,
    result_ref: str = "artifact-result-1",
) -> ScopedInvocationResultManifestV2:
    return ScopedInvocationResultManifestV2(
        schema_version=SCOPED_INVOCATION_RESULT_MANIFEST_SCHEMA_VERSION,
        tenant_id="tenant-result-1",
        workspace_id="workspace-result-1",
        invocation_id="invocation-result-1",
        session_id="session-result-1",
        plan_id="plan-result-1",
        task_id="task-result-1",
        agent_id="agent-result-1",
        job_idempotency_key="invoke:task-result-1",
        task_revision=7,
        correlation_id="correlation-result-1",
        causation_id="task-result-1",
        runtime_revision="runtime:sha256:" + ("d" * 64),
        execution_manifest_digest="e" * 64,
        effect_class=effect_class,
        action_receipt_set_digest=action_receipt_set_digest,
        result_ref=result_ref,
        artifacts=(valid_artifact(),) if artifacts is None else artifacts,
    )


class ScopedInvocationResultArtifactCodecTests(unittest.TestCase):
    def test_artifact_round_trip_is_exact_and_capability_free(self) -> None:
        artifact = valid_artifact()

        wire = artifact.to_dict()
        decoded = ScopedInvocationResultArtifactV2.from_dict(wire)

        self.assertEqual(decoded, artifact)
        self.assertIsNot(decoded, artifact)
        self.assertEqual(
            json.loads(json.dumps(wire, allow_nan=False, sort_keys=True)),
            wire,
        )
        self.assertNotIn("lease", repr(decoded).lower())

    def test_artifact_decoder_rejects_changed_schema_shape_and_subclasses(self) -> None:
        wire = valid_artifact().to_dict()
        for changed in (
            {**wire, "future": True},
            {key: value for key, value in wire.items() if key != "requestDigest"},
            dict(wire, version=True),
            dict(wire, byteSize=-1),
            dict(wire, blobDigest="A" * 64),
            dict(wire, metadataDigest="sha256:" + ("b" * 64)),
            dict(wire, mediaType="text / markdown"),
        ):
            with self.subTest(changed=changed):
                with self.assertRaises((TypeError, ValueError)):
                    ScopedInvocationResultArtifactV2.from_dict(changed)

        class ArtifactSubclass(ScopedInvocationResultArtifactV2):
            pass

        with self.assertRaisesRegex(TypeError, "exact"):
            ArtifactSubclass.from_dict(wire)

    def test_artifact_lineage_and_unicode_are_canonical(self) -> None:
        with self.assertRaisesRegex(ValueError, "parentVersion"):
            replace(valid_artifact(), version=2)
        with self.assertRaisesRegex(ValueError, "precede"):
            replace(valid_artifact(), version=3, parent_version=1)
        version_two = replace(valid_artifact(), version=2, parent_version=1)
        self.assertEqual(version_two.parent_version, 1)

        decomposed = unicodedata.normalize("NFD", "résumé.md")
        self.assertNotEqual(decomposed, unicodedata.normalize("NFC", decomposed))
        with self.assertRaisesRegex(ValueError, "NFC"):
            replace(valid_artifact(), name=decomposed)


class ScopedInvocationResultManifestCodecTests(unittest.TestCase):
    def test_manifest_round_trip_and_domain_separated_digest_are_deterministic(self) -> None:
        manifest = valid_manifest()

        wire = manifest.to_dict()
        decoded = ScopedInvocationResultManifestV2.from_dict(wire)
        canonical = json.dumps(
            wire,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        expected = hashlib.sha256(
            b"quantum-entanglement.invocation-result-manifest/2\n" + canonical
        ).hexdigest()

        self.assertEqual(decoded, manifest)
        self.assertIsNot(decoded, manifest)
        self.assertIsNot(decoded.artifacts, manifest.artifacts)
        self.assertEqual(manifest.canonical_bytes(), canonical)
        self.assertEqual(manifest.canonical_digest(), expected)
        self.assertNotEqual(manifest.canonical_digest(), hashlib.sha256(canonical).hexdigest())

    def test_decoder_snapshots_nested_wire_values(self) -> None:
        wire = valid_manifest().to_dict()
        decoded = ScopedInvocationResultManifestV2.from_dict(wire)

        wire["tenantId"] = "tenant-mutated"
        artifacts = wire["artifacts"]
        self.assertIs(type(artifacts), list)
        artifacts[0]["artifactId"] = "artifact-mutated"  # type: ignore[index]
        artifacts.append(valid_artifact(artifact_id="later").to_dict())  # type: ignore[union-attr]

        self.assertEqual(decoded.tenant_id, "tenant-result-1")
        self.assertEqual(decoded.artifacts[0].artifact_id, "artifact-result-1")
        self.assertEqual(len(decoded.artifacts), 1)

    def test_manifest_rejects_legacy_future_and_non_exact_wire_shapes(self) -> None:
        wire = valid_manifest().to_dict()
        for changed in (
            dict(wire, schemaVersion=1),
            dict(wire, schemaVersion=3),
            dict(wire, schemaVersion=True),
            {**wire, "future": "field"},
            {key: value for key, value in wire.items() if key != "workspaceId"},
            dict(wire, artifacts=tuple(wire["artifacts"])),
            dict(wire, taskRevision=True),
        ):
            with self.subTest(changed=changed):
                with self.assertRaises((TypeError, ValueError)):
                    ScopedInvocationResultManifestV2.from_dict(changed)

        class ManifestSubclass(ScopedInvocationResultManifestV2):
            pass

        with self.assertRaisesRegex(TypeError, "exact"):
            ManifestSubclass.from_dict(wire)

    def test_manifest_binds_primary_artifact_agent_and_unique_artifact_set(self) -> None:
        first = valid_artifact()
        second = valid_artifact(
            artifact_id="artifact-result-2",
            name="evidence.json",
            idempotency_key="result-artifact:invocation-result-1:2",
        )
        manifest = valid_manifest(artifacts=(first, second), result_ref=second.artifact_id)
        self.assertEqual(manifest.result_ref, second.artifact_id)

        invalid_sets = (
            ((first,), "missing-result-ref"),
            ((first, replace(first, name="other.md")), first.artifact_id),
            ((first, replace(second, name=first.name)), first.artifact_id),
            (
                (first, replace(second, idempotency_key=first.idempotency_key)),
                first.artifact_id,
            ),
            ((replace(first, created_by="agent-other"),), first.artifact_id),
        )
        for artifacts, result_ref in invalid_sets:
            with self.subTest(artifacts=artifacts, result_ref=result_ref):
                with self.assertRaises(ValueError):
                    valid_manifest(artifacts=artifacts, result_ref=result_ref)

    def test_effect_class_and_action_receipt_set_cannot_contradict(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty action receipt"):
            valid_manifest(action_receipt_set_digest="f" * 64)
        with self.assertRaisesRegex(ValueError, "non-empty action receipt"):
            valid_manifest(effect_class=EffectClass.IDEMPOTENT)

        effectful = valid_manifest(
            effect_class=EffectClass.IDEMPOTENT,
            action_receipt_set_digest="f" * 64,
        )
        self.assertIs(effectful.effect_class, EffectClass.IDEMPOTENT)

    def test_manifest_collection_is_bounded_and_immutable(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1 and 256"):
            valid_manifest(artifacts=())
        with self.assertRaisesRegex(TypeError, "exact tuple"):
            replace(valid_manifest(), artifacts=[valid_artifact()])  # type: ignore[arg-type]

        many = tuple(
            valid_artifact(
                artifact_id=f"artifact-{index}",
                name=f"artifact-{index}.txt",
                idempotency_key=f"artifact-key-{index}",
            )
            for index in range(257)
        )
        with self.assertRaisesRegex(ValueError, "between 1 and 256"):
            valid_manifest(artifacts=many, result_ref="artifact-0")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
