from __future__ import annotations

import asyncio
import base64
import ctypes
import errno
import hashlib
import json
import os
import socket
import sys
import tempfile
import tracemalloc
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from scripts import report_sync_bundle
from scripts.report_sync_bundle import (
    ReportSyncBundleError,
    canonical_json,
    generate_report_sync_bundle,
    main,
    verify_report_sync_bundle,
)


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_identity(path: Path) -> tuple[int, int]:
    metadata = path.stat()
    return metadata.st_dev, metadata.st_ino


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(data, zlib.crc32(chunk_type)) & 0xFFFFFFFF
    return len(data).to_bytes(4, "big") + chunk_type + data + checksum.to_bytes(4, "big")


class ReportSyncBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repository = Path(self.tempdir.name) / "repository"
        self.repository.mkdir()
        self._write("analysis_report/README.md", b"# Home\n")
        self._write(
            "analysis_report/multi_agent_collaboration_report.md",
            b"# Collaboration\n",
        )
        self._write(
            "analysis_report/NATIVE_IM_INTEGRATION_PREREQUISITES.md",
            b"# Native IM integration prerequisites\n",
        )
        self._write(
            "analysis_report/NATIVE_IM_EARLY_INTEGRATION_PLAN.md",
            b"# Native IM early integration plan\n",
        )
        self._write("analysis_report/NEXT_STAGE_PLAN.md", b"# Next stage\n")
        self._write(
            "analysis_report/PRE_NATIVE_IM_EARLY_INTEGRATION_CHECKPOINT_2026-08-27.md",
            b"# Pre-native-IM early integration checkpoint\n",
        )
        self._write(
            "analysis_report/STAGE_ACCEPTANCE_2026-08-27.md",
            b"# Stage acceptance\n",
        )
        self._write(
            "docs/architecture/NATIVE_IM_CONTRACT_V1.md",
            b"# Native IM provider contract V1\n",
        )
        self._write("docs/TERMINOLOGY.md", b"# Terminology\n")
        self._write("docs/wanwork_im/ARCHITECTURE.md", b"# WanWork IM architecture\n")
        self._write(
            "docs/wanwork_im/IMPLEMENTATION_PLAN.md",
            b"# WanWork IM implementation plan\n",
        )
        self._write(
            "docs/wanwork_im/RESEARCH_TRACEABILITY.md",
            b"# WanWork IM research traceability\n",
        )
        self._write("analysis_report/research/00_scope.md", b"# Scope\n")
        self._write("analysis_report/research/08_new_evidence.md", b"# New\n")
        self._write("analysis_report/screenshots/README.md", b"# Screenshots\n")
        self._write("analysis_report/yuque_sync/source/00_home.md", b"# Mirror\n")
        self.image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        self._write("analysis_report/screenshots/00_fixture.png", self.image)
        self._write_screenshot_manifest(
            [self._image_item("00_fixture.png", self.image, width=1, height=1)]
        )
        self._write_previous_manifests()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write(self, relative: str, value: bytes) -> Path:
        path = self.repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return path

    def _raw_hash(self, relative: str) -> str:
        return sha256((self.repository / relative).read_bytes())

    def _write_json(self, relative: str, value: object) -> None:
        self._write(relative, (json.dumps(value, ensure_ascii=False) + "\n").encode())

    def _image_item(
        self,
        filename: str,
        image: bytes,
        *,
        width: int,
        height: int,
        media_type: str = "image/png",
    ) -> dict[str, object]:
        return {
            "byteSize": len(image),
            "filename": filename,
            "height": height,
            "mediaType": media_type,
            "notForPublicDistribution": True,
            "redactionStatus": "unredacted-restricted-original",
            "sha256": sha256(image),
            "width": width,
        }

    def _write_screenshot_manifest(self, items: list[dict[str, object]]) -> None:
        self._write_json(
            "analysis_report/screenshots/manifest.json",
            {
                "accessClassification": "restricted-internal",
                "format": "quantum-entanglement.research-screenshot-manifest",
                "items": items,
            },
        )

    def _write_previous_manifests(self) -> None:
        pages = []
        for key, paths in (
            ("project-home", ["analysis_report/README.md"]),
            ("native-im-contract-v1", ["docs/architecture/NATIVE_IM_CONTRACT_V1.md"]),
            (
                "native-im-early-integration-plan",
                ["analysis_report/NATIVE_IM_EARLY_INTEGRATION_PLAN.md"],
            ),
            (
                "pre-native-im-early-integration-checkpoint",
                ["analysis_report/PRE_NATIVE_IM_EARLY_INTEGRATION_CHECKPOINT_2026-08-27.md"],
            ),
            (
                "comprehensive-report",
                ["analysis_report/multi_agent_collaboration_report.md"],
            ),
            ("research-00", ["analysis_report/research/00_scope.md"]),
            (
                "screenshot-evidence",
                [
                    "analysis_report/screenshots/README.md",
                    "analysis_report/screenshots/manifest.json",
                ],
            ),
        ):
            pages.append(
                {
                    "key": key,
                    "localFiles": [
                        {"path": path, "sha256": self._raw_hash(path)} for path in paths
                    ],
                    "readback": {"verified": True},
                }
            )
        self._write_json(
            "analysis_report/notion_sync_manifest.json",
            {
                "format": "quantum-entanglement.notion-sync-manifest",
                "pages": pages,
                "version": 1,
            },
        )

        mirror_path = "analysis_report/yuque_sync/source/00_home.md"
        canonical_path = "analysis_report/research/00_scope.md"
        self._write_json(
            "analysis_report/yuque_sync/mapping.json",
            {
                "objects": [
                    {
                        "normalized_sha256": self._raw_hash(mirror_path),
                        "source_path": mirror_path,
                        "verification": "verified_readback",
                        "yuque_slug": "fixture-home",
                    },
                    {
                        "normalized_sha256": self._raw_hash(canonical_path),
                        "source_path": canonical_path,
                        "verification": "verified_unchanged",
                        "yuque_slug": "fixture-scope",
                    },
                ],
                "schema_version": 2,
            },
        )

    def _save_bundle(self, payload: object, name: str = "bundle.json") -> Path:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_text(canonical_json(payload), encoding="utf-8")
        return path

    def _source_targets(
        self,
        payload: dict[str, object],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (page["path"], page["target"]): page
            for page in cast(list[dict[str, Any]], payload["sourceTargets"])
        }

    def test_generation_is_deterministic_multi_target_and_historically_qualified(
        self,
    ) -> None:
        first = generate_report_sync_bundle(self.repository)
        second = generate_report_sync_bundle(self.repository)
        self.assertEqual(first, second)
        rendered = canonical_json(first)
        self.assertEqual(rendered, canonical_json(second))
        self.assertNotIn("remote_verified", rendered)

        pages = self._source_targets(first)
        self.assertEqual(
            pages[("analysis_report/README.md", "notion")]["targetPageKey"],
            "project-home",
        )
        self.assertEqual(
            pages[("analysis_report/README.md", "notion")]["targetStatus"],
            "historical_manifest_claim_digest_match",
        )
        self.assertEqual(
            pages[("analysis_report/research/08_new_evidence.md", "notion")]["targetStatus"],
            "local_pending",
        )
        canonical_targets = {
            target for path, target in pages if path == "analysis_report/research/00_scope.md"
        }
        self.assertEqual(canonical_targets, {"notion", "yuque"})

        self.assertEqual(
            pages[("analysis_report/research/00_scope.md", "yuque")]["targetStatus"],
            "historical_manifest_claim_digest_match",
        )
        self.assertEqual(
            pages[("analysis_report/yuque_sync/source/00_home.md", "yuque")]["targetStatus"],
            "historical_manifest_claim_digest_match",
        )
        self.assertTrue(all(page["liveReadbackPerformed"] is False for page in pages.values()))
        screenshot_readme = pages[("analysis_report/screenshots/README.md", "notion")]
        screenshot_manifest = pages[("analysis_report/screenshots/manifest.json", "notion")]
        self.assertNotEqual(screenshot_readme["entryKey"], screenshot_manifest["entryKey"])
        self.assertEqual(screenshot_readme["targetPageKey"], "screenshot-evidence")
        self.assertEqual(screenshot_manifest["targetPageKey"], "screenshot-evidence")
        self.assertIsNone(screenshot_readme["proposedTargetPageKey"])
        self.assertIsNone(screenshot_manifest["proposedTargetPageKey"])
        self.assertEqual(screenshot_readme["targetStatus"], screenshot_manifest["targetStatus"])
        self.assertEqual(
            pages[("analysis_report/yuque_sync/source/00_home.md", "yuque")]["targetPageKey"],
            "fixture-home",
        )
        pending = pages[("analysis_report/research/08_new_evidence.md", "notion")]
        self.assertIsNone(pending["targetPageKey"])
        self.assertIsNotNone(pending["proposedTargetPageKey"])
        self.assertIn(
            "analysis_report/research/08_new_evidence.md",
            cast(dict[str, Any], first["previousManifestDiagnostics"])["notion"]["extra"],
        )

        image = cast(list[dict[str, Any]], first["images"])[0]
        self.assertEqual(image["sha256"], sha256(self.image))
        self.assertEqual(image["accessClassification"], "restricted-internal")
        self.assertIs(image["notForPublicDistribution"], True)
        self.assertEqual(
            first["imageDiagnostics"],
            {"unmanifestedPolicy": "fail-closed"},
        )
        source_summary = cast(dict[str, Any], first["sourceSummary"])
        self.assertEqual(source_summary["count"], 17)
        self.assertEqual(source_summary["sourceTargetCount"], 18)
        self.assertEqual(source_summary["notionTargetCount"], 16)
        self.assertEqual(source_summary["yuqueTargetCount"], 2)

        path = self._save_bundle(first)
        self.assertEqual(verify_report_sync_bundle(self.repository, path), first)

    def test_native_im_integration_decision_is_an_allowlisted_canonical_source(self) -> None:
        path = "analysis_report/NATIVE_IM_INTEGRATION_PREREQUISITES.md"
        self._write(path, b"# Native IM integration prerequisites\n")
        manifest_path = self.repository / "analysis_report/notion_sync_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["pages"].append(
            {
                "key": "native-im-integration-prerequisites",
                "localFiles": [{"path": path, "sha256": self._raw_hash(path)}],
                "readback": {"verified": True},
            }
        )
        self._write_json("analysis_report/notion_sync_manifest.json", manifest)

        target = self._source_targets(generate_report_sync_bundle(self.repository))[
            (path, "notion")
        ]
        self.assertEqual(target["targetPageKey"], "native-im-integration-prerequisites")
        self.assertEqual(target["targetStatus"], "historical_manifest_claim_digest_match")

    def test_native_im_provider_contract_is_an_allowlisted_canonical_source(self) -> None:
        path = "docs/architecture/NATIVE_IM_CONTRACT_V1.md"
        target = self._source_targets(generate_report_sync_bundle(self.repository))[
            (path, "notion")
        ]
        self.assertEqual(target["targetPageKey"], "native-im-contract-v1")
        self.assertEqual(target["targetStatus"], "historical_manifest_claim_digest_match")

    def test_wanwork_im_review_docs_are_allowlisted_canonical_sources(self) -> None:
        pages = self._source_targets(generate_report_sync_bundle(self.repository))
        for path in (
            "docs/wanwork_im/ARCHITECTURE.md",
            "docs/wanwork_im/IMPLEMENTATION_PLAN.md",
            "docs/wanwork_im/RESEARCH_TRACEABILITY.md",
        ):
            with self.subTest(path=path):
                target = pages[(path, "notion")]
                self.assertIsNone(target["targetPageKey"])
                self.assertEqual(target["targetStatus"], "local_pending")

    def test_native_im_early_integration_plan_is_an_allowlisted_canonical_source(self) -> None:
        path = "analysis_report/NATIVE_IM_EARLY_INTEGRATION_PLAN.md"
        target = self._source_targets(generate_report_sync_bundle(self.repository))[
            (path, "notion")
        ]
        self.assertEqual(target["targetPageKey"], "native-im-early-integration-plan")
        self.assertEqual(target["targetStatus"], "historical_manifest_claim_digest_match")

    def test_pre_native_im_checkpoint_is_an_allowlisted_canonical_source(self) -> None:
        path = "analysis_report/PRE_NATIVE_IM_EARLY_INTEGRATION_CHECKPOINT_2026-08-27.md"
        target = self._source_targets(generate_report_sync_bundle(self.repository))[
            (path, "notion")
        ]
        self.assertEqual(
            target["targetPageKey"],
            "pre-native-im-early-integration-checkpoint",
        )
        self.assertEqual(target["targetStatus"], "historical_manifest_claim_digest_match")

    def test_notion_manifest_v2_remote_readback_is_accepted(self) -> None:
        manifest_path = self.repository / "analysis_report/notion_sync_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = 2
        for page in manifest["pages"]:
            page["remoteReadback"] = page.pop("readback")
        self._write_json("analysis_report/notion_sync_manifest.json", manifest)

        payload = generate_report_sync_bundle(self.repository)
        home = self._source_targets(payload)[("analysis_report/README.md", "notion")]
        self.assertEqual(home["targetStatus"], "historical_manifest_claim_digest_match")

        manifest["version"] = 3
        self._write_json("analysis_report/notion_sync_manifest.json", manifest)
        with self.assertRaisesRegex(ReportSyncBundleError, "notion_manifest_invalid"):
            generate_report_sync_bundle(self.repository)

    def test_source_drift_fails_verification_and_becomes_local_pending(self) -> None:
        original = generate_report_sync_bundle(self.repository)
        bundle_path = self._save_bundle(original)
        stable_key = self._source_targets(original)[("analysis_report/README.md", "notion")][
            "entryKey"
        ]

        self._write("analysis_report/README.md", b"# Home changed\n")
        with self.assertRaisesRegex(ReportSyncBundleError, "bundle_hash_drift"):
            verify_report_sync_bundle(self.repository, bundle_path)

        current = generate_report_sync_bundle(self.repository)
        home = self._source_targets(current)[("analysis_report/README.md", "notion")]
        self.assertEqual(home["entryKey"], stable_key)
        self.assertEqual(home["targetStatus"], "local_pending")
        stale_paths = {
            entry["path"]
            for entry in cast(dict[str, Any], current["previousManifestDiagnostics"])["notion"][
                "stale"
            ]
        }
        self.assertIn("analysis_report/README.md", stale_paths)

    def test_controlled_source_names_symlinks_paths_and_duplicate_keys_are_rejected(
        self,
    ) -> None:
        outside = Path(self.tempdir.name) / "outside.md"
        outside.write_text("must not be read\n", encoding="utf-8")
        link = self.repository / "analysis_report/research/09_link.md"
        link.symlink_to(outside)
        with self.assertRaisesRegex(ReportSyncBundleError, "unsafe_symlink"):
            generate_report_sync_bundle(self.repository)
        link.unlink()

        forbidden = self._write("analysis_report/research/notes.md", b"ignored?\n")
        with self.assertRaisesRegex(ReportSyncBundleError, "source_filename_forbidden"):
            generate_report_sync_bundle(self.repository)
        forbidden.unlink()

        # A controlled research title may discuss secret handling without being a
        # credential file or directory. Its content still passes the credential scanner.
        secret_topic = self._write(
            "analysis_report/research/24_secret_claim_contract.md",
            b"# Secret claim contract\nNo credential values.\n",
        )
        generate_report_sync_bundle(self.repository)
        secret_topic.unlink()

        manifest_path = self.repository / "analysis_report/notion_sync_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["pages"][0]["localFiles"][0]["path"] = "analysis_report/../.env"
        self._write_json("analysis_report/notion_sync_manifest.json", manifest)
        with self.assertRaisesRegex(ReportSyncBundleError, "path_invalid"):
            generate_report_sync_bundle(self.repository)

        self._write_previous_manifests()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["pages"][0]["localFiles"][0]["path"] = "analysis_report/secrets/evidence.md"
        self._write_json("analysis_report/notion_sync_manifest.json", manifest)
        with self.assertRaisesRegex(ReportSyncBundleError, "sensitive_path_forbidden"):
            generate_report_sync_bundle(self.repository)

        self._write_previous_manifests()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        duplicate = dict(manifest["pages"][0])
        duplicate["localFiles"] = [
            {
                "path": "analysis_report/research/08_new_evidence.md",
                "sha256": self._raw_hash("analysis_report/research/08_new_evidence.md"),
            }
        ]
        manifest["pages"].append(duplicate)
        self._write_json("analysis_report/notion_sync_manifest.json", manifest)
        with self.assertRaisesRegex(ReportSyncBundleError, "duplicate_page_key"):
            generate_report_sync_bundle(self.repository)

    def test_pinned_read_session_rejects_directory_rebind_and_final_input_drift(
        self,
    ) -> None:
        research = self.repository / "analysis_report/research"
        displaced = self.repository / "analysis_report/research.pinned"
        outside = Path(self.tempdir.name) / "outside-research"
        outside.mkdir()
        real_open_directory = report_sync_bundle._PinnedReadSession._open_directory
        rebound = False

        def open_then_rebind(
            session: report_sync_bundle._PinnedReadSession,
            relative: str,
            *,
            missing_code: str,
        ) -> report_sync_bundle.DirectoryBinding:
            nonlocal rebound
            binding = real_open_directory(session, relative, missing_code=missing_code)
            if relative == "analysis_report/research" and not rebound:
                research.rename(displaced)
                research.symlink_to(outside, target_is_directory=True)
                rebound = True
            return binding

        try:
            with (
                patch.object(
                    report_sync_bundle._PinnedReadSession,
                    "_open_directory",
                    autospec=True,
                    side_effect=open_then_rebind,
                ),
                self.assertRaisesRegex(ReportSyncBundleError, "source_changed_during_read"),
            ):
                generate_report_sync_bundle(self.repository)
        finally:
            if research.is_symlink():
                research.unlink()
            if displaced.exists():
                displaced.rename(research)

        real_generate = report_sync_bundle._generate_report_sync_bundle
        drift_paths = (
            "analysis_report/README.md",
            "analysis_report/notion_sync_manifest.json",
            "analysis_report/yuque_sync/mapping.json",
            "analysis_report/screenshots/00_fixture.png",
        )
        for relative in drift_paths:
            with self.subTest(relative=relative):
                path = self.repository / relative
                original = path.read_bytes()
                changed = bytes((original[0] ^ 1,)) + original[1:]

                def generate_then_mutate(
                    session: report_sync_bundle._PinnedReadSession,
                    target: Path = path,
                    replacement: bytes = changed,
                ) -> dict[str, object]:
                    payload = real_generate(session)
                    target.write_bytes(replacement)
                    return payload

                try:
                    with (
                        patch(
                            "scripts.report_sync_bundle._generate_report_sync_bundle",
                            side_effect=generate_then_mutate,
                        ),
                        self.assertRaisesRegex(
                            ReportSyncBundleError,
                            "source_changed_during_read",
                        ),
                    ):
                        generate_report_sync_bundle(self.repository)
                finally:
                    path.write_bytes(original)

        added = self.repository / "analysis_report/research/09_added.md"

        def generate_then_add_entry(
            session: report_sync_bundle._PinnedReadSession,
        ) -> dict[str, object]:
            payload = real_generate(session)
            added.write_bytes(b"# Added concurrently\n")
            return payload

        try:
            with (
                patch(
                    "scripts.report_sync_bundle._generate_report_sync_bundle",
                    side_effect=generate_then_add_entry,
                ),
                self.assertRaisesRegex(ReportSyncBundleError, "source_changed_during_read"),
            ):
                generate_report_sync_bundle(self.repository)
        finally:
            added.unlink(missing_ok=True)

    def test_verifier_keeps_and_rehashes_the_pinned_bundle_until_return(self) -> None:
        payload = generate_report_sync_bundle(self.repository)
        bundle = self._save_bundle(payload)
        original = bundle.read_bytes()
        replacement = bytes((original[0] ^ 1,)) + original[1:]
        real_generate = report_sync_bundle._generate_report_sync_bundle

        def generate_then_mutate_bundle(
            session: report_sync_bundle._PinnedReadSession,
        ) -> dict[str, object]:
            current = real_generate(session)
            bundle.write_bytes(replacement)
            return current

        with (
            patch(
                "scripts.report_sync_bundle._generate_report_sync_bundle",
                side_effect=generate_then_mutate_bundle,
            ),
            self.assertRaisesRegex(ReportSyncBundleError, "source_changed_during_read"),
        ):
            verify_report_sync_bundle(self.repository, bundle)

    def test_input_type_races_are_nonblocking_and_close_errors_are_fixed(self) -> None:
        if hasattr(os, "O_NONBLOCK"):
            self.assertTrue(report_sync_bundle._input_file_flags() & os.O_NONBLOCK)
        if hasattr(os, "O_NOFOLLOW"):
            self.assertTrue(report_sync_bundle._input_file_flags() & os.O_NOFOLLOW)

        fifo = self.repository / "analysis_report/research/09_fifo.md"
        os.mkfifo(fifo)
        try:
            with self.assertRaisesRegex(
                ReportSyncBundleError,
                "controlled_directory_entry_forbidden",
            ):
                generate_report_sync_bundle(self.repository)
        finally:
            fifo.unlink()

        socket_path = self.repository / "analysis_report/research/09_socket.md"
        local_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous_directory = os.getcwd()
        try:
            os.chdir(socket_path.parent)
            local_socket.bind(socket_path.name)
        finally:
            os.chdir(previous_directory)
        try:
            with self.assertRaisesRegex(
                ReportSyncBundleError,
                "controlled_directory_entry_forbidden",
            ):
                generate_report_sync_bundle(self.repository)
        finally:
            local_socket.close()
            socket_path.unlink(missing_ok=True)

        victim = self.repository / "analysis_report/research/08_new_evidence.md"
        original = victim.read_bytes()
        real_open = os.open
        replaced = False

        def replace_regular_with_fifo(
            path: os.PathLike[str] | str,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal replaced
            if path == victim.name and dir_fd is not None and not replaced:
                victim.unlink()
                os.mkfifo(victim)
                replaced = True
            return real_open(path, flags, mode, dir_fd=dir_fd)

        try:
            with (
                patch("scripts.report_sync_bundle.os.open", side_effect=replace_regular_with_fifo),
                self.assertRaisesRegex(ReportSyncBundleError, "unsafe_symlink"),
            ):
                generate_report_sync_bundle(self.repository)
        finally:
            victim.unlink(missing_ok=True)
            victim.write_bytes(original)

        def substitute_device(
            path: os.PathLike[str] | str,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if path == victim.name and dir_fd is not None:
                return real_open("/dev/null", report_sync_bundle._input_file_flags())
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            patch("scripts.report_sync_bundle.os.open", side_effect=substitute_device),
            self.assertRaisesRegex(ReportSyncBundleError, "unsafe_symlink"),
        ):
            generate_report_sync_bundle(self.repository)

        real_close = os.close

        def close_then_fail(descriptor: int) -> None:
            real_close(descriptor)
            raise PermissionError("injected private close path")

        with (
            patch.object(
                report_sync_bundle._PinnedReadSession,
                "_read_open_regular",
                side_effect=ReportSyncBundleError("source_unreadable"),
            ),
            patch("scripts.report_sync_bundle.os.close", side_effect=close_then_fail),
            self.assertRaisesRegex(ReportSyncBundleError, "^source_unreadable$"),
        ):
            generate_report_sync_bundle(self.repository)

        with (
            patch("scripts.report_sync_bundle.os.close", side_effect=close_then_fail),
            self.assertRaisesRegex(ReportSyncBundleError, "^source_close_failed$"),
        ):
            generate_report_sync_bundle(self.repository)

        captured_root_descriptors: list[int] = []
        real_fstat = os.fstat

        def capture_root_open(
            path: os.PathLike[str] | str,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            captured_root_descriptors.append(descriptor)
            return descriptor

        with (
            patch("scripts.report_sync_bundle.os.open", side_effect=capture_root_open),
            patch("scripts.report_sync_bundle.os.fstat", side_effect=KeyboardInterrupt()),
            self.assertRaises(KeyboardInterrupt),
        ):
            generate_report_sync_bundle(self.repository)
        self.assertEqual(len(captured_root_descriptors), 1)
        with self.assertRaises(OSError):
            real_fstat(captured_root_descriptors[0])

    def test_pinned_read_enter_closes_root_fd_if_tracking_is_interrupted(self) -> None:
        captured_root_descriptors: list[int] = []

        class InterruptingDescriptors(list[int]):
            def append(self, descriptor: int) -> None:
                super().append(descriptor)
                captured_root_descriptors.append(descriptor)
                raise KeyboardInterrupt()

        session = report_sync_bundle._PinnedReadSession(self.repository)
        session._descriptors = InterruptingDescriptors()

        def close_if_still_open() -> None:
            for descriptor in captured_root_descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

        self.addCleanup(close_if_still_open)
        with self.assertRaises(KeyboardInterrupt):
            session.__enter__()

        self.assertEqual(len(captured_root_descriptors), 1)
        self.assertEqual(session._descriptors, [])
        self.assertNotIn("", session._directories)
        with self.assertRaises(OSError):
            os.fstat(captured_root_descriptors[0])

    def test_credentials_source_count_and_total_bytes_fail_closed(self) -> None:
        source = self.repository / "analysis_report/research/08_new_evidence.md"
        source.write_text("API_KEY=sk-1234567890abcdefghijklmnopqrstuv\n", encoding="utf-8")
        with self.assertRaisesRegex(ReportSyncBundleError, "credential_content_forbidden"):
            generate_report_sync_bundle(self.repository)

        source.write_text("API_KEY=sk-your-example-placeholder\n", encoding="utf-8")
        generate_report_sync_bundle(self.repository)

        source.write_text("API_KEY=${OPENAI_API_KEY}\n", encoding="utf-8")
        generate_report_sync_bundle(self.repository)

        for named_short_credential in (
            "API_KEY=x\n",
            "API Key: x\n",
            "PASSWORD=x\n",
            "Password: x\n",
            "SERVICE_TOKEN=x\n",
            "SERVICE_SECRET=x\n",
            "token=x\n",
            "Token: x\n",
            '"password": "x"\n',
            "- API Key: x\n",
            "1. Refresh Token: x\n",
            "| API Key | x |\n",
            '{"nested":{"refresh_token":"x"}}\n',
            "Authorization: Basic eA==\n",
            "Authorization: Bearer x\n",
            "Cookie: session=x\n",
            "Set-Cookie: session=x\n",
            "- Authorization: Basic eA==\n",
            "1. Cookie: session=x\n",
            "| Authorization | Basic eA== |\n",
            "| Cookie | session=x |\n",
            "PASSWORD=x\r",
        ):
            source.write_text(named_short_credential, encoding="utf-8")
            with self.assertRaisesRegex(ReportSyncBundleError, "credential_content_forbidden"):
                generate_report_sync_bundle(self.repository)

        source.write_text(
            "API Key: ${OPENAI_API_KEY}\n"
            "Token: <redacted>\n"
            "- Refresh Token: ${REFRESH_TOKEN}\n"
            "| Client Secret | <redacted> |\n"
            "Authorization: <redacted>\n"
            "Cookie: <redacted>\n",
            encoding="utf-8",
        )
        generate_report_sync_bundle(self.repository)

        source.write_text(
            "Token: this prose describes token budgeting.\n"
            "API Key: this prose describes where configuration belongs.\n"
            "The password: field is discussed without assigning a value.\n",
            encoding="utf-8",
        )
        generate_report_sync_bundle(self.repository)

        for prefix in ("test", "dummy", "sample"):
            source.write_text(
                "API_KEY=sk-" + prefix + "-" + "a" * 40 + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ReportSyncBundleError, "credential_content_forbidden"):
                generate_report_sync_bundle(self.repository)

        source.write_text("NOTION_API_KEY=" + "ntn_" + "a" * 40 + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ReportSyncBundleError, "credential_content_forbidden"):
            generate_report_sync_bundle(self.repository)

        source.write_text("GITHUB_TOKEN=" + "github_pat_" + "a" * 50 + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ReportSyncBundleError, "credential_content_forbidden"):
            generate_report_sync_bundle(self.repository)

        source.write_text("# No credentials\n", encoding="utf-8")

        screenshot_path = self.repository / "analysis_report/screenshots/manifest.json"
        screenshot = json.loads(screenshot_path.read_text(encoding="utf-8"))
        for credential_field in (
            "api_key",
            "refresh_token",
            "client_secret",
            "authorization",
            "cookie",
            "set-cookie",
        ):
            with self.subTest(json_credential_field=credential_field):
                payload = dict(screenshot)
                payload["unknownMetadata"] = [{"nested": {credential_field: "x"}}]
                self._write(
                    "analysis_report/screenshots/manifest.json",
                    json.dumps(payload, separators=(",", ":")).encode() + b"\n",
                )
                with self.assertRaisesRegex(
                    ReportSyncBundleError,
                    "credential_content_forbidden",
                ):
                    generate_report_sync_bundle(self.repository)
        screenshot["unknownMetadata"] = [
            {
                "api_key": "<redacted>",
                "refresh_token": "${REFRESH_TOKEN}",
            }
        ]
        self._write(
            "analysis_report/screenshots/manifest.json",
            json.dumps(screenshot, separators=(",", ":")).encode() + b"\n",
        )
        generate_report_sync_bundle(self.repository)
        self._write_screenshot_manifest(
            [self._image_item("00_fixture.png", self.image, width=1, height=1)]
        )

        with patch("scripts.report_sync_bundle._MAX_SOURCE_COUNT", 6):
            with self.assertRaisesRegex(ReportSyncBundleError, "source_inventory_too_large"):
                generate_report_sync_bundle(self.repository)
        with patch("scripts.report_sync_bundle._MAX_TOTAL_SOURCE_BYTES", 1):
            with self.assertRaisesRegex(ReportSyncBundleError, "source_inventory_too_large"):
                generate_report_sync_bundle(self.repository)

    def test_image_hash_magic_dimensions_mime_and_policy_are_enforced(self) -> None:
        image_path = self.repository / "analysis_report/screenshots/00_fixture.png"
        image_path.write_bytes(b"not-a-real-png")
        self._write_screenshot_manifest(
            [self._image_item("00_fixture.png", image_path.read_bytes(), width=1, height=1)]
        )
        with self.assertRaisesRegex(ReportSyncBundleError, "image_content_invalid"):
            generate_report_sync_bundle(self.repository)

        header_only_png = (
            b"\x89PNG\r\n\x1a\n"
            + (13).to_bytes(4, "big")
            + b"IHDR"
            + (1).to_bytes(4, "big")
            + (1).to_bytes(4, "big")
        )
        image_path.write_bytes(header_only_png)
        self._write_screenshot_manifest(
            [self._image_item("00_fixture.png", header_only_png, width=1, height=1)]
        )
        with self.assertRaisesRegex(ReportSyncBundleError, "image_content_invalid"):
            generate_report_sync_bundle(self.repository)

        truncated_png = self.image[:-12]
        image_path.write_bytes(truncated_png)
        self._write_screenshot_manifest(
            [self._image_item("00_fixture.png", truncated_png, width=1, height=1)]
        )
        with self.assertRaisesRegex(ReportSyncBundleError, "image_content_invalid"):
            generate_report_sync_bundle(self.repository)

        oversized_scanline_png = (
            b"\x89PNG\r\n\x1a\n"
            + png_chunk(
                b"IHDR",
                (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes((8, 2, 0, 0, 0)),
            )
            + png_chunk(b"IDAT", zlib.compress(b"\x00" + b"\x00" * 1_000_000))
            + png_chunk(b"IEND", b"")
        )
        image_path.write_bytes(oversized_scanline_png)
        self._write_screenshot_manifest(
            [
                self._image_item(
                    "00_fixture.png",
                    oversized_scanline_png,
                    width=1,
                    height=1,
                )
            ]
        )
        with self.assertRaisesRegex(ReportSyncBundleError, "image_content_invalid"):
            generate_report_sync_bundle(self.repository)

        image_path.write_bytes(self.image)
        item = self._image_item("00_fixture.png", self.image, width=2, height=1)
        self._write_screenshot_manifest([item])
        with self.assertRaisesRegex(ReportSyncBundleError, "image_dimension_drift"):
            generate_report_sync_bundle(self.repository)

        item["width"] = 1
        item["mediaType"] = "image/jpeg"
        self._write_screenshot_manifest([item])
        with self.assertRaisesRegex(ReportSyncBundleError, "image_mime_drift"):
            generate_report_sync_bundle(self.repository)

        item["mediaType"] = "image/png"
        item["notForPublicDistribution"] = False
        self._write_screenshot_manifest([item])
        with self.assertRaisesRegex(ReportSyncBundleError, "screenshot_policy_invalid"):
            generate_report_sync_bundle(self.repository)

        item["notForPublicDistribution"] = True
        item["redactionStatus"] = "not-applicable-public-webpage-in-internal-evidence-set"
        self._write_screenshot_manifest([item])
        generate_report_sync_bundle(self.repository)

        item["redactionStatus"] = "unrestricted-public-copy"
        self._write_screenshot_manifest([item])
        with self.assertRaisesRegex(ReportSyncBundleError, "screenshot_policy_invalid"):
            generate_report_sync_bundle(self.repository)

        self._write_screenshot_manifest(
            [self._image_item("00_fixture.png", self.image, width=1, height=1)]
        )
        extra = self._write("analysis_report/screenshots/01_extra.png", self.image)
        with self.assertRaisesRegex(ReportSyncBundleError, "unmanifested_image_forbidden"):
            generate_report_sync_bundle(self.repository)
        extra.unlink()

        image_path.unlink()
        with self.assertRaisesRegex(ReportSyncBundleError, "image_missing"):
            generate_report_sync_bundle(self.repository)

    def test_complete_jpeg_is_supported_but_header_only_and_truncation_are_rejected(
        self,
    ) -> None:
        jpeg = base64.b64decode(
            "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsL"
            "DBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/"
            "wAALCAADAAIBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACP/EABQQAQAAAAAAAA"
            "AAAAAAAAAAAAD/2gAIAQEAAD8AP7//2Q=="
        )
        self._write("analysis_report/screenshots/01_fixture.jpeg", jpeg)
        self._write_screenshot_manifest(
            [
                self._image_item("00_fixture.png", self.image, width=1, height=1),
                self._image_item(
                    "01_fixture.jpeg",
                    jpeg,
                    width=2,
                    height=3,
                    media_type="image/jpeg",
                ),
            ]
        )
        payload = generate_report_sync_bundle(self.repository)
        images = {image["path"]: image for image in cast(list[dict[str, Any]], payload["images"])}
        self.assertEqual(images["analysis_report/screenshots/01_fixture.jpeg"]["width"], 2)

        undefined_huffman_selector = bytearray(jpeg)
        start_of_scan = undefined_huffman_selector.index(b"\xff\xda")
        undefined_huffman_selector[start_of_scan + 6] = 0x33
        malformed_selector = bytes(undefined_huffman_selector)
        self._write("analysis_report/screenshots/01_fixture.jpeg", malformed_selector)
        self._write_screenshot_manifest(
            [
                self._image_item("00_fixture.png", self.image, width=1, height=1),
                self._image_item(
                    "01_fixture.jpeg",
                    malformed_selector,
                    width=2,
                    height=3,
                    media_type="image/jpeg",
                ),
            ]
        )
        with self.assertRaisesRegex(ReportSyncBundleError, "image_content_invalid"):
            generate_report_sync_bundle(self.repository)

        undefined_quantization_selector = bytearray(jpeg)
        start_of_frame = undefined_quantization_selector.index(b"\xff\xc0")
        undefined_quantization_selector[start_of_frame + 12] = 3
        malformed_quantization_selector = bytes(undefined_quantization_selector)
        self._write(
            "analysis_report/screenshots/01_fixture.jpeg",
            malformed_quantization_selector,
        )
        self._write_screenshot_manifest(
            [
                self._image_item("00_fixture.png", self.image, width=1, height=1),
                self._image_item(
                    "01_fixture.jpeg",
                    malformed_quantization_selector,
                    width=2,
                    height=3,
                    media_type="image/jpeg",
                ),
            ]
        )
        with self.assertRaisesRegex(ReportSyncBundleError, "image_content_invalid"):
            generate_report_sync_bundle(self.repository)

        header_only = (
            b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x03\x00\x02\x03"
            b"\x01\x11\x00\x02\x11\x00\x03\x11\x00\xff\xd9"
        )
        self._write("analysis_report/screenshots/01_fixture.jpeg", header_only)
        self._write_screenshot_manifest(
            [
                self._image_item("00_fixture.png", self.image, width=1, height=1),
                self._image_item(
                    "01_fixture.jpeg",
                    header_only,
                    width=2,
                    height=3,
                    media_type="image/jpeg",
                ),
            ]
        )
        with self.assertRaisesRegex(ReportSyncBundleError, "image_content_invalid"):
            generate_report_sync_bundle(self.repository)

        truncated = jpeg[:-2]
        self._write("analysis_report/screenshots/01_fixture.jpeg", truncated)
        self._write_screenshot_manifest(
            [
                self._image_item("00_fixture.png", self.image, width=1, height=1),
                self._image_item(
                    "01_fixture.jpeg",
                    truncated,
                    width=2,
                    height=3,
                    media_type="image/jpeg",
                ),
            ]
        )
        with self.assertRaisesRegex(ReportSyncBundleError, "image_content_invalid"):
            generate_report_sync_bundle(self.repository)

    def test_png_decode_validation_does_not_retain_the_decompressed_image(self) -> None:
        width = 2_048
        height = 2_048
        row = b"\x00" + b"\x00" * (width * 3)
        compressor = zlib.compressobj(level=9)
        compressed_parts = [compressor.compress(row) for _ in range(height)]
        compressed = b"".join(part for part in compressed_parts if part) + compressor.flush()
        png = (
            b"\x89PNG\r\n\x1a\n"
            + png_chunk(
                b"IHDR",
                width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes((8, 2, 0, 0, 0)),
            )
            + png_chunk(b"IDAT", compressed)
            + png_chunk(b"IEND", b"")
        )

        tracemalloc.start()
        try:
            dimensions = report_sync_bundle._png_dimensions(png)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(dimensions, (width, height))
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_png_palette_rules_cover_grayscale_truecolor_alpha_and_indexed_modes(
        self,
    ) -> None:
        def make_png(
            color_type: int,
            bit_depth: int,
            scanline: bytes,
            *,
            palette_entries: int | None,
        ) -> bytes:
            header = (
                (1).to_bytes(4, "big")
                + (1).to_bytes(4, "big")
                + bytes((bit_depth, color_type, 0, 0, 0))
            )
            palette = (
                b""
                if palette_entries is None
                else png_chunk(b"PLTE", b"\x00\x00\x00" * palette_entries)
            )
            return (
                b"\x89PNG\r\n\x1a\n"
                + png_chunk(b"IHDR", header)
                + palette
                + png_chunk(b"IDAT", zlib.compress(scanline))
                + png_chunk(b"IEND", b"")
            )

        for color_type, scanline in ((0, b"\x00\x00"), (4, b"\x00\x00\x00")):
            with self.subTest(grayscale_with_palette=color_type):
                malformed = make_png(
                    color_type,
                    8,
                    scanline,
                    palette_entries=1,
                )
                with self.assertRaisesRegex(ReportSyncBundleError, "image_content_invalid"):
                    report_sync_bundle._png_dimensions(malformed)

        for color_type, scanline in (
            (2, b"\x00\x00\x00\x00"),
            (6, b"\x00\x00\x00\x00\x00"),
        ):
            with self.subTest(truecolor_optional_palette=color_type):
                for palette_entries in (None, 1):
                    self.assertEqual(
                        report_sync_bundle._png_dimensions(
                            make_png(
                                color_type,
                                8,
                                scanline,
                                palette_entries=palette_entries,
                            )
                        ),
                        (1, 1),
                    )

        with self.assertRaisesRegex(ReportSyncBundleError, "image_content_invalid"):
            report_sync_bundle._png_dimensions(make_png(3, 1, b"\x00\x00", palette_entries=None))
        with self.assertRaisesRegex(ReportSyncBundleError, "image_content_invalid"):
            report_sync_bundle._png_dimensions(make_png(3, 1, b"\x00\x00", palette_entries=3))
        self.assertEqual(
            report_sync_bundle._png_dimensions(make_png(3, 1, b"\x00\x00", palette_entries=2)),
            (1, 1),
        )
        self.assertEqual(
            report_sync_bundle._png_dimensions(
                make_png(2, 8, b"\x00\x00\x00\x00", palette_entries=256)
            ),
            (1, 1),
        )
        with self.assertRaisesRegex(ReportSyncBundleError, "image_content_invalid"):
            report_sync_bundle._png_dimensions(
                make_png(2, 8, b"\x00\x00\x00\x00", palette_entries=257)
            )

    def test_manifest_types_and_yuque_verification_are_strict(self) -> None:
        notion_path = self.repository / "analysis_report/notion_sync_manifest.json"
        notion = json.loads(notion_path.read_text(encoding="utf-8"))
        notion["pages"][0]["readback"]["verified"] = 1
        self._write_json("analysis_report/notion_sync_manifest.json", notion)
        with self.assertRaisesRegex(ReportSyncBundleError, "notion_manifest_invalid"):
            generate_report_sync_bundle(self.repository)

        self._write_previous_manifests()
        yuque_path = self.repository / "analysis_report/yuque_sync/mapping.json"
        yuque = json.loads(yuque_path.read_text(encoding="utf-8"))
        yuque["objects"][0]["verification"] = "verified_invented_state"
        self._write_json("analysis_report/yuque_sync/mapping.json", yuque)
        with self.assertRaisesRegex(ReportSyncBundleError, "yuque_verification_state_invalid"):
            generate_report_sync_bundle(self.repository)

        for mutation in ("missing", "invalid", "duplicate"):
            with self.subTest(yuque_page_key=mutation):
                self._write_previous_manifests()
                yuque = json.loads(yuque_path.read_text(encoding="utf-8"))
                if mutation == "missing":
                    del yuque["objects"][0]["yuque_slug"]
                elif mutation == "invalid":
                    yuque["objects"][0]["yuque_slug"] = "invalid/page"
                else:
                    yuque["objects"][1]["yuque_slug"] = yuque["objects"][0]["yuque_slug"]
                self._write_json("analysis_report/yuque_sync/mapping.json", yuque)
                with self.assertRaisesRegex(ReportSyncBundleError, "yuque_page_key_invalid"):
                    generate_report_sync_bundle(self.repository)

        self._write_previous_manifests()
        screenshot_path = self.repository / "analysis_report/screenshots/manifest.json"
        screenshot = json.loads(screenshot_path.read_text(encoding="utf-8"))
        screenshot["items"][0]["byteSize"] = True
        self._write_json("analysis_report/screenshots/manifest.json", screenshot)
        with self.assertRaisesRegex(ReportSyncBundleError, "image_metadata_invalid"):
            generate_report_sync_bundle(self.repository)

        self._write_screenshot_manifest(
            [self._image_item("00_fixture.png", self.image, width=1, height=1)]
        )
        notion_path.write_text('{"format":"x","version":1.0,"pages":[]}\n')
        with self.assertRaisesRegex(ReportSyncBundleError, "notion_manifest_invalid"):
            generate_report_sync_bundle(self.repository)

    def test_bundle_schema_rejects_bool_numeric_and_noncanonical_or_duplicate_json(
        self,
    ) -> None:
        payload = generate_report_sync_bundle(self.repository)
        payload["schemaVersion"] = True
        path = self._save_bundle(payload, "bool-schema.json")
        with self.assertRaisesRegex(ReportSyncBundleError, "bundle_schema_invalid"):
            verify_report_sync_bundle(self.repository, path)

        payload = generate_report_sync_bundle(self.repository)
        cast(list[dict[str, Any]], payload["sourceTargets"])[0]["byteSize"] = True
        path = self._save_bundle(payload, "bool-size.json")
        with self.assertRaisesRegex(ReportSyncBundleError, "bundle_schema_invalid"):
            verify_report_sync_bundle(self.repository, path)

        payload = generate_report_sync_bundle(self.repository)
        cast(list[dict[str, Any]], payload["sourceTargets"])[0]["targetStatus"] = []
        path = self._save_bundle(payload, "status-type.json")
        with self.assertRaisesRegex(ReportSyncBundleError, "bundle_schema_invalid"):
            verify_report_sync_bundle(self.repository, path)

        payload = generate_report_sync_bundle(self.repository)
        pretty = self.repository / "analysis_report/report_sync_bundles/pretty.json"
        pretty.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.assertRaisesRegex(ReportSyncBundleError, "bundle_non_canonical"):
            verify_report_sync_bundle(self.repository, pretty)

        duplicate = self.repository / "analysis_report/report_sync_bundles/duplicate.json"
        duplicate.write_text(
            '{"format":"quantum-entanglement.report-sync-bundle",'
            '"format":"quantum-entanglement.report-sync-bundle","schemaVersion":3}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ReportSyncBundleError, "json_duplicate_key"):
            verify_report_sync_bundle(self.repository, duplicate)

    def test_schema_v3_preserves_remote_page_identity_and_notion_page_aggregation(
        self,
    ) -> None:
        baseline = generate_report_sync_bundle(self.repository)
        baseline_targets = self._source_targets(baseline)
        readme_key = baseline_targets[("analysis_report/screenshots/README.md", "notion")][
            "entryKey"
        ]
        manifest_key = baseline_targets[("analysis_report/screenshots/manifest.json", "notion")][
            "entryKey"
        ]

        screenshot_readme_path = self.repository / "analysis_report/screenshots/README.md"
        original_readme = screenshot_readme_path.read_bytes()
        screenshot_readme_path.write_bytes(b"# Screenshots changed\n")
        changed = self._source_targets(generate_report_sync_bundle(self.repository))
        for path in (
            "analysis_report/screenshots/README.md",
            "analysis_report/screenshots/manifest.json",
        ):
            target = changed[(path, "notion")]
            self.assertEqual(target["targetPageKey"], "screenshot-evidence")
            self.assertIsNone(target["proposedTargetPageKey"])
            self.assertEqual(target["targetStatus"], "local_pending")
        self.assertEqual(
            changed[("analysis_report/screenshots/README.md", "notion")]["entryKey"],
            readme_key,
        )
        self.assertEqual(
            changed[("analysis_report/screenshots/manifest.json", "notion")]["entryKey"],
            manifest_key,
        )
        screenshot_readme_path.write_bytes(original_readme)

        notion_path = self.repository / "analysis_report/notion_sync_manifest.json"
        notion = json.loads(notion_path.read_text(encoding="utf-8"))
        screenshot_page = next(
            page for page in notion["pages"] if page["key"] == "screenshot-evidence"
        )
        screenshot_page["readback"]["verified"] = False
        self._write_json("analysis_report/notion_sync_manifest.json", notion)
        unverified = self._source_targets(generate_report_sync_bundle(self.repository))
        self.assertEqual(
            {
                unverified[(path, "notion")]["targetStatus"]
                for path in (
                    "analysis_report/screenshots/README.md",
                    "analysis_report/screenshots/manifest.json",
                )
            },
            {"local_pending"},
        )

        self._write_previous_manifests()
        notion = json.loads(notion_path.read_text(encoding="utf-8"))
        screenshot_page = next(
            page for page in notion["pages"] if page["key"] == "screenshot-evidence"
        )
        missing_path = "analysis_report/research/09_missing.md"
        screenshot_page["localFiles"].append({"path": missing_path, "sha256": "0" * 64})
        self._write_json("analysis_report/notion_sync_manifest.json", notion)
        missing = generate_report_sync_bundle(self.repository)
        missing_targets = self._source_targets(missing)
        self.assertEqual(
            missing_targets[("analysis_report/screenshots/README.md", "notion")]["targetStatus"],
            "local_pending",
        )
        self.assertIn(
            missing_path,
            cast(dict[str, Any], missing["previousManifestDiagnostics"])["notion"]["missing"],
        )

    def test_schema_v3_validator_rejects_identity_conflation(self) -> None:
        payload = generate_report_sync_bundle(self.repository)
        payload["schemaVersion"] = 2
        with self.assertRaisesRegex(ReportSyncBundleError, "bundle_schema_invalid"):
            report_sync_bundle._validate_bundle_schema(payload)

        payload = generate_report_sync_bundle(self.repository)
        payload["pages"] = payload.pop("sourceTargets")
        with self.assertRaisesRegex(ReportSyncBundleError, "bundle_schema_invalid"):
            report_sync_bundle._validate_bundle_schema(payload)

        payload = generate_report_sync_bundle(self.repository)
        targets = cast(list[dict[str, Any]], payload["sourceTargets"])
        targets[1]["entryKey"] = targets[0]["entryKey"]
        with self.assertRaisesRegex(ReportSyncBundleError, "bundle_schema_invalid"):
            report_sync_bundle._validate_bundle_schema(payload)

        payload = generate_report_sync_bundle(self.repository)
        pending = next(
            target
            for target in cast(list[dict[str, Any]], payload["sourceTargets"])
            if target["targetPageKey"] is None
        )
        pending["proposedTargetPageKey"] = None
        with self.assertRaisesRegex(ReportSyncBundleError, "bundle_schema_invalid"):
            report_sync_bundle._validate_bundle_schema(payload)

        payload = generate_report_sync_bundle(self.repository)
        payload["imageDiagnostics"] = {"unmanifested": []}
        with self.assertRaisesRegex(ReportSyncBundleError, "bundle_schema_invalid"):
            report_sync_bundle._validate_bundle_schema(payload)

        payload = generate_report_sync_bundle(self.repository)
        cast(dict[str, Any], payload["sourceSummary"])["count"] = 99
        with self.assertRaisesRegex(ReportSyncBundleError, "bundle_schema_invalid"):
            report_sync_bundle._validate_bundle_schema(payload)

        payload = generate_report_sync_bundle(self.repository)
        targets = cast(list[dict[str, Any]], payload["sourceTargets"])
        shared_path = "analysis_report/research/00_scope.md"
        shared_targets = [target for target in targets if target["path"] == shared_path]
        self.assertEqual(len(shared_targets), 2)
        shared_targets[1]["rawSha256"] = "f" * 64
        with self.assertRaisesRegex(ReportSyncBundleError, "bundle_schema_invalid"):
            report_sync_bundle._validate_bundle_schema(payload)

        payload = generate_report_sync_bundle(self.repository)
        cast(list[dict[str, Any]], payload["sourceTargets"]).pop()
        with self.assertRaisesRegex(ReportSyncBundleError, "bundle_schema_invalid"):
            report_sync_bundle._validate_bundle_schema(payload)

        payload = generate_report_sync_bundle(self.repository)
        known = next(
            target
            for target in cast(list[dict[str, Any]], payload["sourceTargets"])
            if target["targetPageKey"] is not None
        )
        known["proposedTargetPageKey"] = "forbidden-proposal"
        with self.assertRaisesRegex(ReportSyncBundleError, "bundle_schema_invalid"):
            report_sync_bundle._validate_bundle_schema(payload)

    def test_json_nesting_limit_and_parser_recursion_have_a_fixed_error_code(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir()
        for depth in (65, 2_000):
            nested = directory / f"nested-{depth}.json"
            nested.write_text("[" * depth + "0" + "]" * depth + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ReportSyncBundleError, "json_nesting_too_deep"):
                verify_report_sync_bundle(self.repository, nested)

        value: object = 0
        for _ in range(65):
            value = [value]
        with self.assertRaisesRegex(ReportSyncBundleError, "json_nesting_too_deep"):
            canonical_json(value)

    def test_cli_output_is_dedicated_no_clobber_and_explicitly_overwritable(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(["--repository-root", str(self.repository)])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), canonical_json(json.loads(stdout.getvalue())))
        self.assertFalse(
            (self.repository / "analysis_report/report_sync_bundles/bundle.json").exists()
        )

        output = "analysis_report/report_sync_bundles/bundle.json"
        with redirect_stdout(StringIO()):
            code = main(["--repository-root", str(self.repository), "--output", output])
        self.assertEqual(code, 0)
        bundle = self.repository / output
        original = bundle.read_bytes()
        original_stat = bundle.stat()
        original_identity = (original_stat.st_dev, original_stat.st_ino)
        self.assertTrue(bundle.is_file())

        stderr = StringIO()
        with redirect_stderr(stderr):
            code = main(["--repository-root", str(self.repository), "--output", output])
        self.assertEqual(code, 2)
        self.assertEqual(stderr.getvalue(), "report-sync bundle error: output_exists\n")
        self.assertEqual(bundle.read_bytes(), original)

        with redirect_stdout(StringIO()):
            code = main(
                [
                    "--repository-root",
                    str(self.repository),
                    "--output",
                    output,
                    "--overwrite",
                ]
            )
        self.assertEqual(code, 0)
        current_stat = bundle.stat()
        self.assertNotEqual((current_stat.st_dev, current_stat.st_ino), original_identity)
        recoveries = [path for path in bundle.parent.iterdir() if path.name != "bundle.json"]
        self.assertEqual(len(recoveries), 1)
        recovery_stat = recoveries[0].stat()
        self.assertEqual((recovery_stat.st_dev, recovery_stat.st_ino), original_identity)
        self.assertEqual(recoveries[0].read_bytes(), original)

        verified = StringIO()
        with redirect_stdout(verified):
            code = main(["--repository-root", str(self.repository), "--verify", output])
        self.assertEqual(code, 0)
        self.assertEqual(verified.getvalue(), "report-sync bundle verified\n")

        for forbidden in (
            "bundle.json",
            ".git/bundle.json",
            "analysis_report/notion_sync_manifest.json",
            "analysis_report/research/00_scope.md",
        ):
            stderr = StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [
                        "--repository-root",
                        str(self.repository),
                        "--output",
                        forbidden,
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("bundle_location_forbidden", stderr.getvalue())

        stderr = StringIO()
        with redirect_stderr(stderr):
            code = main(
                [
                    "--repository-root",
                    str(self.repository),
                    "--output",
                    "analysis_report/report_sync_bundles/Bad Name.json",
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(stderr.getvalue(), "report-sync bundle error: bundle_filename_forbidden\n")

    def test_output_directory_symlink_and_path_escape_are_rejected(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        outside = Path(self.tempdir.name) / "outside"
        outside.mkdir()
        directory.symlink_to(outside, target_is_directory=True)
        stderr = StringIO()
        with redirect_stderr(stderr):
            code = main(
                [
                    "--repository-root",
                    str(self.repository),
                    "--output",
                    "analysis_report/report_sync_bundles/bundle.json",
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(stderr.getvalue(), "report-sync bundle error: unsafe_symlink\n")

        directory.unlink()
        stderr = StringIO()
        with redirect_stderr(stderr):
            code = main(
                [
                    "--repository-root",
                    str(self.repository),
                    "--output",
                    "../escaped.json",
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(stderr.getvalue(), "report-sync bundle error: path_escape\n")

    def test_output_directory_swap_cannot_overwrite_an_external_victim(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir()
        displaced = self.repository / "analysis_report/report_sync_bundles.displaced"
        outside = Path(self.tempdir.name) / "outside-swap"
        outside.mkdir()
        victim = outside / "bundle.json"
        victim.write_bytes(b"external-control-content\n")
        victim_before = victim.read_bytes()
        real_open = report_sync_bundle._open_bundle_directory

        def open_then_swap(root: Path) -> int:
            descriptor = real_open(root)
            directory.rename(displaced)
            directory.symlink_to(outside, target_is_directory=True)
            return descriptor

        stderr = StringIO()
        with patch(
            "scripts.report_sync_bundle._open_bundle_directory",
            side_effect=open_then_swap,
        ):
            with redirect_stderr(stderr):
                code = main(
                    [
                        "--repository-root",
                        str(self.repository),
                        "--output",
                        "analysis_report/report_sync_bundles/bundle.json",
                        "--overwrite",
                    ]
                )
        self.assertEqual(code, 2)
        self.assertEqual(stderr.getvalue(), "report-sync bundle error: unsafe_symlink\n")
        self.assertEqual(victim.read_bytes(), victim_before)
        self.assertFalse((displaced / "bundle.json").exists())

    def test_output_regular_entry_checks_are_nonblocking_for_fifo_inodes(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir()
        fifo = directory / ".fixture-fifo"
        os.mkfifo(fifo)
        directory_descriptor = os.open(directory, os.O_RDONLY)
        real_open = os.open
        requested_flags: list[int] = []

        def return_nonblocking_fifo(
            path: os.PathLike[str] | str,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if path == "bundle.json" and dir_fd == directory_descriptor:
                requested_flags.append(flags)
                return real_open(fifo, os.O_RDWR | os.O_NONBLOCK)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        try:
            with (
                patch("scripts.report_sync_bundle.os.open", side_effect=return_nonblocking_fifo),
                self.assertRaisesRegex(ReportSyncBundleError, "output_invalid"),
            ):
                report_sync_bundle._open_optional_output_entry(
                    directory_descriptor,
                    "bundle.json",
                )
            with (
                patch("scripts.report_sync_bundle.os.open", side_effect=return_nonblocking_fifo),
                self.assertRaisesRegex(ReportSyncBundleError, "output_commit_uncertain"),
            ):
                report_sync_bundle._optional_opened_entry_metadata(
                    directory_descriptor,
                    "bundle.json",
                    code="output_commit_uncertain",
                )
        finally:
            os.close(directory_descriptor)
        self.assertEqual(len(requested_flags), 2)
        self.assertTrue(all(flags & os.O_NONBLOCK for flags in requested_flags))

    def test_post_commit_directory_swap_reports_uncertain_and_preserves_create(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir()
        moved_outside = Path(self.tempdir.name) / "post-commit-create"
        real_bound = report_sync_bundle._bundle_directory_is_bound
        checks = 0

        def swap_on_post_check(root: Path, descriptor: int) -> bool:
            nonlocal checks
            checks += 1
            if checks == 3:
                directory.rename(moved_outside)
                directory.mkdir()
            return real_bound(root, descriptor)

        stderr = StringIO()
        with (
            patch(
                "scripts.report_sync_bundle._bundle_directory_is_bound",
                side_effect=swap_on_post_check,
            ),
            redirect_stderr(stderr),
        ):
            code = main(
                [
                    "--repository-root",
                    str(self.repository),
                    "--output",
                    "analysis_report/report_sync_bundles/bundle.json",
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(
            stderr.getvalue(),
            "report-sync bundle error: output_commit_uncertain\n",
        )
        committed = moved_outside / "bundle.json"
        self.assertTrue(committed.is_file())
        self.assertEqual([path.name for path in moved_outside.iterdir()], ["bundle.json"])
        self.assertEqual(list(directory.iterdir()), [])

    def test_post_commit_directory_swap_preserves_both_overwrite_inodes(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir()
        output = directory / "bundle.json"
        previous = b"previous-bundle-content\n"
        output.write_bytes(previous)
        previous_identity = file_identity(output)
        moved_outside = Path(self.tempdir.name) / "post-commit-overwrite"
        real_bound = report_sync_bundle._bundle_directory_is_bound
        checks = 0

        def swap_on_post_check(root: Path, descriptor: int) -> bool:
            nonlocal checks
            checks += 1
            if checks == 3:
                directory.rename(moved_outside)
                directory.mkdir()
            return real_bound(root, descriptor)

        stderr = StringIO()
        with (
            patch(
                "scripts.report_sync_bundle._bundle_directory_is_bound",
                side_effect=swap_on_post_check,
            ),
            redirect_stderr(stderr),
        ):
            code = main(
                [
                    "--repository-root",
                    str(self.repository),
                    "--output",
                    "analysis_report/report_sync_bundles/bundle.json",
                    "--overwrite",
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(
            stderr.getvalue(),
            "report-sync bundle error: output_commit_uncertain\n",
        )
        committed = moved_outside / "bundle.json"
        self.assertNotEqual(file_identity(committed), previous_identity)
        recoveries = [path for path in moved_outside.iterdir() if path.name != "bundle.json"]
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(file_identity(recoveries[0]), previous_identity)
        self.assertEqual(recoveries[0].read_bytes(), previous)
        self.assertEqual(list(directory.iterdir()), [])

    def test_output_transaction_never_calls_destructive_namespace_operations(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir()
        output = "analysis_report/report_sync_bundles/bundle.json"
        with (
            patch("scripts.report_sync_bundle.os.rename", side_effect=AssertionError) as rename,
            patch("scripts.report_sync_bundle.os.replace", side_effect=AssertionError) as replace,
            patch("scripts.report_sync_bundle.os.unlink", side_effect=AssertionError) as unlink,
        ):
            report_sync_bundle._write_output(self.repository, output, b"first\n")
            previous_identity = file_identity(self.repository / output)
            report_sync_bundle._write_output(
                self.repository,
                output,
                b"second\n",
                overwrite=True,
            )
        rename.assert_not_called()
        replace.assert_not_called()
        unlink.assert_not_called()
        bundle = self.repository / output
        self.assertEqual(bundle.read_bytes(), b"second\n")
        recoveries = [path for path in directory.iterdir() if path.name != "bundle.json"]
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(file_identity(recoveries[0]), previous_identity)

    def test_staging_short_write_loop_mode_and_zero_write_failure(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir()
        relative = "analysis_report/report_sync_bundles/partial.json"
        raw = b"complete-through-short-writes\n"
        real_write = os.write
        write_sizes: list[int] = []

        def short_write(descriptor: int, value: bytes) -> int:
            chunk = value[:3]
            write_sizes.append(len(chunk))
            return real_write(descriptor, chunk)

        with patch("scripts.report_sync_bundle.os.write", side_effect=short_write):
            report_sync_bundle._write_output(self.repository, relative, raw)

        output = self.repository / relative
        self.assertEqual(output.read_bytes(), raw)
        self.assertGreater(len(write_sizes), 1)
        self.assertEqual(output.stat().st_mode & 0o777, 0o400)

        captured_descriptor: int | None = None
        real_create = report_sync_bundle._create_temporary_output

        def tracking_create(directory_descriptor: int) -> tuple[int, str]:
            nonlocal captured_descriptor
            result = real_create(directory_descriptor)
            captured_descriptor = result[0]
            return result

        def zero_write(descriptor: int, value: bytes) -> int:
            if descriptor == captured_descriptor:
                return 0
            return real_write(descriptor, value)

        failed_relative = "analysis_report/report_sync_bundles/zero.json"
        with (
            patch(
                "scripts.report_sync_bundle._create_temporary_output",
                side_effect=tracking_create,
            ),
            patch("scripts.report_sync_bundle.os.write", side_effect=zero_write),
            self.assertRaisesRegex(ReportSyncBundleError, "output_write_failed"),
        ):
            report_sync_bundle._write_output(
                self.repository,
                failed_relative,
                b"must-not-commit\n",
            )
        self.assertFalse((self.repository / failed_relative).exists())

    def test_replaced_staging_name_is_never_unlinked_after_write_failure(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir()
        real_create = report_sync_bundle._create_temporary_output
        real_write = os.write
        real_replace = os.replace

        for overwrite in (False, True):
            with self.subTest(overwrite=overwrite):
                suffix = "overwrite" if overwrite else "create"
                output_relative = f"analysis_report/report_sync_bundles/{suffix}.json"
                output = self.repository / output_relative
                previous_identity: tuple[int, int] | None = None
                if overwrite:
                    output.write_bytes(b"previous\n")
                    previous_identity = file_identity(output)
                unrelated = directory / f".unrelated-{suffix}"
                unrelated.write_bytes(b"unrelated\n")
                unrelated_identity = file_identity(unrelated)
                captured: dict[str, Any] = {}

                def tracking_create(
                    descriptor: int,
                    state: dict[str, Any] = captured,
                ) -> tuple[int, str]:
                    result = real_create(descriptor)
                    state["directory"] = descriptor
                    state["descriptor"] = result[0]
                    state["name"] = result[1]
                    return result

                def failing_write(
                    descriptor: int,
                    value: bytes,
                    state: dict[str, Any] = captured,
                    unrelated_name: str = unrelated.name,
                ) -> int:
                    if descriptor == state["descriptor"]:
                        real_replace(
                            unrelated_name,
                            state["name"],
                            src_dir_fd=state["directory"],
                            dst_dir_fd=state["directory"],
                        )
                        raise OSError("injected private staging path")
                    return real_write(descriptor, value)

                stderr = StringIO()
                with (
                    patch(
                        "scripts.report_sync_bundle._create_temporary_output",
                        side_effect=tracking_create,
                    ),
                    patch("scripts.report_sync_bundle.os.write", side_effect=failing_write),
                    patch(
                        "scripts.report_sync_bundle.os.unlink",
                        side_effect=PermissionError("injected private cleanup path"),
                    ) as forbidden_unlink,
                    redirect_stderr(stderr),
                ):
                    argv = [
                        "--repository-root",
                        str(self.repository),
                        "--output",
                        output_relative,
                    ]
                    if overwrite:
                        argv.append("--overwrite")
                    code = main(argv)
                self.assertEqual(code, 2)
                self.assertEqual(
                    stderr.getvalue(),
                    "report-sync bundle error: output_write_failed\n",
                )
                self.assertNotIn("private", stderr.getvalue())
                forbidden_unlink.assert_not_called()
                recovered = directory / captured["name"]
                self.assertEqual(file_identity(recovered), unrelated_identity)
                if overwrite:
                    self.assertEqual(file_identity(output), previous_identity)
                    self.assertEqual(output.read_bytes(), b"previous\n")
                else:
                    self.assertFalse(output.exists())

    def test_staging_replacement_at_commit_preserves_unrelated_inode(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir()
        real_no_replace = report_sync_bundle._rename_no_replace
        real_exchange = report_sync_bundle._rename_exchange
        real_replace = os.replace

        create_unrelated = directory / ".create-unrelated"
        create_unrelated.write_bytes(b"create-unrelated\n")
        create_identity = file_identity(create_unrelated)

        def replace_create_staging(descriptor: int, source: str, destination: str) -> None:
            real_replace(
                create_unrelated.name,
                source,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            real_no_replace(descriptor, source, destination)

        create_stderr = StringIO()
        with (
            patch(
                "scripts.report_sync_bundle._rename_no_replace",
                side_effect=replace_create_staging,
            ),
            redirect_stderr(create_stderr),
        ):
            code = main(
                [
                    "--repository-root",
                    str(self.repository),
                    "--output",
                    "analysis_report/report_sync_bundles/create.json",
                ]
            )
        create_output = directory / "create.json"
        self.assertEqual(code, 2)
        self.assertEqual(
            create_stderr.getvalue(),
            "report-sync bundle error: output_concurrent_change\n",
        )
        self.assertEqual(file_identity(create_output), create_identity)

        overwrite_output = directory / "overwrite.json"
        overwrite_output.write_bytes(b"old\n")
        old_identity = file_identity(overwrite_output)
        overwrite_unrelated = directory / ".overwrite-unrelated"
        overwrite_unrelated.write_bytes(b"overwrite-unrelated\n")
        overwrite_identity = file_identity(overwrite_unrelated)
        captured_name: str | None = None

        def replace_overwrite_staging(descriptor: int, left: str, right: str) -> None:
            nonlocal captured_name
            captured_name = left
            real_replace(
                overwrite_unrelated.name,
                left,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            real_exchange(descriptor, left, right)

        overwrite_stderr = StringIO()
        with (
            patch(
                "scripts.report_sync_bundle._rename_exchange",
                side_effect=replace_overwrite_staging,
            ),
            redirect_stderr(overwrite_stderr),
        ):
            code = main(
                [
                    "--repository-root",
                    str(self.repository),
                    "--output",
                    "analysis_report/report_sync_bundles/overwrite.json",
                    "--overwrite",
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(
            overwrite_stderr.getvalue(),
            "report-sync bundle error: output_concurrent_change\n",
        )
        self.assertEqual(file_identity(overwrite_output), overwrite_identity)
        self.assertIsNotNone(captured_name)
        self.assertEqual(file_identity(directory / str(captured_name)), old_identity)

    def test_atomic_publish_detects_same_length_in_place_inode_mutation(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir()
        candidate = b"candidate\n"
        mutated = b"tampered!\n"
        self.assertEqual(len(candidate), len(mutated))
        real_create = report_sync_bundle._create_temporary_output
        real_no_replace = report_sync_bundle._rename_no_replace
        real_exchange = report_sync_bundle._rename_exchange

        for overwrite in (False, True):
            with self.subTest(staging_inode=overwrite):
                relative = "analysis_report/report_sync_bundles/" + (
                    "overwrite-staging.json" if overwrite else "create-staging.json"
                )
                output = self.repository / relative
                if overwrite:
                    output.write_bytes(b"previous!\n")
                captured_descriptors: list[int] = []

                def tracking_create(
                    directory_descriptor: int,
                    state: list[int] = captured_descriptors,
                ) -> tuple[int, str]:
                    result = real_create(directory_descriptor)
                    state[:] = [result[0]]
                    return result

                def mutate_staging_then_create(
                    directory_descriptor: int,
                    source: str,
                    destination: str,
                    state: list[int] = captured_descriptors,
                ) -> None:
                    self.assertEqual(len(state), 1)
                    os.pwrite(state[0], mutated, 0)
                    os.fsync(state[0])
                    real_no_replace(directory_descriptor, source, destination)

                def mutate_staging_then_exchange(
                    directory_descriptor: int,
                    left: str,
                    right: str,
                    state: list[int] = captured_descriptors,
                ) -> None:
                    self.assertEqual(len(state), 1)
                    os.pwrite(state[0], mutated, 0)
                    os.fsync(state[0])
                    real_exchange(directory_descriptor, left, right)

                rename_patch = (
                    patch(
                        "scripts.report_sync_bundle._rename_exchange",
                        side_effect=mutate_staging_then_exchange,
                    )
                    if overwrite
                    else patch(
                        "scripts.report_sync_bundle._rename_no_replace",
                        side_effect=mutate_staging_then_create,
                    )
                )
                with (
                    patch(
                        "scripts.report_sync_bundle._create_temporary_output",
                        side_effect=tracking_create,
                    ),
                    rename_patch,
                    self.assertRaisesRegex(
                        ReportSyncBundleError,
                        "output_concurrent_change",
                    ),
                ):
                    report_sync_bundle._write_output(
                        self.repository,
                        relative,
                        candidate,
                        overwrite=overwrite,
                    )
                self.assertEqual(output.read_bytes(), mutated)

        relative = "analysis_report/report_sync_bundles/old-target.json"
        output = self.repository / relative
        previous = b"previous!\n"
        self.assertEqual(len(previous), len(mutated))
        output.write_bytes(previous)
        recovery_name: str | None = None
        real_open = os.open

        def mutate_old_target_then_exchange(
            directory_descriptor: int,
            left: str,
            right: str,
        ) -> None:
            nonlocal recovery_name
            recovery_name = left
            old_descriptor = real_open(right, os.O_RDWR, dir_fd=directory_descriptor)
            try:
                os.pwrite(old_descriptor, mutated, 0)
                os.fsync(old_descriptor)
            finally:
                os.close(old_descriptor)
            real_exchange(directory_descriptor, left, right)

        with (
            patch(
                "scripts.report_sync_bundle._rename_exchange",
                side_effect=mutate_old_target_then_exchange,
            ),
            self.assertRaisesRegex(ReportSyncBundleError, "output_concurrent_change"),
        ):
            report_sync_bundle._write_output(
                self.repository,
                relative,
                candidate,
                overwrite=True,
            )
        self.assertEqual(output.read_bytes(), candidate)
        self.assertIsNotNone(recovery_name)
        self.assertEqual((directory / str(recovery_name)).read_bytes(), mutated)

    def test_atomic_publish_rechecks_early_target_and_final_visible_names(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir()
        candidate = b"candidate\n"
        previous = b"previous!\n"
        mutated = b"tampered!\n"
        self.assertEqual(len(previous), len(mutated))
        relative = "analysis_report/report_sync_bundles/early-target.json"
        output = self.repository / relative
        output.write_bytes(previous)
        real_create = report_sync_bundle._create_temporary_output

        def mutate_target_then_create(directory_descriptor: int) -> tuple[int, str]:
            target_descriptor = os.open(output.name, os.O_RDWR, dir_fd=directory_descriptor)
            try:
                os.pwrite(target_descriptor, mutated, 0)
                os.fsync(target_descriptor)
            finally:
                os.close(target_descriptor)
            return real_create(directory_descriptor)

        with (
            patch(
                "scripts.report_sync_bundle._create_temporary_output",
                side_effect=mutate_target_then_create,
            ),
            self.assertRaisesRegex(ReportSyncBundleError, "output_concurrent_change"),
        ):
            report_sync_bundle._write_output(
                self.repository,
                relative,
                candidate,
                overwrite=True,
            )
        self.assertEqual(output.read_bytes(), mutated)

        real_fsync = os.fsync
        real_replace = os.replace
        real_open_bundle = report_sync_bundle._open_bundle_directory
        for overwrite in (False, True):
            with self.subTest(final_visible_name=overwrite):
                suffix = "overwrite" if overwrite else "create"
                relative = f"analysis_report/report_sync_bundles/final-{suffix}.json"
                output = self.repository / relative
                if overwrite:
                    output.write_bytes(previous)
                attacker = directory / f".attacker-{suffix}"
                attacker.write_bytes(b"attacker!\n")
                directory_fsync_count = 0
                captured_directory: dict[str, int] = {}

                def capture_bundle_directory(
                    root: Path,
                    state: dict[str, int] = captured_directory,
                ) -> int:
                    descriptor = real_open_bundle(root)
                    state["descriptor"] = descriptor
                    return descriptor

                def replace_after_final_directory_fsync(
                    descriptor: int,
                    target_name: str = output.name,
                    attacker_name: str = attacker.name,
                    state: dict[str, int] = captured_directory,
                ) -> None:
                    nonlocal directory_fsync_count
                    real_fsync(descriptor)
                    if descriptor == state.get("descriptor"):
                        directory_fsync_count += 1
                        if directory_fsync_count == 2:
                            real_replace(
                                attacker_name,
                                target_name,
                                src_dir_fd=descriptor,
                                dst_dir_fd=descriptor,
                            )

                with (
                    patch(
                        "scripts.report_sync_bundle._open_bundle_directory",
                        side_effect=capture_bundle_directory,
                    ),
                    patch(
                        "scripts.report_sync_bundle.os.fsync",
                        side_effect=replace_after_final_directory_fsync,
                    ),
                    self.assertRaisesRegex(
                        ReportSyncBundleError,
                        "output_concurrent_change",
                    ),
                ):
                    report_sync_bundle._write_output(
                        self.repository,
                        relative,
                        candidate,
                        overwrite=overwrite,
                    )
                self.assertEqual(output.read_bytes(), b"attacker!\n")

        real_snapshot = report_sync_bundle._descriptor_snapshot
        real_create = report_sync_bundle._create_temporary_output
        for overwrite in (False, True):
            with self.subTest(final_directory_binding=overwrite):
                suffix = "overwrite" if overwrite else "create"
                relative = f"analysis_report/report_sync_bundles/rebind-{suffix}.json"
                output = self.repository / relative
                if overwrite:
                    output.write_bytes(previous)
                displaced_directory = self.repository / f"analysis_report/bundles-{suffix}.pinned"
                captured: dict[str, int] = {}
                candidate_commit_snapshots = 0

                def capture_candidate(
                    directory_descriptor: int,
                    state: dict[str, int] = captured,
                ) -> tuple[int, str]:
                    result = real_create(directory_descriptor)
                    state["descriptor"] = result[0]
                    return result

                def rebind_during_final_candidate_snapshot(
                    descriptor: int,
                    *,
                    limit: int,
                    code: str,
                    state: dict[str, int] = captured,
                    displaced: Path = displaced_directory,
                ) -> report_sync_bundle.DescriptorSnapshot:
                    nonlocal candidate_commit_snapshots
                    snapshot = real_snapshot(descriptor, limit=limit, code=code)
                    if descriptor == state.get("descriptor") and code == "output_commit_uncertain":
                        candidate_commit_snapshots += 1
                        if candidate_commit_snapshots == 2:
                            directory.rename(displaced)
                            directory.mkdir()
                    return snapshot

                try:
                    with (
                        patch(
                            "scripts.report_sync_bundle._create_temporary_output",
                            side_effect=capture_candidate,
                        ),
                        patch(
                            "scripts.report_sync_bundle._descriptor_snapshot",
                            side_effect=rebind_during_final_candidate_snapshot,
                        ),
                        self.assertRaisesRegex(
                            ReportSyncBundleError,
                            "output_commit_uncertain",
                        ),
                    ):
                        report_sync_bundle._write_output(
                            self.repository,
                            relative,
                            candidate,
                            overwrite=overwrite,
                        )
                    self.assertFalse(output.exists())
                    self.assertTrue((displaced_directory / output.name).exists())
                finally:
                    if directory.exists():
                        directory.rmdir()
                    if displaced_directory.exists():
                        displaced_directory.rename(directory)

    def test_target_replacement_at_exchange_preserves_named_inodes(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir()
        relative = "analysis_report/report_sync_bundles/bundle.json"
        output = self.repository / relative
        output.write_bytes(b"previous\n")
        old_anchor = directory / ".old-anchor"
        os.link(output, old_anchor)
        old_identity = file_identity(output)
        unrelated = directory / ".unrelated"
        unrelated.write_bytes(b"unrelated\n")
        unrelated_identity = file_identity(unrelated)
        real_exchange = report_sync_bundle._rename_exchange
        real_replace = os.replace
        captured: dict[str, str] = {}

        def replace_target_then_exchange(descriptor: int, left: str, right: str) -> None:
            captured["recovery"] = left
            real_replace(
                unrelated.name,
                right,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            real_exchange(descriptor, left, right)

        stderr = StringIO()
        with (
            patch(
                "scripts.report_sync_bundle._rename_exchange",
                side_effect=replace_target_then_exchange,
            ),
            redirect_stderr(stderr),
        ):
            code = main(
                [
                    "--repository-root",
                    str(self.repository),
                    "--output",
                    relative,
                    "--overwrite",
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(
            stderr.getvalue(),
            "report-sync bundle error: output_concurrent_change\n",
        )
        self.assertEqual(file_identity(old_anchor), old_identity)
        self.assertEqual(
            file_identity(directory / captured["recovery"]),
            unrelated_identity,
        )
        self.assertNotIn(file_identity(output), {old_identity, unrelated_identity})

    def test_real_atomic_rename_primitives_and_unsupported_platform(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir()
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            first = directory / ".first"
            second = directory / ".second"
            target = directory / "target.json"
            first.write_bytes(b"first\n")
            second.write_bytes(b"second\n")
            first_identity = file_identity(first)
            second_identity = file_identity(second)

            report_sync_bundle._rename_no_replace(descriptor, first.name, target.name)
            self.assertFalse(first.exists())
            self.assertEqual(file_identity(target), first_identity)

            first.write_bytes(b"replacement\n")
            replacement_identity = file_identity(first)
            with self.assertRaises(FileExistsError):
                report_sync_bundle._rename_no_replace(descriptor, first.name, target.name)
            self.assertEqual(file_identity(first), replacement_identity)
            self.assertEqual(file_identity(target), first_identity)

            report_sync_bundle._rename_exchange(descriptor, second.name, target.name)
            self.assertEqual(file_identity(second), first_identity)
            self.assertEqual(file_identity(target), second_identity)

            with patch.object(sys, "platform", "unsupported"):
                with self.assertRaisesRegex(
                    ReportSyncBundleError,
                    "output_atomic_publish_unsupported",
                ):
                    report_sync_bundle._rename_no_replace(descriptor, first.name, "unused.json")
            self.assertEqual(file_identity(first), replacement_identity)
            self.assertFalse((directory / "unused.json").exists())
        finally:
            os.close(descriptor)

    def test_linux_atomic_wrapper_flags_and_errno_mapping(self) -> None:
        class FakeFunction:
            def __init__(self, result: int, error_number: int = 0) -> None:
                self.result = result
                self.error_number = error_number
                self.calls: list[tuple[Any, ...]] = []
                self.argtypes: object = None
                self.restype: object = None

            def __call__(self, *args: Any) -> int:
                self.calls.append(args)
                ctypes.set_errno(self.error_number)
                return self.result

        class FakeLibrary:
            def __init__(self, function: FakeFunction) -> None:
                self.renameat2 = function

        for exchange, expected_flag in ((False, 1), (True, 2)):
            with self.subTest(exchange=exchange):
                function = FakeFunction(0)
                with (
                    patch.object(sys, "platform", "linux"),
                    patch(
                        "scripts.report_sync_bundle.ctypes.CDLL",
                        return_value=FakeLibrary(function),
                    ),
                ):
                    if exchange:
                        report_sync_bundle._rename_exchange(7, "left", "right")
                    else:
                        report_sync_bundle._rename_no_replace(7, "left", "right")
                self.assertEqual(function.calls[0][0], 7)
                self.assertEqual(function.calls[0][1], b"left")
                self.assertEqual(function.calls[0][3], b"right")
                self.assertEqual(function.calls[0][4], expected_flag)

        unsupported_errors = {
            errno.EINVAL,
            errno.ENOSYS,
            errno.ENOTSUP,
            errno.EOPNOTSUPP,
        }
        error_cases: list[tuple[int, type[BaseException]]] = [
            *((error_number, ReportSyncBundleError) for error_number in unsupported_errors),
            (errno.EEXIST, FileExistsError),
            (errno.ENOENT, FileNotFoundError),
        ]
        for error_number, expected_exception in error_cases:
            with self.subTest(error_number=error_number):
                function = FakeFunction(-1, error_number)
                with (
                    patch.object(sys, "platform", "linux"),
                    patch(
                        "scripts.report_sync_bundle.ctypes.CDLL",
                        return_value=FakeLibrary(function),
                    ),
                    self.assertRaises(expected_exception) as raised,
                ):
                    report_sync_bundle._rename_exchange(7, "left", "right")
                if error_number in unsupported_errors:
                    self.assertEqual(
                        str(raised.exception),
                        "output_atomic_publish_unsupported",
                    )

    def test_darwin_atomic_wrapper_flags_and_missing_native_symbol(self) -> None:
        class FakeFunction:
            def __init__(self) -> None:
                self.calls: list[tuple[Any, ...]] = []
                self.argtypes: object = None
                self.restype: object = None

            def __call__(self, *args: Any) -> int:
                self.calls.append(args)
                return 0

        class DarwinLibrary:
            def __init__(self, function: FakeFunction) -> None:
                self.renameatx_np = function

        for exchange, expected_flag in ((False, 4), (True, 2)):
            with self.subTest(exchange=exchange):
                function = FakeFunction()
                with (
                    patch.object(sys, "platform", "darwin"),
                    patch(
                        "scripts.report_sync_bundle.ctypes.CDLL",
                        return_value=DarwinLibrary(function),
                    ),
                ):
                    if exchange:
                        report_sync_bundle._rename_exchange(11, "left", "right")
                    else:
                        report_sync_bundle._rename_no_replace(11, "left", "right")
                self.assertEqual(function.calls[0][0], 11)
                self.assertEqual(function.calls[0][1], b"left")
                self.assertEqual(function.calls[0][3], b"right")
                self.assertEqual(function.calls[0][4], expected_flag)

        class MissingNativeLibrary:
            pass

        for platform in ("darwin", "linux"):
            with (
                self.subTest(platform=platform),
                patch.object(sys, "platform", platform),
                patch(
                    "scripts.report_sync_bundle.ctypes.CDLL",
                    return_value=MissingNativeLibrary(),
                ),
                self.assertRaisesRegex(
                    ReportSyncBundleError,
                    "output_atomic_publish_unsupported",
                ),
            ):
                report_sync_bundle._rename_no_replace(11, "left", "right")

    def test_atomic_publish_base_exceptions_preserve_committed_inodes(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir()
        real_no_replace = report_sync_bundle._rename_no_replace
        real_exchange = report_sync_bundle._rename_exchange
        interruptions: tuple[BaseException, ...] = (
            KeyboardInterrupt(),
            SystemExit(17),
            GeneratorExit(),
            asyncio.CancelledError(),
        )

        for index, interruption in enumerate(interruptions):
            with self.subTest(mode="create", interruption=type(interruption).__name__):
                relative = f"analysis_report/report_sync_bundles/create-{index}.json"

                def create_then_interrupt(
                    descriptor: int,
                    source: str,
                    destination: str,
                    *,
                    error: BaseException = interruption,
                ) -> None:
                    real_no_replace(descriptor, source, destination)
                    raise error

                with patch(
                    "scripts.report_sync_bundle._rename_no_replace",
                    side_effect=create_then_interrupt,
                ):
                    with self.assertRaises(type(interruption)) as raised:
                        report_sync_bundle._write_output(
                            self.repository,
                            relative,
                            b"candidate\n",
                        )
                self.assertIs(raised.exception, interruption)
                self.assertEqual((self.repository / relative).read_bytes(), b"candidate\n")

        for index, interruption in enumerate(interruptions):
            with self.subTest(mode="overwrite", interruption=type(interruption).__name__):
                relative = f"analysis_report/report_sync_bundles/overwrite-{index}.json"
                output = self.repository / relative
                output.write_bytes(b"previous\n")
                previous_identity = file_identity(output)
                captured: dict[str, str] = {}

                def exchange_then_interrupt(
                    descriptor: int,
                    left: str,
                    right: str,
                    *,
                    error: BaseException = interruption,
                    state: dict[str, str] = captured,
                ) -> None:
                    state["recovery"] = left
                    real_exchange(descriptor, left, right)
                    raise error

                with patch(
                    "scripts.report_sync_bundle._rename_exchange",
                    side_effect=exchange_then_interrupt,
                ):
                    with self.assertRaises(type(interruption)) as raised:
                        report_sync_bundle._write_output(
                            self.repository,
                            relative,
                            b"candidate\n",
                            overwrite=True,
                        )
                self.assertIs(raised.exception, interruption)
                self.assertEqual(output.read_bytes(), b"candidate\n")
                self.assertEqual(
                    file_identity(directory / captured["recovery"]),
                    previous_identity,
                )

    def test_exchange_oserror_after_side_effect_is_fixed_and_non_destructive(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir()
        relative = "analysis_report/report_sync_bundles/bundle.json"
        output = self.repository / relative
        output.write_bytes(b"previous\n")
        previous_identity = file_identity(output)
        real_exchange = report_sync_bundle._rename_exchange
        captured: dict[str, str] = {}

        def exchange_then_fail(descriptor: int, left: str, right: str) -> None:
            captured["recovery"] = left
            real_exchange(descriptor, left, right)
            raise OSError("injected private post-exchange path")

        stderr = StringIO()
        with (
            patch(
                "scripts.report_sync_bundle._rename_exchange",
                side_effect=exchange_then_fail,
            ),
            redirect_stderr(stderr),
        ):
            code = main(
                [
                    "--repository-root",
                    str(self.repository),
                    "--output",
                    relative,
                    "--overwrite",
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(
            stderr.getvalue(),
            "report-sync bundle error: output_commit_uncertain\n",
        )
        self.assertNotIn("private", stderr.getvalue())
        self.assertNotEqual(file_identity(output), previous_identity)
        self.assertEqual(file_identity(directory / captured["recovery"]), previous_identity)

    def test_directory_close_error_is_fixed_without_hiding_committed_output(self) -> None:
        directory = self.repository / "analysis_report/report_sync_bundles"
        directory.mkdir()
        relative = "analysis_report/report_sync_bundles/bundle.json"
        real_open_bundle = report_sync_bundle._open_bundle_directory
        real_close = os.close
        captured: dict[str, int] = {}

        def tracking_open(root: Path) -> int:
            descriptor = real_open_bundle(root)
            captured["directory"] = descriptor
            return descriptor

        def close_then_fail(descriptor: int) -> None:
            real_close(descriptor)
            if descriptor == captured.get("directory"):
                raise PermissionError("injected private close path")

        stderr = StringIO()
        with (
            patch(
                "scripts.report_sync_bundle._open_bundle_directory",
                side_effect=tracking_open,
            ),
            patch("scripts.report_sync_bundle.os.close", side_effect=close_then_fail),
            redirect_stderr(stderr),
        ):
            code = main(
                [
                    "--repository-root",
                    str(self.repository),
                    "--output",
                    relative,
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(
            stderr.getvalue(),
            "report-sync bundle error: output_commit_uncertain\n",
        )
        self.assertNotIn("private", stderr.getvalue())
        self.assertTrue((self.repository / relative).is_file())


if __name__ == "__main__":
    unittest.main()
