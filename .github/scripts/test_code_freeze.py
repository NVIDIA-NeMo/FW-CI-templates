# Copyright (c) 2026, NVIDIA CORPORATION. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Execute freeze workflow shells against disposable Git repositories."""

import os
from concurrent.futures import ThreadPoolExecutor
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

    def resolve_cutoff(self, cutoff, **overrides):
        """Run the template's resolver with disposable output and offline API responses."""
        dispatch_sha = "a" * 40
        cutoff_sha = "b" * 40
        output = self.root / "cutoff-output"
        output.write_text("")
        env = {
            "FREEZE_COMMIT": cutoff,
            "FREEZE_SOURCE_BRANCH": "main",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": "example/project",
            "GITHUB_SHA": dispatch_sha,
            "GITHUB_OUTPUT": str(output),
            "FIXTURE_SHA": cutoff_sha,
            "FIXTURE_STATUS": "ahead",
        }
        env.update(overrides)
        api = """
gh() {
    [[ $1 == api && $# == 4 && $3 == --jq ]] || return 1
    case "$2" in
        "repos/example/project/commits/release%2Fcutoff"|"repos/example/project/commits/$FIXTURE_SHA")
            [[ -n "$FIXTURE_SHA" ]] || return 1
            jq -n --arg sha "$FIXTURE_SHA" '{sha: $sha}' | jq -r "$4" ;;
        "repos/example/project/compare/$FIXTURE_SHA...$GITHUB_SHA")
            [[ -n "$FIXTURE_STATUS" ]] || return 1
            jq -n --arg status "$FIXTURE_STATUS" '{status: $status}' | jq -r "$4" ;;
        *) return 1 ;;
    esac
}
"""
        result = self.shell(api + step("resolve-freeze-commit", "Resolve freeze cutoff")["run"], env)
        return result.returncode, output.read_text()

    def test_cutoff_resolution_pins_the_run_or_an_ancestor(self):
        """Accept an empty cutoff, the source branch, and immutable or named ancestors."""
        for branch in ("main", "trunk", "release/maintained"):
            for cutoff in ("", branch):
                with self.subTest(branch=branch, cutoff=cutoff):
                    result = self.resolve_cutoff(
                        cutoff, FREEZE_SOURCE_BRANCH=branch, GITHUB_REF=f"refs/heads/{branch}"
                    )
                    self.assertEqual(result, (0, "sha=" + "a" * 40 + "\n"))
        for cutoff in ("release/cutoff", "b" * 40):
            for status in ("ahead", "identical"):
                with self.subTest(cutoff=cutoff, status=status):
                    self.assertEqual(
                        self.resolve_cutoff(cutoff, FIXTURE_STATUS=status),
                        (0, "sha=" + "b" * 40 + "\n"),
                    )

    def test_cutoff_resolution_fails_closed(self):
        """Reject off-source dispatch, unrelated cutoffs, and failed or malformed API responses."""
        for overrides in (
            {"GITHUB_REF": "refs/heads/topic"},
            {"GITHUB_REF": "refs/tags/main"},
            {"FIXTURE_SHA": ""},
            {"FIXTURE_SHA": "invalid"},
            {"FIXTURE_STATUS": "behind"},
            {"FIXTURE_STATUS": "diverged"},
            {"FIXTURE_STATUS": ""},
        ):
            with self.subTest(overrides=overrides):
                code, output = self.resolve_cutoff("release/cutoff", **overrides)
                self.assertNotEqual(code, 0)
                self.assertEqual(output, "")

    def test_cutoff_policy_is_optional_and_applies_before_release_credentials(self):
        """Select the caller's ref only when it omitted the optional branch policy."""
        policy = WORKFLOW["on"]["workflow_call"]["inputs"]["freeze-source-branch"]
        self.assertEqual(policy["default"], "")
        resolver = WORKFLOW["jobs"]["resolve-freeze-commit"]
        self.assertNotIn("if", resolver)
        self.assertEqual(resolver["permissions"], {"contents": "read"})
        resolution = step("resolve-freeze-commit", "Resolve freeze cutoff")
        self.assertEqual(resolution["if"], "inputs.freeze-source-branch != ''")
        self.assertEqual(resolution["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertEqual(resolver["outputs"]["sha"], "${{ steps.resolve.outputs.sha }}")
        writer = WORKFLOW["jobs"]["create-release-branch"]
        self.assertEqual(writer["needs"], "resolve-freeze-commit")
        self.assertEqual(writer["if"], "inputs.freeze-source-branch == '' || needs.resolve-freeze-commit.outputs.sha != ''")
        self.assertEqual(
            step("create-release-branch", "Checkout repository")["with"]["ref"],
            "${{ inputs.freeze-source-branch == '' && inputs.freeze-commit || needs.resolve-freeze-commit.outputs.sha }}",
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
                            "RELEASE_ENVIRONMENT": "",
                            "DRY_RUN": "false",
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

    def test_only_live_legacy_freezes_use_the_legacy_push(self):
        """Both protected runs and dry runs must skip the legacy force push."""
        for dry_run, environment in (("true", ""), ("false", ""), ("true", "gym-release"), ("false", "gym-release")):
            with self.subTest(dry_run=dry_run, environment=environment), tempfile.TemporaryDirectory(dir=self.root) as work:
                previous_root, self.root = self.root, Path(work)
                try:
                    _, remote = self.fixture()
                    output = self.root / "outputs"
                    script = step("create-release-branch", "Get release branch ref")["run"]
                    result = self.shell(script, {
                        "DRY_RUN": dry_run, "RELEASE_ENVIRONMENT": environment, "SRC_DIR": "", "PYPROJECT_NAME": "package",
                        "RELEASE_BRANCH_PREFIX": "core_", "GITHUB_RUN_ID": "123", "GITHUB_OUTPUT": str(output),
                    })
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(output.read_text(), "version=core_r1.2.3\n")
                    refs = subprocess.run(
                        ["git", "--git-dir", str(remote), "for-each-ref", "--format=%(refname)"],
                        check=True, capture_output=True, text=True,
                    ).stdout.splitlines()
                    self.assertEqual("refs/heads/core_r1.2.3" in refs, dry_run == "false" and not environment)
                finally:
                    self.root = previous_root

    def publish_protected_ref(self, checkout, cutoff, branch="r1.2.3"):
        """Run the protected publication step against the disposable origin."""
        publish = step("create-release-branch", "Create protected release branch")
        return self.shell(publish["run"], {
            "CUTOFF_SHA": cutoff, "RELEASE_BRANCH": branch, "GITHUB_REF": "refs/heads/main",
        }, cwd=checkout)

    def test_protected_release_creation_and_identical_retry(self):
        """Retry the same cutoff, reject an existing ancestor, and preserve the source branch."""
        checkout, remote = self.fixture()
        initial = subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip()
        for _ in range(2):
            result = self.publish_protected_ref(checkout, initial)
            self.assertEqual(result.returncode, 0, result.stderr)
        subprocess.run(["git", "-C", checkout, "commit", "--allow-empty", "-m", "new cutoff"], check=True, capture_output=True)
        later = subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip()
        for cutoff, branch in ((later, "r1.2.3"), (later, "main"), ("", "r1.2.3")):
            with self.subTest(cutoff=cutoff, branch=branch):
                result = self.publish_protected_ref(checkout, cutoff, branch)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("::error::", result.stdout)
        for branch in ("r1.2.3", "main"):
            self.assertEqual(subprocess.check_output(["git", "-C", remote, "rev-parse", branch], text=True).strip(), initial)

    def test_concurrent_protected_release_creation(self):
        """Two simultaneous cutoffs may create one branch, and cannot move its tip."""
        checkout, remote = self.fixture()
        initial = subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip()
        subprocess.run(["git", "-C", checkout, "commit", "--allow-empty", "-m", "concurrent cutoff"], check=True, capture_output=True)
        later = subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip()
        with ThreadPoolExecutor(max_workers=2) as executor:
            runs = {cutoff: executor.submit(self.publish_protected_ref, checkout, cutoff) for cutoff in (initial, later)}
            successes = [cutoff for cutoff, run in runs.items() if run.result().returncode == 0]
        published = subprocess.check_output(["git", "-C", remote, "rev-parse", "r1.2.3"], text=True).strip()
        self.assertEqual(successes, [published])
        self.assertEqual(subprocess.check_output(["git", "-C", remote, "rev-parse", "main"], text=True).strip(), initial)

    def test_version_bump_computes_setuptools_and_hatch_without_pushing(self):
        for packaging, release_type, prerelease, dev, expected in [
            ("setuptools", "minor", "rc0", "dev0", "1.3.0rc0.dev0"),
            ("setuptools", "major", "", "", "2.0.0"),
            ("hatch", "minor", "", "", "1.3.0"),
            ("setuptools", "minor", "rc/team", "nightly", "1.3.0rc/team.nightly"),
            ("hatch", "minor", "", "team's-build", "1.3.0.team's-build"),
            ("setuptools", "minor", "", "preview\nnightly", "1.3.0.preview\nnightly"),
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
                        result = self.shell(script, {
                            "SRC_DIR": "", "PYPROJECT_NAME": "package", "PACKAGING": packaging,
                            "RELEASE_TYPE": release_type, "NEXT_PRERELEASE": prerelease, "NEXT_DEV": dev,
                            "GITHUB_RUN_ID": "123", "GITHUB_OUTPUT": str(output),
                        })
                        self.assertEqual(result.returncode, 0, result.stderr)
                        emitted = output.read_text()
                        if "\n" in expected:
                            header, _, remainder = emitted.partition("\n")
                            self.assertRegex(header, r"^version<<[0-9a-f]{32}$")
                            self.assertEqual(remainder, f"{expected}\n{header.removeprefix('version<<')}\n")
                        else:
                            self.assertEqual(emitted, f"version={expected}\n")
                        info = (checkout / "package/package_info.py").read_text()
                        values = {}
                        exec(info, values)
                        if packaging == "hatch":
                            self.assertEqual(values["__version__"], expected)
                        else:
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

    def test_protected_freeze_token_selection(self):
        """Real freezes require the environment App, and dry runs use the read token."""
        cases = [
            ("false", "12345", "release-installation", "release-installation"),
            ("false", "", "release-installation", None),
            ("false", "invalid", "release-installation", None),
            ("false", "12345", "", None),
            ("true", "", "", "read-token"),
        ]
        for job in ("create-release-branch", "bump-next-version"):
            script = step(job, "Select GitHub token")["run"]
            for dry_run, release_id, app_token, expected in cases:
                with self.subTest(job=job, dry_run=dry_run, release_id=release_id, token=bool(app_token)):
                    output = self.root / "selection"
                    output.write_text("")
                    result = self.shell(script, {
                        "RELEASE_ENVIRONMENT": "gym-release",
                        "DRY_RUN": dry_run,
                        "RELEASE_APP_ID": release_id,
                        "APP_TOKEN": app_token,
                        "APP_ID": "67890",
                        "BOT_KEY": "legacy-key",
                        "LEGACY_TOKEN": "read-token",
                        "GITHUB_OUTPUT": str(output),
                    })
                    if expected is None:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(output.read_text(), "")
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(output.read_text(), f"token={expected}\n")

    def test_tokens_stay_in_their_job_and_cover_every_writer(self):
        token = "${{ steps.github-token.outputs.token }}"
        for job in ["create-release-branch", "bump-next-version"]:
            minted = step(job, "Generate GitHub App token")
            self.assertEqual(minted["if"], "(inputs.release-environment != '' && !inputs.dry-run) || (inputs.release-environment == '' && inputs.app-id != '')")
            self.assertIn("(inputs.release-environment != '' && inputs.dry-run && 'public') || inputs.release-environment", WORKFLOW["jobs"][job]["environment"]["name"])
            self.assertEqual(minted["with"]["private-key"], "${{ secrets[inputs.release-environment == '' && 'BOT_KEY' || 'RELEASE_APP_PRIVATE_KEY'] }}")
            self.assertNotIn("owner", minted["with"])
            self.assertNotIn("repositories", minted["with"])
            self.assertEqual(minted["with"]["permission-contents"], "write")
            self.assertEqual(minted["with"].get("permission-pull-requests"),
                             "write" if job == "bump-next-version" else None)
            self.assertNotIn("permission-issues", minted["with"])
            self.assertEqual(step(job, "Checkout repository")["with"]["token"], token)
            self.assertNotIn("token", WORKFLOW["jobs"][job].get("outputs", {}))
        release_token = step("create-release-branch", "Generate GitHub App token")
        publish = step("create-release-branch", "Create protected release branch")
        self.assertEqual(publish["if"], "inputs.release-environment != '' && !inputs.dry-run")
        self.assertEqual(publish["env"]["CUTOFF_SHA"], "${{ needs.resolve-freeze-commit.outputs.sha }}")
        self.assertEqual(release_token["with"]["permission-administration"], "${{ inputs.release-environment == '' && 'write' || 'read' }}")
        checkout = step("bump-next-version", "Checkout repository")
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        pull = step("bump-next-version", "Create Version Bump PR")
        self.assertEqual(pull["if"], "${{ inputs.dry-run != true }}")
        self.assertEqual(pull["with"]["token"],
                         "${{ inputs.release-environment == '' && inputs.app-id == '' && github.token || steps.github-token.outputs.token }}")

    def test_backports_use_cherrypick_command_instead_of_labels(self):
        for item in WORKFLOW["jobs"]["create-release-branch"]["steps"]:
            self.assertNotEqual(item["name"], "Create cherry-pick label")
            self.assertNotIn("gh label", item.get("run", ""))
        message = step("notify", "Send Slack alert")["with"]["message"]
        self.assertIn("/cherrypick to=${{ env.RELEASE_BRANCH }}", message)
        self.assertNotIn("assign the label", message)


if __name__ == "__main__":
    unittest.main()
