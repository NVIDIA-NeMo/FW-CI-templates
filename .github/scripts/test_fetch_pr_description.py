# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""PR body fetches fail safely and never expose response or token values."""

import io
import json
import os
import sys
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import fetch_pr_description


class FetchDescriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.output = Path(self.workspace.name) / "description.txt"
        self.head = "a" * 40
        self.metadata = {
            "number": 42,
            "base": {"repo": {"full_name": "example/repo"}},
            "head": {"sha": self.head},
            "body": "sensitive-marker\n$(malicious-command)\n",
        }

    def _run(
        self, *, error: Exception | None = None, token: str = "synthetic-token"
    ) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, {"GH_TOKEN": token}),
            patch.object(
                sys,
                "argv",
                [
                    "fetch_pr_description.py",
                    "--repository",
                    "example/repo",
                    "--number",
                    "42",
                    "--head",
                    self.head,
                    "--output",
                    str(self.output),
                ],
            ),
            patch.object(
                fetch_pr_description.urllib.request,
                "urlopen",
                side_effect=error,
                return_value=io.BytesIO(json.dumps(self.metadata).encode()),
            ) as fetch,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = fetch_pr_description.main()
        if error is None and token:
            request = fetch.call_args.args[0]
            self.assertEqual(
                request.full_url, "https://api.github.com/repos/example/repo/pulls/42"
            )
            self.assertEqual(
                request.get_header("Authorization"), "Bearer synthetic-token"
            )
            self.assertEqual(fetch.call_args.kwargs["timeout"], 20)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_fetches_exact_description_to_file_without_output(self) -> None:
        self.assertEqual(self._run(), (0, "", ""))
        self.assertEqual(self.output.read_text(), self.metadata["body"])

    def test_null_description_writes_empty_file(self) -> None:
        self.metadata["body"] = None
        self.assertEqual(self._run(), (0, "", ""))
        self.assertEqual(self.output.read_text(), "")

    def test_metadata_mismatches_fail_without_writing_body(self) -> None:
        for metadata in [
            {**self.metadata, "head": {"sha": "b" * 40}},
            {**self.metadata, "number": 43},
            {**self.metadata, "base": {"repo": {"full_name": "other/repo"}}},
            {**self.metadata, "body": {"unexpected": "sensitive-marker"}},
        ]:
            with self.subTest(metadata=metadata):
                self.metadata = metadata
                code, stdout, stderr = self._run()
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    stderr,
                    "Guardwords check failed: PR description unavailable or metadata mismatch.\n",
                )
                self.assertFalse(self.output.exists())

    def test_fetch_errors_do_not_expose_diagnostics_or_token(self) -> None:
        for error in [
            urllib.error.HTTPError(
                "sensitive-marker", 403, "synthetic-token", {}, None
            ),
            urllib.error.URLError("sensitive-marker synthetic-token"),
            TimeoutError("sensitive-marker synthetic-token"),
            ValueError("sensitive-marker synthetic-token"),
        ]:
            with self.subTest(error=type(error).__name__):
                code, stdout, stderr = self._run(error=error)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertNotIn("sensitive-marker", stderr)
                self.assertNotIn("synthetic-token", stderr)
                self.assertFalse(self.output.exists())

    def test_missing_token_fails_without_fetching(self) -> None:
        code, stdout, stderr = self._run(token="")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertNotIn("sensitive-marker", stderr)
        self.assertFalse(self.output.exists())

    def test_invalid_identifiers_do_not_make_requests(self) -> None:
        with patch.object(fetch_pr_description.urllib.request, "urlopen") as fetch:
            for repository, number, head in [
                ("example/repo/../../secrets", 42, self.head),
                ("example/repo", 0, self.head),
                ("example/repo", 42, "$(malicious-command)"),
            ]:
                with self.assertRaises(ValueError):
                    fetch_pr_description.fetch_description(
                        repository=repository,
                        number=number,
                        head=head,
                        output=self.output,
                    )
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
