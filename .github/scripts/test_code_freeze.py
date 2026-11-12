# Copyright (c) 2026, NVIDIA CORPORATION. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Execute freeze workflow shells against disposable Git repositories."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.load(
    (ROOT / ".github/workflows/_code_freeze.yml").read_text(), Loader=yaml.BaseLoader
)


def step(job, name):
    return next(item for item in WORKFLOW["jobs"][job]["steps"] if item["name"] == name)


class CodeFreezeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def shell(self, script, env, cwd=None):
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", script],
            cwd=cwd or self.root,
            env={**os.environ, **env},
            text=True,
            capture_output=True,
        )

    def test_app_authentication_never_falls_back_to_legacy_credentials(self):
        cases = [
            ("3610216", "test-key", "test-app-token", True, "test-app-token"),
            ("1605575", "test-key", "test-app-token", True, "test-app-token"),
            ("3610216", "", "test-app-token", False, ""),
            ("3610216", "test-key", "", False, ""),
            ("", "test-key", "", False, ""),
            ("", "test-key", "test-app-token", False, ""),
            ("", "", "", True, "test-legacy-token"),
        ]
        for job in ["create-release-branch", "bump-next-version"]:
            for app_id, key, app_token, succeeds, expected in cases:
                with self.subTest(job=job, app_id=app_id, key=bool(key), token=bool(app_token)):
                    output = self.root / "outputs"
                    output.write_text("")
                    result = self.shell(
                        step(job, "Select GitHub token")["run"],
                        {
                            "APP_ID": app_id,
                            "BOT_KEY": key,
                            "APP_TOKEN": app_token,
                            "LEGACY_TOKEN": "test-legacy-token",
                            "GITHUB_OUTPUT": str(output),
                        },
                    )
                    self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                    self.assertEqual(output.read_text(), f"token={expected}\n" if succeeds else "")
                    self.assertNotIn("test-key", result.stdout + result.stderr)
                    self.assertNotIn("test-legacy-token", result.stdout if app_id or key else "")

    def fixture(self):
        remote = self.root / "remote.git"
        checkout = self.root / "123"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
        package = checkout / "package"
        package.mkdir()
        (package / "package_info.py").write_text(
            "MAJOR = 1\nMINOR = 2\nPATCH = 3\nPRE_RELEASE = 'rc0'\nDEV = 'dev0'\n"
            "__version__ = '1.2.3rc0.dev0'\n"
        )
        for args in [
            ["config", "user.name", "Freeze test"],
            ["config", "user.email", "freeze-test@example.invalid"],
            ["config", "commit.gpgsign", "false"],
            ["add", "."],
            ["commit", "-m", "fixture"],
            ["remote", "add", "origin", str(remote)],
            ["push", "-u", "origin", "main"],
        ]:
            subprocess.run(["git", "-C", str(checkout), *args], check=True, capture_output=True)
        return checkout, remote

    def test_release_branch_push_is_suppressed_in_dry_run(self):
        for dry_run in ["true", "false"]:
            with self.subTest(dry_run=dry_run), tempfile.TemporaryDirectory(dir=self.root) as work:
                previous_root, self.root = self.root, Path(work)
                try:
                    _, remote = self.fixture()
                    output = self.root / "outputs"
                    script = step("create-release-branch", "Get release branch ref")["run"]
                    script = script.replace("${{ github.run_id }}", "123")
                    result = self.shell(script, {
                        "DRY_RUN": dry_run, "SRC_DIR": "", "PYPROJECT_NAME": "package",
                        "RELEASE_BRANCH_PREFIX": "core_", "GITHUB_OUTPUT": str(output),
                    })
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(output.read_text(), "version=core_r1.2.3\n")
                    refs = subprocess.run(
                        ["git", "--git-dir", str(remote), "for-each-ref", "--format=%(refname)"],
                        check=True, capture_output=True, text=True,
                    ).stdout.splitlines()
                    self.assertEqual("refs/heads/core_r1.2.3" in refs, dry_run == "false")
                finally:
                    self.root = previous_root

    def test_version_bump_computes_setuptools_and_hatch_without_pushing(self):
        for packaging, release_type, prerelease, dev, expected in [
            ("setuptools", "minor", "rc0", "dev0", "1.3.0rc0.dev0"),
            ("setuptools", "major", "", "", "2.0.0"),
            ("hatch", "minor", "", "", "1.3.0"),
        ]:
            with self.subTest(packaging=packaging, release_type=release_type):
                with tempfile.TemporaryDirectory(dir=self.root) as work:
                    previous_root, self.root = self.root, Path(work)
                    try:
                        checkout, remote = self.fixture()
                        output = self.root / "outputs"
                        before = subprocess.run(
                            ["git", "--git-dir", str(remote), "rev-parse", "main"],
                            check=True, capture_output=True, text=True,
                        ).stdout
                        script = step("bump-next-version", "Bump version")["run"]
                        script = script.replace("${{ github.run_id }}", "123")
                        script = script.replace("${{ inputs.release-type }}", release_type)
                        result = self.shell(script, {
                            "SRC_DIR": "", "PYPROJECT_NAME": "package", "PACKAGING": packaging,
                            "NEXT_PRERELEASE": prerelease, "NEXT_DEV": dev,
                            "GITHUB_OUTPUT": str(output),
                        })
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(output.read_text(), f"version={expected}\n")
                        info = (checkout / "package/package_info.py").read_text()
                        if packaging == "hatch":
                            self.assertIn(f"__version__ = '{expected}'", info)
                        else:
                            values = {}
                            exec(info, values)
                            computed = f"{values['MAJOR']}.{values['MINOR']}.{values['PATCH']}{values['PRE_RELEASE']}"
                            if values["DEV"]:
                                computed += "." + values["DEV"]
                            self.assertEqual(computed, expected)
                        after = subprocess.run(
                            ["git", "--git-dir", str(remote), "rev-parse", "main"],
                            check=True, capture_output=True, text=True,
                        ).stdout
                        self.assertEqual(before, after)
                    finally:
                        self.root = previous_root

    def test_tokens_stay_in_their_job_and_cover_every_writer(self):
        token = "${{ steps.github-token.outputs.token }}"
        for job in ["create-release-branch", "bump-next-version"]:
            minted = step(job, "Generate GitHub App token")
            self.assertEqual(minted["if"], "inputs.app-id != ''")
            self.assertNotIn("owner", minted["with"])
            self.assertNotIn("repositories", minted["with"])
            self.assertEqual(minted["with"]["permission-contents"], "write")
            self.assertEqual(minted["with"]["permission-pull-requests"], "write")
            self.assertNotIn("permission-issues", minted["with"])
            self.assertEqual(step(job, "Checkout repository")["with"]["token"], token)
            self.assertNotIn("token", WORKFLOW["jobs"][job].get("outputs", {}))
        label = step("create-release-branch", "Create cherry-pick label")
        self.assertEqual(label["env"]["GH_TOKEN"], token)
        self.assertEqual(label["if"], "${{ inputs.dry-run != true }}")
        checkout = step("bump-next-version", "Checkout repository")
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        pull = step("bump-next-version", "Create Version Bump PR")
        self.assertEqual(pull["if"], "${{ inputs.dry-run != true }}")
        self.assertEqual(pull["with"]["token"],
                         "${{ inputs.app-id != '' && steps.github-token.outputs.token || github.token }}")


if __name__ == "__main__":
    unittest.main()
