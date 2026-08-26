from __future__ import annotations

import hashlib
import json
import unicodedata
import unittest
from dataclasses import replace

from quantum_entanglement.invocation_execution import (
    SCOPED_INVOCATION_EXECUTION_MANIFEST_DOMAIN,
    TASK_EXECUTION_REQUESTED_EVENT_TYPE,
    EffectClass,
    InvocationExecutionManifest,
    ScopedInvocationExecutionManifestV2,
)


def valid_manifest_dict() -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "tenantId": "tenant-scoped-1",
        "workspaceId": "workspace-scoped-1",
        "invocationId": "invocation-scoped-1",
        "sessionId": "session-scoped-1",
        "planId": "plan-scoped-1",
        "taskId": "task-scoped-1",
        "agentId": "agent-scoped-1",
        "jobIdempotencyKey": "invoke:task-scoped-1",
        "taskRevision": 19,
        "correlationId": "correlation-scoped-1",
        "causationId": "task-scoped-1",
        "envelopeDigest": "a" * 64,
        "contextDigest": "b" * 64,
        "authorizationDigest": "c" * 64,
        "runtimeRevision": "runtime:sha256:" + ("d" * 64),
        "effectClass": "pure",
        "retryClass": "never",
    }


def valid_legacy_manifest_dict() -> dict[str, object]:
    scoped = valid_manifest_dict()
    del scoped["tenantId"]
    del scoped["workspaceId"]
    scoped["schemaVersion"] = 1
    return scoped


