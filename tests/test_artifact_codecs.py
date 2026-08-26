from __future__ import annotations

import json
import unicodedata
import unittest

from quantum_entanglement._artifact_codec import (
    ARTIFACT_METADATA_DOMAIN_V1,
    ArtifactMetadataCodecTooLargeError,
    artifact_blob_digest_v1,
    artifact_metadata_digest_v1,
    artifact_request_digest_v1,
    canonical_artifact_metadata_v1,
    decode_canonical_artifact_metadata_v1,
)


class ArtifactCanonicalCodecTests(unittest.TestCase):
    def test_golden_metadata_blob_and_legacy_request_digests(self) -> None:
        metadata = canonical_artifact_metadata_v1({"β": [True, None], "a": 1})
        content = b"hello\x00world"
        blob_digest = artifact_blob_digest_v1(content)

        self.assertEqual(metadata.canonical_bytes, b'{"a":1,"\xce\xb2":[true,null]}')
        self.assertEqual(
            artifact_metadata_digest_v1(metadata),
            "45bb713addfa4f91d18d7b0b59618ef6a41875168d43f5c55c00c89da416cc8b",
        )
        self.assertEqual(
            blob_digest,
            "sha256:b206899bc103669c8e7b36de29d73f95b46795b508aa87d612b2ce84bfb29df2",
        )
        self.assertEqual(
            artifact_request_digest_v1(
                tenant_id="tenant-a",
                workspace_id="workspace-a",
                session_id="session-a",
                task_id="task-a",
                name="report.md",
                media_type="text/markdown",
                blob_digest=blob_digest,
                byte_size=len(content),
                metadata=metadata,
                created_by="agent-a",
            ),
            "980fb603821d12ea722f3d950b2f82812e310972c38e3fc2ed90f81df84cffbe",
        )
        self.assertTrue(ARTIFACT_METADATA_DOMAIN_V1.endswith("\n"))

    def test_metadata_snapshot_and_decoded_values_are_independent(self) -> None:
        nested: list[object] = [{"value": "original"}]
        source: dict[str, object] = {"nested": nested}
        metadata = canonical_artifact_metadata_v1(source)

        nested[0] = {"value": "mutated"}
        source["later"] = True
        first = metadata.to_dict()
        first["nested"][0]["value"] = "wire-mutated"  # type: ignore[index]

        self.assertEqual(metadata.to_dict(), {"nested": [{"value": "original"}]})
        self.assertEqual(
            decode_canonical_artifact_metadata_v1(metadata.canonical_bytes),
            metadata,
        )
        self.assertNotIn("original", repr(metadata))

    def test_decoder_rejects_noncanonical_and_duplicate_json(self) -> None:
        invalid = (
            b' {"a":1}',
            b'{"a":1}\n',
            b'{"b":2,"a":1}',
            b'{"a":1,"a":1}',
            b'{"a":NaN}',
            b"[]",
            b"\xff",
        )
        for encoded in invalid:
            with self.subTest(encoded=encoded):
                with self.assertRaises((TypeError, ValueError)):
                    decode_canonical_artifact_metadata_v1(encoded)

    def test_metadata_rejects_unsafe_types_unicode_numbers_cycles_and_bounds(self) -> None:
        cycle: dict[str, object] = {}
        cycle["self"] = cycle
        decomposed = unicodedata.normalize("NFD", "résumé")
        too_deep: object = "leaf"
        for _ in range(65):
            too_deep = [too_deep]
        invalid: tuple[object, ...] = (
            [],
            {1: "bad-key"},
            {"value": object()},
            {"value": float("nan")},
            {"value": float("inf")},
            {"value": 1 << 4_096},
            {"value": decomposed},
            {decomposed: "value"},
            {"value": "bad\x00text"},
            {"value": "\ud800"},
            cycle,
            {"deep": too_deep},
            {str(index): index for index in range(5_001)},
        )
        for metadata in invalid:
            with self.subTest(metadata_type=type(metadata).__name__):
                with self.assertRaises((TypeError, ValueError)):
                    canonical_artifact_metadata_v1(metadata)

        with self.assertRaises(ArtifactMetadataCodecTooLargeError):
            canonical_artifact_metadata_v1({"payload": "x" * 65_536})

    def test_digest_functions_reject_coercion(self) -> None:
        metadata = canonical_artifact_metadata_v1({})
        for content in (bytearray(b"x"), memoryview(b"x"), "x"):
            with self.subTest(content_type=type(content).__name__):
                with self.assertRaises(TypeError):
                    artifact_blob_digest_v1(content)
        with self.assertRaises(TypeError):
            artifact_request_digest_v1(
                tenant_id="tenant",
                workspace_id="workspace",
                session_id="session",
                task_id="task",
                name="name",
                media_type="text/plain",
                blob_digest=artifact_blob_digest_v1(b"x"),
                byte_size=True,
                metadata=metadata,
                created_by="agent",
            )

    def test_key_order_does_not_change_canonical_bytes(self) -> None:
        first = canonical_artifact_metadata_v1({"b": 2, "a": 1})
        second = canonical_artifact_metadata_v1(json.loads('{"a":1,"b":2}'))
        self.assertEqual(first, second)
        self.assertEqual(first.canonical_bytes, b'{"a":1,"b":2}')


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
