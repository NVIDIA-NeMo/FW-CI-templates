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
        self,
        *,
        generated: str | None = None,
        legacy: str | None = None,
        version: str = "1.2.3",
        file_state: str = "present",
        builder_enabled: bool = True,
        expected_error: bool = False,
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
                "steps.build-changelog.outputs.changelog": (generated or "") if builder_enabled else "",
                "steps.build-changelog.outputs.changelog != ''": "true" if generated and builder_enabled else "false",
                "steps.changelog-file.outputs.path": "",
            }

            def render(value: str) -> str:
                return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda match: expressions[match[1]], value)

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
                RUNNER_TEMP=str(temporary_files),
                PUBLISH_MARKER=str(workspace / "published"),
                PATH=str(commands) + os.pathsep + os.environ["PATH"],
            )
            prepare = next((step for step in job["steps"] if step.get("id") == "changelog-file"), None)
            if builder_enabled and prepare is not None:
                prepared_output = workspace / "prepare-outputs"
                prepared = subprocess.run(
                    ["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", prepare["run"]],
                    cwd=workspace,
                    env=dict(env, GITHUB_OUTPUT=str(prepared_output)),
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(prepared.returncode, 0, prepared.stderr[-2000:])
                path = Path(prepared_output.read_text().strip().removeprefix("path="))
                self.assertTrue(path.is_relative_to(temporary_files))
                self.assertEqual(path.stat().st_size, 0)
                expressions["steps.changelog-file.outputs.path"] = str(path)
            built_path = (
                workspace / render(build["with"]["outputFile"])
                if builder_enabled
                else checkout / "release-changelog.md"
            )
            if builder_enabled and generated:
                built_path.write_text(generated)
            if file_state == "missing":
                built_path.unlink(missing_ok=True)
            elif file_state == "empty":
                built_path.write_text("")
            elif file_state == "stale":
                built_path.write_text("Unrelated prior notes\n")
            env.update({key: render(value) for key, value in create["env"].items()})
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-euo", "pipefail", str(script)],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertFalse((workspace / "published").exists())
            self.assertFalse((checkout / "unexpected-command").exists())
            self.assertEqual(list(temporary_files.iterdir()), [])
            if expected_error:
                self.assertEqual(result.returncode, 1)
                self.assertIn("The nonempty built changelog was not written to its file.", result.stdout)
                self.assertFalse((checkout / "payload.txt").exists())
                return {}
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
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

    def test_nonempty_generated_notes_require_a_present_nonempty_file(self) -> None:
        for state in ("missing", "empty"):
            with self.subTest(file_state=state):
                self.build_payload(generated="Expected current notes\n", file_state=state, expected_error=True)

    def test_empty_generated_notes_ignore_stale_file_contents(self) -> None:
        payload = self.build_payload(generated="", file_state="stale")
        self.assertEqual(payload["body"], "Release: NVIDIA Test 1.2.3")

    def test_disabled_builder_ignores_stale_checkout_notes(self) -> None:
        payload = self.build_payload(builder_enabled=False, file_state="stale")
        self.assertEqual(payload["body"], "Release: NVIDIA Test 1.2.3")

    def test_newline_only_generated_notes_are_preserved(self) -> None:
        payload = self.build_payload(generated="\n\n")
        self.assertEqual(payload["body"], "\n\n")


if __name__ == "__main__":
    unittest.main()