class ScopedInvocationExecutionManifestTests(unittest.TestCase):
    def test_exact_round_trip_and_domain_separated_digest(self) -> None:
        raw = valid_manifest_dict()

        manifest = ScopedInvocationExecutionManifestV2.from_dict(raw)

        self.assertEqual(manifest.to_dict(), raw)
        expected_bytes = json.dumps(
            raw,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.assertEqual(manifest.canonical_bytes(), expected_bytes)
        self.assertEqual(
            manifest.canonical_digest(),
            hashlib.sha256(
                SCOPED_INVOCATION_EXECUTION_MANIFEST_DOMAIN.encode("utf-8") + expected_bytes
            ).hexdigest(),
        )
        self.assertEqual(
            ScopedInvocationExecutionManifestV2.from_event_payload(
                TASK_EXECUTION_REQUESTED_EVENT_TYPE,
                raw,
            ),
            manifest,
        )

    def test_scope_is_covered_by_the_manifest_digest(self) -> None:
        manifest = ScopedInvocationExecutionManifestV2.from_dict(valid_manifest_dict())

        other_tenant = replace(manifest, tenant_id="tenant-scoped-2")
        other_workspace = replace(manifest, workspace_id="workspace-scoped-2")

        self.assertNotEqual(manifest.canonical_digest(), other_tenant.canonical_digest())
        self.assertNotEqual(manifest.canonical_digest(), other_workspace.canonical_digest())
        self.assertNotEqual(other_tenant.canonical_digest(), other_workspace.canonical_digest())

    def test_legacy_and_scoped_schemas_cannot_upcast_each_other(self) -> None:
        legacy = valid_legacy_manifest_dict()
        scoped = valid_manifest_dict()

        legacy_model = InvocationExecutionManifest.from_dict(legacy)
        scoped_model = ScopedInvocationExecutionManifestV2.from_dict(scoped)

        with self.assertRaises(ValueError):
            ScopedInvocationExecutionManifestV2.from_dict(legacy)
        with self.assertRaises(ValueError):
            InvocationExecutionManifest.from_dict(scoped)
        with self.assertRaises(TypeError):
            ScopedInvocationExecutionManifestV2.from_dict(legacy_model)
        with self.assertRaises(TypeError):
            InvocationExecutionManifest.from_dict(scoped_model)

    def test_exact_field_set_and_schema_version_fail_closed(self) -> None:
        baseline = valid_manifest_dict()

        missing = dict(baseline)
        del missing["workspaceId"]
        extra = dict(baseline)
        extra["scope"] = "forbidden"
        future = dict(baseline)
        future["schemaVersion"] = 3
        bool_version = dict(baseline)
        bool_version["schemaVersion"] = True

        for label, raw in (
            ("missing", missing),
            ("extra", extra),
            ("future", future),
            ("bool", bool_version),
        ):
            with self.subTest(case=label):
                with self.assertRaises((TypeError, ValueError)):
                    ScopedInvocationExecutionManifestV2.from_dict(raw)

        with self.assertRaises(TypeError):
            ScopedInvocationExecutionManifestV2.from_dict(tuple(baseline.items()))
        with self.assertRaises(TypeError):
            ScopedInvocationExecutionManifestV2.from_dict({1: "non-string-key"})

    def test_all_identity_fields_require_bounded_nfc_plain_text(self) -> None:
        identity_fields = (
            "tenantId",
            "workspaceId",
            "invocationId",
            "sessionId",
            "planId",
            "taskId",
            "agentId",
            "jobIdempotencyKey",
            "correlationId",
            "runtimeRevision",
        )
        invalid_values = (
            "",
            " padded",
            "padded ",
            "line\nfeed",
            "delete\x7f",
            "x" * 4_097,
            "surrogate-\ud800",
            unicodedata.normalize("NFD", "é"),
            7,
        )
        for field_name in identity_fields:
            for value in invalid_values:
                with self.subTest(field=field_name, value=repr(value)[:40]):
                    raw = valid_manifest_dict()
                    raw[field_name] = value
                    if field_name == "taskId":
                        raw["causationId"] = value
                    with self.assertRaises((TypeError, ValueError, UnicodeError)):
                        ScopedInvocationExecutionManifestV2.from_dict(raw)

    def test_causation_is_exactly_the_scoped_task(self) -> None:
        raw = valid_manifest_dict()
        raw["causationId"] = "task-other"

        with self.assertRaisesRegex(ValueError, "causationId"):
            ScopedInvocationExecutionManifestV2.from_dict(raw)

    def test_revision_digests_and_enums_are_strict(self) -> None:
        invalid_revision_values = (True, 0, -1, (1 << 63), "1")
        for value in invalid_revision_values:
            with self.subTest(revision=value):
                raw = valid_manifest_dict()
                raw["taskRevision"] = value
                with self.assertRaises((TypeError, ValueError)):
                    ScopedInvocationExecutionManifestV2.from_dict(raw)

        for field_name in ("envelopeDigest", "contextDigest", "authorizationDigest"):
            for value in ("A" * 64, "a" * 63, "g" * 64, 1):
                with self.subTest(field=field_name, digest=value):
                    raw = valid_manifest_dict()
                    raw[field_name] = value
                    with self.assertRaises((TypeError, ValueError)):
                        ScopedInvocationExecutionManifestV2.from_dict(raw)

        for value in ("unknown", 1, True):
            with self.subTest(effect=value):
                raw = valid_manifest_dict()
                raw["effectClass"] = value
                with self.assertRaises((TypeError, ValueError)):
                    ScopedInvocationExecutionManifestV2.from_dict(raw)
            with self.subTest(retry=value):
                raw = valid_manifest_dict()
                raw["retryClass"] = value
                with self.assertRaises((TypeError, ValueError)):
                    ScopedInvocationExecutionManifestV2.from_dict(raw)

        for effect_class in EffectClass:
            raw = valid_manifest_dict()
            raw["effectClass"] = effect_class.value
            self.assertIs(
                ScopedInvocationExecutionManifestV2.from_dict(raw).effect_class,
                effect_class,
            )

    def test_event_decoder_rejects_every_other_vocabulary(self) -> None:
        for event_type in (
            "task.execution_requested",
            "task.invocation.started",
            "task.result.received",
            " task.execution.requested",
            1,
        ):
            with self.subTest(event_type=event_type):
                with self.assertRaises((TypeError, ValueError)):
                    ScopedInvocationExecutionManifestV2.from_event_payload(
                        event_type,
                        valid_manifest_dict(),
                    )

    def test_decoder_snapshots_input_and_rejects_subclass_dispatch(self) -> None:
        raw = valid_manifest_dict()
        manifest = ScopedInvocationExecutionManifestV2.from_dict(raw)
        raw["tenantId"] = "mutated-after-decode"

        self.assertEqual(manifest.tenant_id, "tenant-scoped-1")

        class ScopedSubclass(ScopedInvocationExecutionManifestV2):
            pass

        with self.assertRaisesRegex(TypeError, "exact schema-2 class"):
            ScopedSubclass.from_dict(valid_manifest_dict())
        with self.assertRaisesRegex(TypeError, "exact schema-2 class"):
            ScopedSubclass.from_event_payload(
                TASK_EXECUTION_REQUESTED_EVENT_TYPE,
                valid_manifest_dict(),
            )

    def test_canonical_bytes_are_stable_across_input_key_order(self) -> None:
        baseline = valid_manifest_dict()
        reversed_input = dict(reversed(tuple(baseline.items())))

        first = ScopedInvocationExecutionManifestV2.from_dict(baseline)
        second = ScopedInvocationExecutionManifestV2.from_dict(reversed_input)

        self.assertEqual(first, second)
        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertEqual(first.canonical_digest(), second.canonical_digest())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
