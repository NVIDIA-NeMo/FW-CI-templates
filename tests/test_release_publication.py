# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Run GitHub release publication and retry against a stateful offline API."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


TESTS = Path(__file__).resolve().parent
WORKFLOW = TESTS.parent / ".github/workflows/_release_finalize.yml"
TAG = "gym/v1.2.3rc1"
READ_RELEASE = "GET releases/tags/gym%2Fv1.2.3rc1"
READ_COMMIT = "GET commits/refs%2Ftags%2Fgym%2Fv1.2.3rc1"


class ReleasePublicationTests(unittest.TestCase):
    """Verify the exact workflow step, including reads after a duplicate creation."""

    def setUp(self):
        """Prepare a checked-out commit, a nested package directory, and the local gh fake."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        checkout = self.root / "run"
        package = checkout / "package"
        package.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", checkout], check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.test",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--quiet",
                "--allow-empty",
                "-m",
                "release",
            ],
            cwd=checkout,
            check=True,
        )
        self.sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=checkout, text=True
        ).strip()
        self.payload = {
            "tag_name": TAG,
            "target_commitish": self.sha,
            "draft": False,
            "prerelease": True,
            "name": "Gym candidate",
            "body": "Release notes",
        }
        (package / "payload.txt").write_text(json.dumps(self.payload))
        gh = self.root / "gh"
        gh.write_text(
            f"#!{sys.executable}\n" + (TESTS / "fixtures/gh_release_api.py").read_text()
        )
        gh.chmod(0o755)
        self.jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
        self.script = next(
            step["run"]
            for step in self.jobs["create-gh-release"]["steps"]
            if step.get("name") == "Publish GitHub release"
        )
        self.state_file = self.root / "state.json"
        self.initial = {
            "tag": TAG,
            "tag_sha": self.sha,
            "calls": [],
            "releases": [],
            "errors": {},
            "lose_first_response": False,
        }

    def publish(self):
        """Run the publish shell with the test GitHub CLI first on PATH."""
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", self.script],
            env=dict(
                os.environ,
                GITHUB_REPOSITORY="org/gym",
                GITHUB_WORKSPACE=str(self.root),
                GITHUB_RUN_ID="run",
                ROOT_DIR="package",
                RUNNER_TEMP=str(self.root),
                RELEASE_API_STATE=str(self.state_file),
                RELEASE_SHA=self.sha,
                GH_TOKEN="offline",
                PATH=str(self.root) + os.pathsep + os.environ["PATH"],
            ),
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def check_downstream(self, publish_result):
        """Evaluate the downstream job conditions with docs enabled and publication complete."""
        results = {"create-gh-release": publish_result.returncode == 0}
        for name in ("build-docs", "publish-docs", "notify"):
            job = self.jobs[name]
            self.assertIn("create-gh-release", job["needs"])
            success = all(results[need] for need in job["needs"])
            condition = " ".join(job["if"].split())
            for source, value in (
                ("success()", str(success)),
                ("failure()", str(not success)),
                ("cancelled()", "False"),
                ("inputs.publish-docs == true", "True"),
                ("!", " not "),
                ("&&", " and "),
                ("||", " or "),
            ):
                condition = condition.replace(source, value)
            results[name] = eval(condition.strip(), {"__builtins__": {}}, {})
            self.assertTrue(results[name], name)

    def test_normal_and_lost_response_retries_preserve_the_release(self):
        """Both retries reuse the one published release and unblock its dependent jobs."""
        for lost in (False, True):
            with self.subTest(lost_response=lost):
                self.state_file.write_text(
                    json.dumps(self.initial | {"lose_first_response": lost})
                )
                first = self.publish()
                self.assertEqual(first.returncode == 0, not lost, first.stderr)
                state = json.loads(self.state_file.read_text())
                self.assertEqual(len(state["releases"]), 1)
                release = state["releases"][0]
                release.update(
                    name="Curated title",
                    body="Notes after publication",
                    target_commitish="main",
                )
                self.state_file.write_text(json.dumps(state))
                retry = self.publish()
                self.assertEqual(retry.returncode, 0, retry.stderr)
                done = json.loads(self.state_file.read_text())
                self.assertEqual(done["releases"], [release])
                self.assertEqual(done["calls"][:2], ["POST releases", "POST releases"])
                self.assertCountEqual(done["calls"][2:], [READ_RELEASE, READ_COMMIT])
                self.check_downstream(retry)

    def test_conflicts_and_api_errors_fail_closed(self):
        """A stored release cannot mask an unrelated validation, authorization, or read failure."""
        duplicate = {
            "resource": "Release",
            "field": "tag_name",
            "code": "already_exists",
        }
        invalid = {"resource": "Release", "field": "name", "code": "invalid"}
        mixed = {
            "status": "422",
            "message": "Validation Failed",
            "errors": [duplicate, invalid],
        }
        denied = {"status": "403", "message": "Resource not accessible by integration"}
        cases = (
            ("wrong SHA", {}, {"tag_sha": "0" * 40}, {}),
            ("wrong tag", {"tag_name": "different"}, {}, {}),
            ("draft", {"draft": True}, {}, {}),
            ("different prerelease state", {"prerelease": False}, {}, {}),
            ("mixed validation errors", {}, {}, {"POST releases": mixed}),
            ("access denied", {}, {}, {"POST releases": denied}),
            ("release read denied", {}, {}, {READ_RELEASE: denied}),
            ("commit read denied", {}, {}, {READ_COMMIT: denied}),
        )
        for name, changes, overrides, errors in cases:
            with self.subTest(case=name):
                release = (
                    self.payload
                    | {"id": 101, "html_url": "https://example.test/release/101"}
                    | changes
                )
                before = (
                    self.initial | overrides | {"releases": [release], "errors": errors}
                )
                self.state_file.write_text(json.dumps(before))
                self.assertNotEqual(self.publish().returncode, 0)
                after = json.loads(self.state_file.read_text())
                self.assertEqual(after["releases"], [release])
                if "POST releases" in errors:
                    self.assertEqual(after["calls"], ["POST releases"])
