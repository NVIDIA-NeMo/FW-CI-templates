# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Exercise the protected release workflows without contacting GitHub."""

import json
import os
import re
import subprocess
import unittest
from pathlib import Path

import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"


def expression(value, context):
    """Evaluate the release conditions' boolean and string operators."""
    source = value.strip().removeprefix("${{").removesuffix("}}").strip()
    source = re.sub(r"(?:inputs|env)\.[\w-]+", lambda m: repr(context[m[0]]), source)
    source = re.sub(
        r"!(?!=)", "not ", source.replace("&&", " and ").replace("||", " or ")
    ).strip()
    return eval(source, {"__builtins__": {}}, {})


class ReleaseIdentityTests(unittest.TestCase):
    """Keep App use and branch authorization within production publication."""

    @classmethod
    def setUpClass(cls):
        """Read the actual shared workflows."""
        cls.bump = yaml.safe_load((WORKFLOWS / "_release_bump.yml").read_text())[
            "jobs"
        ]["bump-next-version"]
        cls.finalize = yaml.safe_load(
            (WORKFLOWS / "_release_finalize.yml").read_text()
        )["jobs"]["create-gh-release"]

    def test_production_only_mints_environment_token(self):
        """Both rehearsal modes use the job token, while legacy callers retain their App."""
        for job in (self.bump, self.finalize):
            for environment in ("", "gym-release"):
                for validate, dry in (
                    (False, False),
                    (True, False),
                    (False, True),
                    (True, True),
                ):
                    context = {
                        "inputs.release-environment": environment,
                        "inputs.validate-only": validate,
                        "inputs.dry-run": dry,
                        "env.IS_INERT": str(validate or dry).lower(),
                    }
                    with self.subTest(
                        environment=environment, validate=validate, dry=dry
                    ):
                        mints = [
                            step
                            for step in job["steps"]
                            if "create-github-app-token" in step.get("uses", "")
                            and expression(step["if"], context)
                        ]
                        if not environment:
                            self.assertEqual(
                                [s["with"]["private-key"] for s in mints],
                                ["${{ secrets.BOT_KEY }}"],
                            )
                        elif validate or dry:
                            self.assertEqual(mints, [])
                            self.assertEqual(
                                expression(job["environment"]["name"], context),
                                "public",
                            )
                        else:
                            self.assertEqual(
                                expression(job["environment"]["name"], context),
                                environment,
                            )
                            self.assertEqual(
                                [s["with"]["private-key"] for s in mints],
                                ["${{ secrets.RELEASE_APP_PRIVATE_KEY }}"],
                            )
                            self.assertEqual(
                                mints[0]["with"]["app-id"], "${{ vars.RELEASE_APP_ID }}"
                            )
                            self.assertEqual(
                                mints[0]["with"]["permission-contents"], "write"
                            )

    def test_guard_accepts_only_authorized_source(self):
        """Run the protected bump guard with the same API responses as production."""
        step = next(
            s
            for s in self.bump["steps"]
            if s.get("name") == "Verify protected release source and destination"
        )
        mocks = """
gh() {
  case "$2" in
    graphql) [[ -n "$BRANCH_REPLY" ]] && printf '%s\n' "$BRANCH_REPLY" ;;
    "repos/org/repo/compare/$RELEASE_SHA...$TIP") [[ -n "$COMPARE_REPLY" ]] && printf '%s\n' "$COMPARE_REPLY" ;;
    *) return 1 ;;
  esac
}
"""
        pattern = "[rv][0-9].[0-9].[0-9]"
        rule = dict(
            pattern=pattern,
            restrictsPushes=True,
            blocksCreations=True,
            requiresApprovingReviews=True,
            requiresCodeOwnerReviews=True,
        )
        tip = "b" * 40
        for target, changes, status, event_ref, accepted in (
            ("r1.2.3", {}, "ahead", "main", True),
            ("r1.2.3", {}, "identical", "main", True),
            (
                "main",
                {"pattern": "main", "restrictsPushes": False},
                "ahead",
                "main",
                True,
            ),
            ("r1.2.3", {"pattern": "*"}, "ahead", "main", False),
            ("r1.2.3", {"restrictsPushes": False}, "ahead", "main", False),
            ("r1.2.3", {"blocksCreations": False}, "ahead", "main", False),
            (
                "main",
                {"pattern": "main", "requiresCodeOwnerReviews": False},
                "ahead",
                "main",
                False,
            ),
            ("r1.2.3", {}, "diverged", "main", False),
            ("r1.2.3", {}, "ahead", "feature", False),
            ("r1.2.3", {}, "", "main", False),
        ):
            with self.subTest(
                target=target, changes=changes, status=status, ref=event_ref
            ):
                env = dict(
                    os.environ,
                    GITHUB_EVENT_NAME="workflow_dispatch",
                    GITHUB_REF="refs/heads/" + event_ref,
                    DEFAULT_BRANCH="main",
                    GITHUB_REPOSITORY="org/repo",
                    GITHUB_REPOSITORY_OWNER="org",
                    BRANCH_QUERY=step["env"]["BRANCH_QUERY"],
                    RELEASE_SHA="a" * 40,
                    RELEASE_PATTERN=pattern,
                    BUMP_BRANCH=target,
                    COMPARE_REPLY=status,
                    TIP=tip,
                    BRANCH_REPLY=json.dumps(
                        {"branchProtectionRule": rule | changes, "target": {"oid": tip}}
                    ),
                )
                run = subprocess.run(
                    ["bash", "-euo", "pipefail", "-c", mocks + step["run"]],
                    env=env,
                    capture_output=True,
                )
                self.assertEqual(run.returncode == 0, accepted, run.stderr)

    def test_protected_bump_waits_for_pr_review(self):
        """Keep the new topic branch available for reviewers and native checks."""
        steps = {step.get("name"): step for step in self.bump["steps"]}
        create = steps["Create and push deployment branch"]
        merge = steps["Merge deploy-release branch into target"]
        cleanup = steps["Delete temporary branch"]
        for environment in ("", "gym-release"):
            context = {
                "inputs.release-environment": environment,
                "env.IS_INERT": "false",
            }
            self.assertTrue(expression(create["if"], context))
            self.assertEqual(expression(merge["if"], context), not environment)
            self.assertEqual(
                expression(cleanup["if"].removeprefix("always() && "), context),
                not environment,
            )
            prefix = expression(create["env"]["BUMP_BRANCH_PREFIX"], context)
            self.assertEqual(
                prefix, "release-bump" if environment else "deploy-release"
            )

    def test_only_legacy_bump_commits_skip_ci(self):
        """Allow required GitHub checks to run on the protected bump PR head."""
        create = next(
            step
            for step in self.bump["steps"]
            if step.get("name") == "Create and push deployment branch"
        )
        commit = next(
            line.strip()
            for line in create["run"].splitlines()
            if line.strip().startswith("git commit ")
        )
        title = "beep boop 🤖: Bumping Gym to v1.2.4"
        for environment, expected in (
            ("gym-release", title),
            ("", title + " [skip ci]"),
        ):
            with self.subTest(environment=environment):
                suffix = expression(
                    create["env"]["CI_SKIP_SUFFIX"],
                    {"inputs.release-environment": environment},
                )
                result = subprocess.run(
                    ["bash", "-eu", "-c", 'git() { printf "%s" "$4"; }\n' + commit],
                    env=dict(os.environ, BUMP_TITLE=title, CI_SKIP_SUFFIX=suffix),
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(result.stdout, expected)
