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

import sys
import unittest
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
            check_guardwords.find_added_matches(diff, ["sensitive-marker"], case_sensitive=False),
            ["example.py:2"],
        )

    def test_ignores_deleted_match(self) -> None:
        diff = """diff --git example.py example.py
--- example.py
+++ example.py
@@ -1 +0,0 @@
-sensitive-marker
"""

        self.assertEqual(
            check_guardwords.find_added_matches(diff, ["sensitive-marker"], case_sensitive=False),
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
            check_guardwords.find_added_matches(diff, ["sensitive-marker"], case_sensitive=False),
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
            with patch.object(check_guardwords, "_git_diff", return_value=diff), patch.object(
                sys,
                "argv",
                ["check_guardwords.py", "--patterns", str(patterns_file), "--base", "base", "--head", "head"],
            ):
                from contextlib import redirect_stderr, redirect_stdout
                from io import StringIO

                stdout = StringIO()
                stderr = StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = check_guardwords.main()

            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout.getvalue(), "example.py:2\n")
            self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
