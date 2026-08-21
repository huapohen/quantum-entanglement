import base64
import copy
import csv
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path, PurePosixPath

from scripts.distribution_manifest import (
    DistributionManifestError,
    generate_distribution_manifest,
    load_distribution_manifest,
    main,
    verify_distribution_manifest,
)
from scripts.generate_release_evidence import canonical_json

T0 = datetime(2026, 8, 20, 12, 34, 56, 789, tzinfo=timezone.utc)


class DistributionManifestTests(unittest.TestCase):
    project_name = "quantum-entanglement"
    normalized_name = "quantum_entanglement"
    version = "0.1.0"

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.repository = self.base / "repository"
        self.distributions = self.base / "dist"
        self.repository.mkdir()
        self.distributions.mkdir()
        self._write_source()
        self._git("init", "-q")
        self._git("config", "user.name", "Distribution Manifest Test")
        self._git("config", "user.email", "distribution-manifest@example.invalid")
        self._git("add", ".")
        self._git("commit", "-qm", "fixture")
        self._write_distributions()

    def tearDown(self):
        self.tempdir.cleanup()

    def _git(self, *arguments):
        return subprocess.run(
            ("git", "-C", str(self.repository), *arguments),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _write_source(self):
        package = self.repository / "src" / self.normalized_name
        tests = self.repository / "tests"
        package.mkdir(parents=True)
        tests.mkdir()
        (self.repository / "LICENSE").write_text("fixture license\n", encoding="utf-8")
        (self.repository / "MANIFEST.in").write_text(
            "include tests/__init__.py\n", encoding="utf-8"
        )
        (self.repository / "README.md").write_text("# Fixture\n", encoding="utf-8")
        (self.repository / "pyproject.toml").write_text(
            "[build-system]\n"
            'requires = ["setuptools>=77"]\n'
            'build-backend = "setuptools.build_meta"\n\n'
            "[project]\n"
            f'name = "{self.project_name}"\n'
            f'version = "{self.version}"\n',
            encoding="utf-8",
        )
        (package / "__init__.py").write_text(f'__version__ = "{self.version}"\n', encoding="utf-8")
        (package / "admin_cli.py").write_text("def main(): return 0\n", encoding="utf-8")
        (package / "cli.py").write_text("def main(): return 0\n", encoding="utf-8")
        (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        (tests / "__init__.py").write_text('"""Fixture tests."""\n', encoding="utf-8")
        (tests / "test_fixture.py").write_text("def test_fixture(): pass\n", encoding="utf-8")

    @property
    def metadata(self):
        return (
            f"Metadata-Version: 2.4\nName: {self.project_name}\nVersion: {self.version}\n\n"
        ).encode()

    @property
    def entry_points(self):
        return (
            b"[console_scripts]\n"
            b"qe-admin = quantum_entanglement.admin_cli:main\n"
            b"qe-demo = quantum_entanglement.cli:main\n"
        )

    def _source_files(self):
        result = {}
        for prefix in ("src", "tests"):
            for path in sorted((self.repository / prefix).rglob("*")):
                if path.is_file():
                    result[path.relative_to(self.repository).as_posix()] = path.read_bytes()
        for name in ("LICENSE", "MANIFEST.in", "README.md", "pyproject.toml"):
            result[name] = (self.repository / name).read_bytes()
        return result

    @staticmethod
    def _record(files, record_name):
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        for name, value in sorted(files.items()):
            digest = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=")
            writer.writerow((name, "sha256=" + digest.decode("ascii"), str(len(value))))
        writer.writerow((record_name, "", ""))
        return output.getvalue().encode("utf-8")

    def _wheel_files(self, *, source_override=None, invalid_record=False):
        source = self._source_files()
        package_files = {
            name[len("src/") :]: value
            for name, value in source.items()
            if name.startswith(f"src/{self.normalized_name}/")
        }
        if source_override is not None:
            package_files[f"{self.normalized_name}/module.py"] = source_override
        dist_info = f"{self.normalized_name}-{self.version}.dist-info"
        files = {
            **package_files,
            f"{dist_info}/licenses/LICENSE": source["LICENSE"],
            f"{dist_info}/METADATA": self.metadata,
            f"{dist_info}/WHEEL": (
                b"Wheel-Version: 1.0\n"
                b"Generator: fixture\n"
                b"Root-Is-Purelib: true\n"
                b"Tag: py3-none-any\n\n"
            ),
            f"{dist_info}/entry_points.txt": self.entry_points,
            f"{dist_info}/top_level.txt": f"{self.normalized_name}\n".encode("ascii"),
        }
        record_name = f"{dist_info}/RECORD"
        files[record_name] = self._record(files, record_name)
        if invalid_record:
            files[record_name] = files[record_name].replace(b"sha256=", b"sha512=", 1)
        return files

    def _write_wheel(self, *, source_override=None, invalid_record=False, traversal=False):
        path = self.distributions / (f"{self.normalized_name}-{self.version}-py3-none-any.whl")
        files = self._wheel_files(
            source_override=source_override,
            invalid_record=invalid_record,
        )
        if traversal:
            files["../outside"] = b"escape"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in files.items():
                archive.writestr(name, value)
        return path

    def _sdist_files(self, *, setup_override=None):
        root = f"{self.normalized_name}-{self.version}"
        source = self._source_files()
        egg_info = f"src/{self.normalized_name}.egg-info"
        egg_names = (
            "PKG-INFO",
            "SOURCES.txt",
            "dependency_links.txt",
            "entry_points.txt",
            "requires.txt",
            "top_level.txt",
        )
        source_lines = sorted(source)
        source_lines.extend(f"{egg_info}/{name}" for name in egg_names)
        generated = {
            "PKG-INFO": self.metadata,
            "setup.cfg": (
                b"[egg_info]\ntag_build = \ntag_date = 0\n\n"
                if setup_override is None
                else setup_override
            ),
            f"{egg_info}/PKG-INFO": self.metadata,
            f"{egg_info}/SOURCES.txt": ("\n".join(source_lines) + "\n").encode("utf-8"),
            f"{egg_info}/dependency_links.txt": b"\n",
            f"{egg_info}/entry_points.txt": self.entry_points,
            f"{egg_info}/requires.txt": b"\n",
            f"{egg_info}/top_level.txt": f"{self.normalized_name}\n".encode("ascii"),
        }
        return root, {
            **{f"{root}/{name}": value for name, value in source.items()},
            **{f"{root}/{name}": value for name, value in generated.items()},
        }

    def _write_sdist(self, *, add_link=False, setup_override=None):
        root, files = self._sdist_files(setup_override=setup_override)
        path = self.distributions / f"{self.normalized_name}-{self.version}.tar.gz"
        directories = {root}
        for name in files:
            parts = PurePosixPath(name).parts
            for index in range(1, len(parts)):
                directories.add("/".join(parts[:index]))
        with tarfile.open(path, "w:gz") as archive:
            for name in sorted(directories, key=lambda item: (item.count("/"), item)):
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            for name, value in sorted(files.items()):
                info = tarfile.TarInfo(name)
                info.size = len(value)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(value))
            if add_link:
                link = tarfile.TarInfo(f"{root}/malicious-link")
                link.type = tarfile.SYMTYPE
                link.linkname = "../../outside"
                archive.addfile(link)
        return path

    def _write_distributions(self, **wheel_options):
        self._write_wheel(**wheel_options)
        self._write_sdist()

    def _generate(self):
        return generate_distribution_manifest(
            self.repository,
            self.distributions,
            clock=lambda: T0,
        )

    def test_valid_wheel_and_sdist_generate_and_verify_source_bound_manifest(self):
        manifest = self._generate()

        self.assertEqual(manifest["format"], "quantum-entanglement.distribution-manifest")
        self.assertEqual(manifest["generatedAt"], "2026-08-20T12:34:56.000789Z")
        self.assertEqual(manifest["project"], {"name": self.project_name, "version": self.version})
        self.assertEqual([item["kind"] for item in manifest["artifacts"]], ["sdist", "wheel"])
        self.assertEqual(manifest["source"]["commitSha"], self._git("rev-parse", "HEAD"))
        self.assertFalse(manifest["source"]["dirty"])

        verify_distribution_manifest(
            manifest,
            self.repository,
            self.distributions,
            expected_commit_sha=self._git("rev-parse", "HEAD"),
        )

    def test_wheel_source_bytes_and_record_must_both_match(self):
        for options, code in (
            ({"source_override": b"VALUE = 999\n"}, "wheel_source_mismatch"),
            ({"invalid_record": True}, "wheel_record_invalid"),
        ):
            with self.subTest(code=code):
                self._write_wheel(**options)
                with self.assertRaisesRegex(DistributionManifestError, code):
                    self._generate()
                self._write_wheel()

    def test_archive_traversal_and_sdist_links_are_rejected(self):
        self._write_wheel(traversal=True)
        with self.assertRaisesRegex(DistributionManifestError, "wheel_path_invalid"):
            self._generate()

        self._write_wheel()
        self._write_sdist(add_link=True)
        with self.assertRaisesRegex(DistributionManifestError, "sdist_link_or_special_file"):
            self._generate()

    def test_generated_sdist_configuration_cannot_change_build_semantics(self):
        self._write_sdist(setup_override=b"[options]\ninstall_requires = attacker\n")

        with self.assertRaisesRegex(DistributionManifestError, "sdist_setup_invalid"):
            self._generate()

    def test_missing_or_extra_distribution_is_rejected(self):
        wheel = next(self.distributions.glob("*.whl"))
        wheel.unlink()
        with self.assertRaisesRegex(DistributionManifestError, "distribution_set_invalid"):
            self._generate()

        self._write_wheel()
        (self.distributions / "stale.whl").write_bytes(b"stale")
        with self.assertRaisesRegex(DistributionManifestError, "distribution_set_invalid"):
            self._generate()

    def test_dirty_source_or_version_disagreement_fails_closed(self):
        (self.repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(DistributionManifestError, "repository_not_clean"):
            self._generate()
        (self.repository / "untracked.txt").unlink()

        init = self.repository / "src" / self.normalized_name / "__init__.py"
        init.write_text('__version__ = "9.9.9"\n', encoding="utf-8")
        self._git("add", str(init.relative_to(self.repository)))
        self._git("commit", "-qm", "mismatched version")
        with self.assertRaisesRegex(DistributionManifestError, "project_version_invalid"):
            self._generate()

    def test_tampered_manifest_artifact_source_or_shape_is_rejected(self):
        manifest = self._generate()
        cases = []
        artifact = copy.deepcopy(manifest)
        artifact["artifacts"][0]["sha256"] = "0" * 64
        cases.append((artifact, "manifest_artifact_mismatch"))
        source = copy.deepcopy(manifest)
        source["source"]["commitSha"] = "0" * 40
        cases.append((source, "manifest_source_mismatch"))
        extra = copy.deepcopy(manifest)
        extra["unexpected"] = True
        cases.append((extra, "manifest_shape_invalid"))
        false_dirty = copy.deepcopy(manifest)
        false_dirty["source"]["dirty"] = True
        cases.append((false_dirty, "manifest_source_invalid"))
        runtime = copy.deepcopy(manifest)
        runtime["inspectionRuntime"]["pythonVersion"] = "0.0.0"
        cases.append((runtime, "manifest_runtime_mismatch"))

        for candidate, code in cases:
            with self.subTest(code=code):
                with self.assertRaisesRegex(DistributionManifestError, code):
                    verify_distribution_manifest(candidate, self.repository, self.distributions)

    def test_manifest_loader_requires_canonical_unambiguous_json(self):
        manifest = self._generate()
        path = self.base / "manifest.json"
        invalid = (
            json.dumps(manifest, indent=2),
            '{"format":"duplicate",' + canonical_json(manifest)[1:],
            '{"value":NaN}\n',
        )
        for value in invalid:
            with self.subTest(prefix=value[:20]):
                path.write_text(value, encoding="utf-8")
                with self.assertRaisesRegex(DistributionManifestError, "manifest_file_invalid"):
                    load_distribution_manifest(path)

        path.write_text(canonical_json(manifest), encoding="utf-8")
        self.assertEqual(load_distribution_manifest(path), manifest)

    def test_cli_verifies_out_of_tree_manifest_and_rejects_in_tree_copy(self):
        manifest = self._generate()
        outside = self.base / "manifest.json"
        outside.write_text(canonical_json(manifest), encoding="utf-8")
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(
                (
                    "verify",
                    str(outside),
                    "--repository-root",
                    str(self.repository),
                    "--distribution-directory",
                    str(self.distributions),
                    "--expected-commit",
                    self._git("rev-parse", "HEAD"),
                )
            )
        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "distribution manifest verified\n")
        self.assertEqual(stderr.getvalue(), "")

        inside = self.repository / "ignored-manifest.json"
        (self.repository / ".git" / "info" / "exclude").write_text(
            "ignored-manifest.json\n", encoding="utf-8"
        )
        inside.write_text(canonical_json(manifest), encoding="utf-8")
        stderr = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(stderr):
            result = main(
                (
                    "verify",
                    str(inside),
                    "--repository-root",
                    str(self.repository),
                    "--distribution-directory",
                    str(self.distributions),
                )
            )
        self.assertEqual(result, 1)
        self.assertEqual(
            stderr.getvalue(),
            "distribution manifest failed: manifest_inside_repository\n",
        )

    def test_cli_failure_does_not_echo_secret_content_or_paths(self):
        path = self.base / "secret-manifest.json"
        secret = "distribution-secret-canary"
        path.write_text(secret, encoding="utf-8")
        stderr = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(stderr):
            result = main(
                (
                    "verify",
                    str(path),
                    "--repository-root",
                    str(self.repository),
                    "--distribution-directory",
                    str(self.distributions),
                )
            )
        self.assertEqual(result, 1)
        self.assertRegex(stderr.getvalue(), r"^distribution manifest failed: [a-z_]+\n$")
        self.assertNotIn(secret, stderr.getvalue())
        self.assertNotIn(str(path), stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
