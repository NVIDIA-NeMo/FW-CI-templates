# Copyright (c) 2026, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The Guardwords scanner reports locations without printing matched values."""

import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import check_guardwords


class GuardwordsScannerTests(unittest.TestCase):
    def test_reports_only_file_and_line_for_added_match(self) -> None:
        diff = """diff --git example.py example.py
--- example.py
+++ example.py
@@ -1,0 +2 @@
+value = 'sensitive-marker'
"""

        self.assertEqual(
            check_guardwords.find_added_matches(
                diff, ["sensitive-marker"], case_sensitive=False
            ),
            ["example.py:2"],
        )

    def test_matches_added_lines_starting_with_plus_signs(self) -> None:
        diff = """diff --git example.py example.py
--- example.py
+++ example.py
@@ -0,0 +1,2 @@
+++sensitive-marker
+sensitive-marker
"""

        self.assertEqual(
            check_guardwords.find_added_matches(
                diff, ["sensitive-marker"], case_sensitive=False
            ),
            ["example.py:1", "example.py:2"],
        )

    def test_marker_in_file_header_is_not_a_match(self) -> None:
        diff = """diff --git sensitive-marker.txt sensitive-marker.txt
--- /dev/null
+++ sensitive-marker.txt
@@ -0,0 +1 @@
+ordinary content
"""

        self.assertEqual(
            check_guardwords.find_added_matches(
                diff, ["sensitive-marker"], case_sensitive=False
            ),
            [],
        )

    def test_preserves_non_lf_separators_inside_added_records(self) -> None:
        for separator in ("\u2028", "\u0085", "\r"):
            with self.subTest(separator=repr(separator)):
                diff = (
                    "diff --git example.py example.py\n"
                    "--- example.py\n"
                    "+++ example.py\n"
                    "@@ -0,0 +1,2 @@\n"
                    f"+prefix{separator}sensitive-marker\n"
                    "+sensitive-marker\n"
                )
                self.assertEqual(
                    check_guardwords.find_added_matches(
                        diff, ["sensitive-marker"], case_sensitive=False
                    ),
                    ["example.py:1", "example.py:2"],
                )

    def test_matches_crlf_added_record_and_ignores_deleted_record(self) -> None:
        diff = (
            "diff --git example.py example.py\n"
            "--- example.py\n"
            "+++ example.py\n"
            "@@ -1 +1 @@\n"
            "-prefix\u2028sensitive-marker\n"
            "+sensitive-marker\r\n"
        )

        self.assertEqual(
            check_guardwords.find_added_matches(
                diff, ["sensitive-marker"], case_sensitive=False
            ),
            ["example.py:1"],
        )

    def test_ignores_deleted_match(self) -> None:
        diff = """diff --git example.py example.py
--- example.py
+++ example.py
@@ -1 +0,0 @@
-sensitive-marker
"""

        self.assertEqual(
            check_guardwords.find_added_matches(
                diff, ["sensitive-marker"], case_sensitive=False
            ),
            [],
        )

    def test_matches_case_insensitively(self) -> None:
        diff = """diff --git example.py example.py
--- example.py
+++ example.py
@@ -0,0 +1 @@
+VALUE = 'Sensitive-Marker'
"""

        self.assertEqual(
            check_guardwords.find_added_matches(
                diff, ["sensitive-marker"], case_sensitive=False
            ),
            ["example.py:1"],
        )

    def test_cli_output_does_not_include_matched_value(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            patterns_file = Path(temporary_directory) / "guardwords.yaml"
            patterns_file.write_text(
                "version: 1\nmatching:\n  mode: literal\n  case_sensitive: false\n"
                "patterns:\n  - id: synthetic\n    category: test\n    pattern: sensitive-marker\n",
                encoding="utf-8",
            )
            diff = """diff --git example.py example.py
--- example.py
+++ example.py
@@ -1,0 +2 @@
+value = 'sensitive-marker'
"""
            with (
                patch.object(check_guardwords, "_git_diff", return_value=diff),
                patch.object(
                    sys,
                    "argv",
                    [
                        "check_guardwords.py",
                        "--patterns",
                        str(patterns_file),
                        "--base",
                        "base",
                        "--head",
                        "head",
                    ],
                ),
            ):
                from contextlib import redirect_stderr, redirect_stdout
                from io import StringIO

                stdout = StringIO()
                stderr = StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = check_guardwords.main()

            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout.getvalue(), "example.py:2\n")
            self.assertNotIn("sensitive-marker", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

    def test_private_catalog_fetch_skips_when_access_is_unavailable(self) -> None:
        denied = subprocess.CompletedProcess(
            args=["git", "clone"],
            returncode=128,
            stdout="",
            stderr="",
        )
        with patch.object(check_guardwords.subprocess, "run", return_value=denied):
            self.assertIsNone(check_guardwords._private_patterns())

    def test_staged_mode_skips_when_private_catalog_is_unavailable(self) -> None:
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        with (
            patch.object(check_guardwords, "_private_patterns", return_value=None),
            patch.object(
                sys,
                "argv",
                ["check_guardwords.py", "--staged", "--skip-if-unavailable"],
            ),
        ):
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = check_guardwords.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            stdout.getvalue(),
            "Guardwords check skipped: private catalog unavailable.\n",
        )
        self.assertEqual(stderr.getvalue(), "")

    def test_staged_mode_fails_with_location_only(self) -> None:
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        diff = """diff --git example.py example.py
--- example.py
+++ example.py
@@ -1,0 +2 @@
+value = 'sensitive-marker'
"""
        with (
            patch.object(
                check_guardwords,
                "_private_patterns",
                return_value=(["sensitive-marker"], False),
            ),
            patch.object(check_guardwords, "_staged_git_diff", return_value=diff),
            patch.object(
                sys,
                "argv",
                ["check_guardwords.py", "--staged", "--skip-if-unavailable"],
            ),
        ):
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = check_guardwords.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "example.py:2\n")
        self.assertNotIn("sensitive-marker", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_diff_helpers_force_text_and_disable_textconv(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            self._git(repository, "init", "--quiet")
            self._git(repository, "config", "user.name", "Guardwords Test")
            self._git(
                repository, "config", "user.email", "guardwords-test@example.invalid"
            )
            self._git(repository, "config", "commit.gpgsign", "false")
            (repository / ".gitattributes").write_text(
                "hidden.txt -diff\ntextconv.txt diff=mask\n", encoding="utf-8"
            )
            (repository / "hidden.txt").write_text("ordinary\n", encoding="utf-8")
            (repository / "textconv.txt").write_text("ordinary\n", encoding="utf-8")
            (repository / "removed.txt").write_text(
                "sensitive-marker\n", encoding="utf-8"
            )
            textconv = repository / "mask-textconv.sh"
            textconv.write_text(
                "#!/bin/sh\nsed 's/sensitive-marker/ordinary/' \"$1\"\n",
                encoding="utf-8",
            )
            textconv.chmod(0o755)
            self._git(repository, "config", "diff.mask.textconv", str(textconv))
            self._git(repository, "add", ".")
            self._git(repository, "commit", "--quiet", "-m", "base")
            base = self._git(repository, "rev-parse", "HEAD").stdout.strip()

            (repository / "hidden.txt").write_text(
                "sensitive-marker\n", encoding="utf-8"
            )
            (repository / "textconv.txt").write_text(
                "sensitive-marker\n", encoding="utf-8"
            )
            (repository / "removed.txt").unlink()
            (repository / "clean-binary.bin").write_bytes(b"\x00ordinary\n")
            self._git(repository, "add", "-A")

            previous_directory = Path.cwd()
            try:
                os.chdir(repository)
                staged_diff = check_guardwords._staged_git_diff()
                self.assertEqual(
                    check_guardwords.find_added_matches(
                        staged_diff, ["sensitive-marker"], case_sensitive=False
                    ),
                    ["hidden.txt:1", "textconv.txt:1"],
                )
                self._git(repository, "commit", "--quiet", "-m", "text additions")
                head = self._git(repository, "rev-parse", "HEAD").stdout.strip()
                committed_diff = check_guardwords._git_diff(base, head)
            finally:
                os.chdir(previous_directory)

            self.assertEqual(
                check_guardwords.find_added_matches(
                    committed_diff, ["sensitive-marker"], case_sensitive=False
                ),
                ["hidden.txt:1", "textconv.txt:1"],
            )

    def test_git_helpers_preserve_embedded_carriage_returns(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            repository = workspace / "repo"
            repository.mkdir()
            self._git(repository, "init", "--quiet")
            self._git(repository, "config", "user.name", "Guardwords Test")
            self._git(
                repository, "config", "user.email", "guardwords-test@example.invalid"
            )
            self._git(repository, "config", "commit.gpgsign", "false")
            (repository / "example.py").write_bytes(b"")
            (repository / "unicode.py").write_bytes(b"")
            (repository / "crlf.py").write_bytes(b"ordinary\r\n")
            self._git(repository, "add", ".")
            self._git(repository, "commit", "--quiet", "-m", "base")
            base = self._git(repository, "rev-parse", "HEAD").stdout.strip()

            (repository / "example.py").write_bytes(
                b"prefix\rsensitive-marker\nsensitive-marker\n"
            )
            (repository / "unicode.py").write_bytes(
                b"prefix\xe2\x80\xa8sensitive-marker\nprefix\xc2\x85sensitive-marker\n"
            )
            (repository / "crlf.py").write_bytes(b"ordinary\r\nsensitive-marker\r\n")
            self._git(repository, "add", "-A")

            expected = [
                "crlf.py:2",
                "example.py:1",
                "example.py:2",
                "unicode.py:1",
                "unicode.py:2",
            ]
            patterns_file = workspace / "guardwords.yaml"
            patterns_file.write_text(
                "matching:\n  mode: literal\n  case_sensitive: false\n"
                "patterns:\n  - id: synthetic\n    category: test\n    pattern: sensitive-marker\n",
                encoding="utf-8",
            )
            previous_directory = Path.cwd()
            try:
                os.chdir(repository)
                staged_diff = check_guardwords._staged_git_diff()
                self.assertEqual(
                    check_guardwords.find_added_matches(
                        staged_diff, ["sensitive-marker"], case_sensitive=False
                    ),
                    expected,
                )
                staged_stdout = StringIO()
                staged_stderr = StringIO()
                with (
                    patch.object(
                        check_guardwords,
                        "_private_patterns",
                        return_value=(["sensitive-marker"], False),
                    ),
                    patch.object(
                        sys,
                        "argv",
                        ["check_guardwords.py", "--staged", "--skip-if-unavailable"],
                    ),
                    redirect_stdout(staged_stdout),
                    redirect_stderr(staged_stderr),
                ):
                    staged_exit = check_guardwords.main()
                self.assertEqual(staged_exit, 1)
                self.assertEqual(staged_stdout.getvalue(), "\n".join(expected) + "\n")
                self.assertEqual(staged_stderr.getvalue(), "")

                self._git(repository, "commit", "--quiet", "-m", "embedded separators")
                head = self._git(repository, "rev-parse", "HEAD").stdout.strip()
                committed_diff = check_guardwords._git_diff(base, head)
                self.assertEqual(
                    check_guardwords.find_added_matches(
                        committed_diff, ["sensitive-marker"], case_sensitive=False
                    ),
                    expected,
                )

                committed_stdout = StringIO()
                committed_stderr = StringIO()
                with (
                    patch.object(
                        sys,
                        "argv",
                        [
                            "check_guardwords.py",
                            "--patterns",
                            str(patterns_file),
                            "--base",
                            base,
                            "--head",
                            head,
                        ],
                    ),
                    redirect_stdout(committed_stdout),
                    redirect_stderr(committed_stderr),
                ):
                    committed_exit = check_guardwords.main()
            finally:
                os.chdir(previous_directory)

            self.assertEqual(committed_exit, 1)
            self.assertEqual(committed_stdout.getvalue(), "\n".join(expected) + "\n")
            self.assertNotIn("sensitive-marker", committed_stdout.getvalue())
            self.assertEqual(committed_stderr.getvalue(), "")

    def test_isolated_pip_and_scanner_ignore_pr_controlled_imports(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            repository = workspace / "repo"
            repository.mkdir()
            pip_package = repository / "pip"
            pip_package.mkdir()
            marker = repository / "pip-payload-ran"
            (pip_package / "__init__.py").write_text("", encoding="utf-8")
            (pip_package / "__main__.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
                encoding="utf-8",
            )
            (repository / "yaml.py").write_text(
                "raise RuntimeError('PR-controlled yaml import was executed')\n",
                encoding="utf-8",
            )

            pip_result = subprocess.run(
                [sys.executable, "-I", "-m", "pip", "--version"],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(pip_result.returncode, 0, pip_result.stderr)
            self.assertFalse(marker.exists())

            self._git(repository, "init", "--quiet")
            self._git(repository, "config", "user.name", "Guardwords Test")
            self._git(
                repository, "config", "user.email", "guardwords-test@example.invalid"
            )
            self._git(repository, "config", "commit.gpgsign", "false")
            (repository / "example.py").write_text(
                "ordinary\nordinary\n", encoding="utf-8"
            )
            self._git(repository, "add", "example.py")
            self._git(repository, "commit", "--quiet", "-m", "base")
            base = self._git(repository, "rev-parse", "HEAD").stdout.strip()
            (repository / "example.py").write_text(
                "ordinary\nsensitive-marker\n", encoding="utf-8"
            )
            self._git(repository, "add", "example.py")
            self._git(repository, "commit", "--quiet", "-m", "match")
            head = self._git(repository, "rev-parse", "HEAD").stdout.strip()
            patterns_file = workspace / "guardwords.yaml"
            patterns_file.write_text(
                "matching:\n  mode: literal\n  case_sensitive: false\n"
                "patterns:\n  - id: synthetic\n    category: test\n    pattern: sensitive-marker\n",
                encoding="utf-8",
            )

            command = [
                sys.executable,
                "-I",
                str(Path(check_guardwords.__file__).resolve()),
                "--patterns",
                str(patterns_file),
            ]
            result = subprocess.run(
                [*command, "--base", base, "--head", head],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(result.stdout, "example.py:2\n")
            self.assertNotIn("sensitive-marker", result.stdout)
            self.assertEqual(result.stderr, "")
            self.assertFalse(marker.exists())

            (repository / "clean.py").write_text(
                "ordinary addition\n", encoding="utf-8"
            )
            self._git(repository, "add", "clean.py")
            self._git(repository, "commit", "--quiet", "-m", "clean addition")
            clean_head = self._git(repository, "rev-parse", "HEAD").stdout.strip()
            clean_result = subprocess.run(
                [*command, "--base", head, "--head", clean_head],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(clean_result.returncode, 0, clean_result.stderr)
            self.assertEqual(clean_result.stdout, "")

            patterns_file.write_text("not: [valid", encoding="utf-8")
            invalid_result = subprocess.run(
                [*command, "--base", base, "--head", head],
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(invalid_result.returncode, 2)
            self.assertEqual(invalid_result.stdout, "")
            self.assertEqual(invalid_result.stderr, "")
            self.assertFalse(marker.exists())

    @staticmethod
    def _git(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repository), *args],
            check=True,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
