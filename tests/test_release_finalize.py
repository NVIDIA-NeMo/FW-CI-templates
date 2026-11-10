# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Exercise release payload construction without publishing (requires PyYAML and jq)."""

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/_release_finalize.yml"


class ReleasePayloadTests(unittest.TestCase):
    def build_payload(
        self, *, generated: str | None = None, legacy: str | None = None, version: str = "1.2.3"
    ) -> dict[str, object]:
        job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["create-gh-release"]
        build = next(step for step in job["steps"] if step.get("id") == "build-changelog")
        create = next(step for step in job["steps"] if step.get("id") == "version-number")
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            checkout = workspace / "run"
            checkout.mkdir()
            temporary_files = workspace / "temporary"
            temporary_files.mkdir()
            expressions = {
                "github.workspace": str(workspace),
                "github.run_id": "run",
                "inputs.root-dir": ".",
                "inputs.release-ref": "a" * 40,
                "steps.app-token.outputs.token": "test-token",
                "inputs.validate-only || inputs.dry-run": "true",
                "steps.build-changelog.outputs.changelog": generated or "",
            }

            def render(value: str) -> str:
                return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda match: expressions[match[1]], value)

            if generated is not None and "outputFile" in build["with"]:
                (workspace / render(build["with"]["outputFile"])).write_text(generated)
            if legacy is not None:
                (checkout / "CHANGELOG.md").write_text(
                    f"## NVIDIA Test {version}\n\n{legacy}\n\n## Older release\nOld notes\n"
                )
            # Even if the script regresses, it cannot contact GitHub from this test.
            commands = workspace / "bin"
            commands.mkdir()
            curl = commands / "curl"
            curl.write_text('#!/bin/sh\ntouch "$PUBLISH_MARKER"\nexit 99\n')
            curl.chmod(0o755)
            script = workspace / "create-release.sh"
            script.write_text(render(create["run"]))
            env = dict(
                os.environ,
                REPOSITORY="example/test",
                PROJECT_NAME="Test",
                VERSION=version,
                TAG_PREFIX="test-",
                GITHUB_OUTPUT=str(workspace / "outputs"),
                TMPDIR=str(temporary_files),
                PUBLISH_MARKER=str(workspace / "published"),
                PATH=str(commands) + os.pathsep + os.environ["PATH"],
                **{key: render(value) for key, value in create["env"].items()},
            )
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-euo", "pipefail", str(script)],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertFalse((workspace / "published").exists())
            self.assertFalse((checkout / "unexpected-command").exists())
            self.assertEqual(list(temporary_files.iterdir()), [])
            payload = json.loads((checkout / "payload.txt").read_text())
            self.assertEqual(payload["tag_name"], f"test-v{version}")
            self.assertEqual(payload["target_commitish"], "a" * 40)
            self.assertFalse(payload["draft"])
            self.assertEqual(
                (workspace / "outputs").read_text(),
                f"is-release-candidate={'true' if 'rc' in version else 'false'}\n",
            )
            return payload

    def test_large_generated_changelog_is_preserved(self) -> None:
        notes = "- Example change\n" * 16000 + "Quotes: \" ' $()\n$(touch unexpected-command)\n"
        payload = self.build_payload(generated=notes)
        self.assertEqual(payload["body"], notes)
        self.assertFalse(payload["prerelease"])

    def test_large_legacy_changelog_is_preserved(self) -> None:
        notes = "- Legacy change\n" * 16000
        payload = self.build_payload(legacy=notes)
        self.assertEqual(payload["body"], notes.rstrip("\n"))

    def test_empty_generated_changelog_uses_release_candidate_fallback(self) -> None:
        payload = self.build_payload(generated="", version="1.2.3rc1")
        self.assertRegex(payload["body"], r"^Prerelease: NVIDIA Test 1\.2\.3rc1 \(\d{4}-\d{2}-\d{2}\)$")
        self.assertTrue(payload["prerelease"])

    def test_missing_generated_changelog_uses_legacy_section(self) -> None:
        self.assertEqual(self.build_payload(legacy="Current notes")["body"], "Current notes")

    def test_missing_changelogs_use_release_fallback(self) -> None:
        self.assertEqual(self.build_payload()["body"], "Release: NVIDIA Test 1.2.3")

    def test_alpha_release_keeps_generated_notes(self) -> None:
        payload = self.build_payload(generated="Alpha notes\n", version="1.2.3a1")
        self.assertEqual(payload["body"], "Alpha notes\n")
        self.assertTrue(payload["prerelease"])


if __name__ == "__main__":
    unittest.main()
