import gzip
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import normalize_sdist as normalizer


class NormalizeSdistTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _write_archive(
        self,
        path,
        *,
        source_epoch=100,
        gzip_epoch=200,
        owner=123,
        group=456,
        reverse=False,
        special_type=None,
        duplicate=False,
        traversal=False,
        root_name=None,
    ):
        root_name = root_name or path.name[: -len(".tar.gz")]
        members = [
            (root_name, None, 0o777),
            (f"{root_name}/README.md", b"read me\n", 0o664),
            (f"{root_name}/bin/tool", b"#!/bin/sh\n", 0o775),
        ]
        if reverse:
            members.reverse()
        if duplicate:
            members.append((f"{root_name}/README.md", b"again", 0o644))
        if traversal:
            members.append((f"{root_name}/../escape", b"bad", 0o644))
        with path.open("wb") as raw:
            with gzip.GzipFile(
                filename="builder-name", mode="wb", fileobj=raw, mtime=gzip_epoch
            ) as gz:
                with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    for name, body, mode in members:
                        info = tarfile.TarInfo(name)
                        info.mtime = source_epoch
                        info.uid = owner
                        info.gid = group
                        info.uname = "builder"
                        info.gname = "staff"
                        info.mode = mode
                        if body is None:
                            info.type = tarfile.DIRTYPE
                            archive.addfile(info)
                        else:
                            info.size = len(body)
                            archive.addfile(info, io.BytesIO(body))
                    if special_type is not None:
                        special = tarfile.TarInfo(f"{root_name}/link")
                        special.type = special_type
                        special.linkname = "README.md"
                        archive.addfile(special)

    def _metadata(self, path):
        with tarfile.open(path, "r:gz") as archive:
            return [
                (
                    member.name,
                    member.type,
                    member.mtime,
                    member.uid,
                    member.gid,
                    member.uname,
                    member.gname,
                    member.mode,
                )
                for member in archive.getmembers()
            ]

    def test_different_source_metadata_normalizes_to_identical_bytes(self):
        first_directory = self.root / "first"
        second_directory = self.root / "second"
        first_directory.mkdir()
        second_directory.mkdir()
        first = first_directory / "quantum_entanglement-0.1.0.tar.gz"
        second = second_directory / "quantum_entanglement-0.1.0.tar.gz"
        self._write_archive(first, source_epoch=11, gzip_epoch=12, owner=501, reverse=False)
        self._write_archive(second, source_epoch=99, gzip_epoch=98, owner=1000, reverse=True)

        first_result = normalizer.normalize_sdist(first, "1700000000")
        second_result = normalizer.normalize_sdist(second, "1700000000")

        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(first_result["sha256"], second_result["sha256"])
        self.assertEqual(int.from_bytes(first.read_bytes()[4:8], "little"), 1700000000)
        metadata = self._metadata(first)
        self.assertEqual([item[0] for item in metadata], sorted(item[0] for item in metadata))
        self.assertTrue(all(item[2] == 1700000000 for item in metadata))
        self.assertTrue(all(item[3:7] == (0, 0, "", "") for item in metadata))
        self.assertEqual(metadata[0][-1], 0o755)
        self.assertEqual(metadata[1][-1], 0o644)
        self.assertEqual(metadata[2][-1], 0o755)

    def test_normalization_is_idempotent_and_summary_is_canonical(self):
        archive = self.root / "project-0.1.0.tar.gz"
        self._write_archive(archive)
        result = normalizer.normalize_sdist(archive, "0")
        first = archive.read_bytes()
        second_result = normalizer.normalize_sdist(archive, "0")

        self.assertEqual(archive.read_bytes(), first)
        self.assertEqual(second_result, result)
        self.assertEqual(result["archive"], archive.name)
        self.assertEqual(result["sourceDateEpoch"], 0)

    def test_traversal_duplicates_and_links_are_rejected(self):
        cases = [
            ({"traversal": True}, "member_name_invalid"),
            ({"duplicate": True}, "member_duplicate"),
            ({"special_type": tarfile.SYMTYPE}, "member_type_forbidden"),
            ({"special_type": tarfile.LNKTYPE}, "member_type_forbidden"),
        ]
        for index, (arguments, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                archive = self.root / f"case-{index}.tar.gz"
                self._write_archive(archive, **arguments)
                with self.assertRaisesRegex(normalizer.SdistNormalizationError, expected):
                    normalizer.normalize_sdist(archive, "1")

        wrong_root = self.root / "expected-0.1.0.tar.gz"
        self._write_archive(wrong_root, root_name="unexpected-0.1.0")
        with self.assertRaisesRegex(normalizer.SdistNormalizationError, "archive_root_invalid"):
            normalizer.normalize_sdist(wrong_root, "1")

    def test_bounds_and_epoch_are_fail_closed(self):
        archive = self.root / "project-0.1.0.tar.gz"
        self._write_archive(archive)
        for value in (None, "", "-1", "+1", "01", "1.0", str(1 << 32)):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    normalizer.SdistNormalizationError, "source_date_epoch_invalid"
                ):
                    normalizer.normalize_sdist(archive, value)

        with mock.patch.object(normalizer, "_MAX_MEMBER_COUNT", 1):
            with self.assertRaisesRegex(normalizer.SdistNormalizationError, "member_count_invalid"):
                normalizer.normalize_sdist(archive, "1")
        with mock.patch.object(normalizer, "_MAX_MEMBER_BYTES", 3):
            with self.assertRaisesRegex(normalizer.SdistNormalizationError, "member_size_invalid"):
                normalizer.normalize_sdist(archive, "1")

    def test_directory_requires_exactly_one_regular_sdist(self):
        distribution = self.root / "dist"
        distribution.mkdir()
        with self.assertRaisesRegex(normalizer.SdistNormalizationError, "sdist_set_invalid"):
            normalizer.normalize_distribution_directory(distribution, "1")

        archive = distribution / "project-0.1.0.tar.gz"
        self._write_archive(archive)
        result = normalizer.normalize_distribution_directory(distribution, "1")
        self.assertEqual(result["archive"], archive.name)

        other = distribution / "other-0.1.0.tar.gz"
        self._write_archive(other)
        with self.assertRaisesRegex(normalizer.SdistNormalizationError, "sdist_set_invalid"):
            normalizer.normalize_distribution_directory(distribution, "1")

        other.unlink()
        archive.unlink()
        archive.symlink_to(self.root / "missing.tar.gz")
        with self.assertRaisesRegex(normalizer.SdistNormalizationError, "archive_not_regular"):
            normalizer.normalize_distribution_directory(distribution, "1")

    def test_cli_uses_environment_and_emits_no_path_on_failure(self):
        distribution = self.root / "sensitive-directory-name"
        distribution.mkdir()
        archive = distribution / "project-0.1.0.tar.gz"
        self._write_archive(archive)
        script = Path(__file__).parents[1] / "scripts" / "normalize_sdist.py"
        environment = dict(os.environ)
        environment["SOURCE_DATE_EPOCH"] = "1700000000"
        completed = subprocess.run(
            [sys.executable, str(script), "--distribution-directory", str(distribution)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["sourceDateEpoch"], 1700000000)

        archive.write_bytes(b"not gzip")
        failed = subprocess.run(
            [sys.executable, str(script), "--distribution-directory", str(distribution)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stderr, "normalize_sdist: archive_invalid\n")
        self.assertNotIn(str(distribution), failed.stderr)


if __name__ == "__main__":
    unittest.main()
