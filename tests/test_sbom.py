import copy
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from io import StringIO
from pathlib import Path

from scripts.sbom import (
    SbomError,
    generate_sbom_documents,
    main,
    validate_sbom_bytes,
    verify_sbom_directory,
    write_sbom_documents,
)
from scripts.verify_dependency_locks import LockedPackage, verify_dependency_locks

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILENAME = "quantum-entanglement-runtime.cdx.json"
BUILD_FILENAME = "quantum-entanglement-build.cdx.json"


def canonical_json(value):
    return (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True, separators=(",", ": "))
        + "\n"
    ).encode("ascii")


class SbomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.targets = verify_dependency_locks(PROJECT_ROOT)

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def manifest():
        return {
            "project": {"name": "quantum-entanglement", "version": "0.1.0"},
            "source": {"commitSha": "a" * 40, "treeSha": "b" * 40},
            "artifacts": [
                {
                    "byteSize": 123,
                    "filename": "quantum_entanglement-0.1.0.tar.gz",
                    "kind": "sdist",
                    "sha256": "c" * 64,
                },
                {
                    "byteSize": 456,
                    "filename": "quantum_entanglement-0.1.0-py3-none-any.whl",
                    "kind": "wheel",
                    "sha256": "d" * 64,
                },
            ],
        }

    def documents(self):
        return generate_sbom_documents(PROJECT_ROOT, self.manifest(), self.targets)

    def _assert_code(self, code, callback):
        with self.assertRaisesRegex(SbomError, f"^{code}$"):
            callback()

    def test_runtime_sbom_is_deterministic_and_binds_source_and_artifacts(self):
        first = self.documents()
        second = self.documents()
        self.assertEqual(first, second)

        runtime = validate_sbom_bytes(first[RUNTIME_FILENAME], kind="runtime")
        root = runtime["metadata"]["component"]
        properties = {item["name"]: item["value"] for item in root["properties"]}
        self.assertEqual(root["purl"], "pkg:pypi/quantum-entanglement@0.1.0")
        self.assertEqual(properties["quantum-entanglement:source:commit-sha"], "a" * 40)
        self.assertEqual(properties["quantum-entanglement:source:tree-sha"], "b" * 40)
        self.assertEqual(properties["quantum-entanglement:artifact:sdist:sha256"], "c" * 64)
        self.assertEqual(properties["quantum-entanglement:artifact:wheel:sha256"], "d" * 64)
        self.assertEqual(properties["quantum-entanglement:runtime:base-dependency-count"], "0")
        self.assertNotIn("components", runtime)
        self.assertNotIn("timestamp", runtime["metadata"])
        self.assertNotIn("serialNumber", runtime)

    def test_build_sbom_covers_every_exact_lock_component_and_target(self):
        build = validate_sbom_bytes(self.documents()[BUILD_FILENAME], kind="build")
        components = build["components"]
        self.assertEqual(len(components), 51)
        self.assertEqual(len(build["dependencies"]), 52)
        references = {component["bom-ref"] for component in components}
        self.assertIn("pkg:pypi/pip@26.0.1", references)
        self.assertIn("pkg:pypi/cyclonedx-bom@7.3.1", references)
        self.assertIn("pkg:pypi/setuptools@82.0.1", references)

        pip_component = next(component for component in components if component["name"] == "pip")
        pip_properties = {
            item["name"]: item["value"] for item in pip_component["properties"]
        }
        self.assertEqual(
            set(pip_properties["quantum-entanglement:lock:targets"].split(",")),
            {
                "build|cp3.12|x86_64-unknown-linux-gnu",
                "dev|cp3.12|x86_64-unknown-linux-gnu",
                "dev|cp3.9|x86_64-unknown-linux-gnu",
                "release|cp3.12|x86_64-unknown-linux-gnu",
            },
        )
        digests = pip_properties["quantum-entanglement:lock:artifact-sha256"].split(",")
        self.assertTrue(digests)
        self.assertTrue(all(len(digest) == 64 for digest in digests))

    def test_documents_do_not_embed_local_paths_or_nondeterministic_fields(self):
        for value in self.documents().values():
            self.assertNotIn(str(PROJECT_ROOT).encode(), value)
            self.assertNotIn(b'"timestamp"', value)
            self.assertNotIn(b'"serialNumber"', value)
            self.assertNotIn(b"file://", value)

    def test_nonempty_base_runtime_dependencies_fail_closed(self):
        repository = self.base / "repository"
        repository.mkdir()
        source = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        (repository / "pyproject.toml").write_text(
            source.replace('dependencies = []', 'dependencies = ["requests==2.32.5"]', 1),
            encoding="utf-8",
        )

        self._assert_code(
            "runtime_dependencies_unlocked",
            lambda: generate_sbom_documents(repository, self.manifest(), self.targets),
        )

    def test_invalid_artifact_identity_and_order_are_rejected(self):
        for mutate in (
            lambda manifest: manifest["artifacts"][0].update(filename="../escape.tar.gz"),
            lambda manifest: manifest["artifacts"][0].update(sha256="ABC"),
            lambda manifest: manifest["artifacts"].reverse(),
        ):
            with self.subTest(mutate=mutate):
                manifest = self.manifest()
                mutate(manifest)
                self._assert_code(
                    "source_manifest_invalid",
                    lambda value=manifest: generate_sbom_documents(
                        PROJECT_ROOT, value, self.targets
                    ),
                )

    def test_same_component_cannot_have_conflicting_lock_hash_sets(self):
        targets = list(self.targets)
        target = targets[1]
        packages = list(target.packages)
        index = next(i for i, package in enumerate(packages) if package.name == "pip")
        packages[index] = LockedPackage(name="pip", version="26.0.1", sha256=("e" * 64,))
        targets[1] = replace(target, packages=tuple(packages))

        self._assert_code(
            "lock_component_hash_mismatch",
            lambda: generate_sbom_documents(PROJECT_ROOT, self.manifest(), targets),
        )

    def test_noncanonical_unknown_and_duplicate_json_are_rejected(self):
        runtime_bytes = self.documents()[RUNTIME_FILENAME]
        runtime = json.loads(runtime_bytes)
        runtime["unknown"] = True
        self._assert_code(
            "sbom_shape_invalid",
            lambda: validate_sbom_bytes(canonical_json(runtime), kind="runtime"),
        )
        self._assert_code(
            "sbom_noncanonical",
            lambda: validate_sbom_bytes(runtime_bytes.rstrip() + b"  \n", kind="runtime"),
        )
        self._assert_code(
            "sbom_json_invalid",
            lambda: validate_sbom_bytes(b'{"bomFormat":"a","bomFormat":"b"}\n', kind="runtime"),
        )

    def test_duplicate_components_and_graph_escape_are_rejected(self):
        build = json.loads(self.documents()[BUILD_FILENAME])
        build["components"].append(copy.deepcopy(build["components"][0]))
        self._assert_code(
            "sbom_component_invalid",
            lambda: validate_sbom_bytes(canonical_json(build), kind="build"),
        )

        build = json.loads(self.documents()[BUILD_FILENAME])
        build["dependencies"][0]["dependsOn"].append("pkg:pypi/unknown@1")
        self._assert_code(
            "sbom_dependency_invalid",
            lambda: validate_sbom_bytes(canonical_json(build), kind="build"),
        )

    def test_property_order_duplicates_and_path_leaks_are_rejected(self):
        for mutate, code in (
            (
                lambda root: root["properties"].reverse(),
                "sbom_property_invalid",
            ),
            (
                lambda root: root["properties"].append(copy.deepcopy(root["properties"][0])),
                "sbom_property_invalid",
            ),
            (
                lambda root: root["properties"][0].update(value="/Users/example/secret"),
                "sbom_path_leak",
            ),
        ):
            with self.subTest(code=code):
                runtime = json.loads(self.documents()[RUNTIME_FILENAME])
                mutate(runtime["metadata"]["component"])
                self._assert_code(
                    code,
                    lambda value=runtime: validate_sbom_bytes(
                        canonical_json(value), kind="runtime"
                    ),
                )

    def test_exact_document_set_can_be_written_once_and_verified(self):
        output = self.base / "sbom"
        output.mkdir()
        documents = self.documents()

        write_sbom_documents(output, documents, repository_root=PROJECT_ROOT)
        actual = verify_sbom_directory(output, documents, repository_root=PROJECT_ROOT)
        self.assertEqual(actual, documents)
        self.assertEqual(
            {path.name for path in output.iterdir()},
            {RUNTIME_FILENAME, BUILD_FILENAME},
        )
        self._assert_code(
            "sbom_directory_not_empty",
            lambda: write_sbom_documents(output, documents, repository_root=PROJECT_ROOT),
        )

    def test_output_directory_must_be_outside_repository_and_empty(self):
        repository = self.base / "repository"
        output = repository / "sbom"
        output.mkdir(parents=True)
        self._assert_code(
            "sbom_directory_inside_repository",
            lambda: write_sbom_documents(output, self.documents(), repository_root=repository),
        )

        outside = self.base / "outside"
        outside.mkdir()
        (outside / "unexpected").write_text("x", encoding="ascii")
        self._assert_code(
            "sbom_directory_not_empty",
            lambda: write_sbom_documents(outside, self.documents(), repository_root=repository),
        )

    def test_verifier_rejects_drift_extra_files_symlinks_and_oversize(self):
        documents = self.documents()
        output = self.base / "sbom"
        output.mkdir()
        write_sbom_documents(output, documents, repository_root=PROJECT_ROOT)

        runtime_path = output / RUNTIME_FILENAME
        runtime = json.loads(runtime_path.read_bytes())
        runtime["metadata"]["component"]["properties"][0]["value"] = "999"
        runtime_path.write_bytes(canonical_json(runtime))
        self._assert_code(
            "sbom_drift",
            lambda: verify_sbom_directory(output, documents, repository_root=PROJECT_ROOT),
        )

        runtime_path.write_bytes(documents[RUNTIME_FILENAME])
        (output / "unexpected").write_text("x", encoding="ascii")
        self._assert_code(
            "sbom_document_set_invalid",
            lambda: verify_sbom_directory(output, documents, repository_root=PROJECT_ROOT),
        )
        (output / "unexpected").unlink()

        runtime_path.unlink()
        runtime_path.symlink_to(output / BUILD_FILENAME)
        self._assert_code(
            "sbom_file_invalid",
            lambda: verify_sbom_directory(output, documents, repository_root=PROJECT_ROOT),
        )

        runtime_path.unlink()
        runtime_path.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
        self._assert_code(
            "sbom_file_invalid",
            lambda: verify_sbom_directory(output, documents, repository_root=PROJECT_ROOT),
        )

    def test_cli_failure_is_fixed_code_and_does_not_echo_paths(self):
        missing = self.base / "sensitive-name" / "manifest.json"
        stderr = StringIO()
        with redirect_stderr(stderr):
            exit_code = main(
                [
                    "verify",
                    "--repository-root",
                    str(PROJECT_ROOT),
                    "--distribution-directory",
                    str(self.base / "dist"),
                    "--distribution-manifest",
                    str(missing),
                    "--sbom-directory",
                    str(self.base),
                    "--expected-commit",
                    "a" * 40,
                ]
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), "SBOM operation failed: source_evidence_invalid\n")
        self.assertNotIn(str(missing), stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
