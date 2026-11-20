# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Exercise shared reviewer resolution and GitLab write boundaries."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeowners_suggestions import match_rules, suggested_owners
from gitlab_review_pools import (
    RULE_PREFIX,
    api,
    apply_changes,
    event_mr,
    main,
    mr_changed_paths,
    plan_changes,
    resolve_identities,
    resolve_pools,
)


class SharedResolverTests(unittest.TestCase):
    """Test matching, precedence, and per-file suppression through the real engine."""

    def test_specific_suggestions_override_fallback_independently_of_owners(self):
        """Keep architecture fallback and a nested override only where applicable."""
        text = "\n".join(
            [
                "/build/ @org/ci",
                "# suggest: * @org/architects",
                "# suggest: /rollouts/ @org/rollouts",
                "# suggest: /rollouts/harness/ @org/agents",
                "# suggest: /build/ @org/ci @org/release",
                "# suggest: /Makefile @org/ci",
            ]
        )
        self.assertEqual(
            suggested_owners(
                text,
                [
                    "rollouts/harness/a.py",
                    "build/Dockerfile",
                    "Makefile",
                    ".hidden/new.py",
                ],
            ),
            ["@org/agents", "@org/architects", "@org/ci", "@org/release"],
        )

    def test_micromatch_options_preserve_globstar_dotfiles_and_literal_extglobs(self):
        """Use the same options as the prior changed-files action."""
        rules = [
            {"pattern": pattern, "owners": []}
            for pattern in [
                "docker/",
                "/a/**/b.py",
                "/literal/@(one|two).py",
                "/root/*",
            ]
        ]
        files = [
            "docker/.env",
            "nested/docker/a",
            "a/b.py",
            "a/x/b.py",
            "literal/@(one|two).py",
            "literal/one.py",
            "root/a",
            "root/sub/a",
        ]
        self.assertEqual(
            [rule["matched_files"] for rule in match_rules(rules, files)],
            [
                ["docker/.env", "nested/docker/a"],
                ["a/b.py", "a/x/b.py"],
                ["literal/@(one|two).py"],
                ["root/a"],
            ],
        )

    def test_filenames_are_data_and_empty_changes_produce_no_owners(self):
        """Preserve unusual paths without shell evaluation."""
        text = "# suggest: /a/ @org/reviewers"
        self.assertEqual(suggested_owners(text, ["a/$(touch sentinel)'file.py"]), ["@org/reviewers"])
        self.assertEqual(suggested_owners(text, ["a/line\nbreak.py"]), [])
        self.assertEqual(suggested_owners(text, []), [])


class GitLabReviewPoolTests(unittest.TestCase):
    """Keep review pools optional, idempotent, and isolated from human policy."""

    def test_webhook_only_selects_open_ready_mrs_in_the_expected_target(self):
        """Reject wrong projects, branches, drafts, and approval feedback events."""
        event = {
            "object_kind": "merge_request",
            "project": {"id": 12},
            "object_attributes": {
                "target_project_id": 12,
                "target_branch": "internal",
                "action": "update",
                "state": "opened",
                "iid": 34,
                "draft": False,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.json"
            path.write_text(json.dumps(event))
            self.assertEqual(event_mr(path, "12", "internal"), 34)
            self.assertIsNone(event_mr(path, "13", "internal"))
            self.assertIsNone(event_mr(path, "12", "other"))
            for key, value in [("action", "approved"), ("state", "closed"), ("draft", True), ("target_project_id", 99)]:
                with self.subTest(key=key):
                    altered = dict(event, object_attributes=dict(event["object_attributes"], **{key: value}))
                    path.write_text(json.dumps(altered))
                    self.assertIsNone(event_mr(path, "12", "internal"))

    def test_ignored_webhook_does_not_read_credentials_or_make_api_calls(self):
        """A non-MR trigger cannot accidentally reconcile arbitrary requests."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.json"
            path.write_text(json.dumps({"object_kind": "pipeline"}))
            argv = [
                "script",
                "--gitlab-host",
                "example.test",
                "--project",
                "12",
                "--event-file",
                str(path),
                "--target-branch",
                "internal",
                "--github-repository",
                "org/repo",
                "--apply",
            ]
            with patch("sys.argv", argv), patch("gitlab_review_pools.api") as request, patch("builtins.print"):
                main()
            request.assert_not_called()

    def test_expands_live_teams_and_excludes_author_without_guessing_accounts(self):
        """Account mapping, active membership, and author exclusion are explicit."""
        members = [
            {"id": 1, "username": "one", "access_level": 30, "state": "active"},
            {"id": 2, "username": "two", "access_level": 40, "state": "active"},
        ]
        pools, problems = resolve_pools(
            {"@org/team": ["GH-ONE", "gh-two", "missing"]},
            {"gh-one": "one", "gh-two": "two"},
            members,
            1,
        )
        self.assertEqual(pools[0]["user_ids"], [2])
        self.assertEqual(pools[0]["approvals_required"], 0)
        self.assertEqual(problems, ["@org/team: no GitLab identity for missing"])

    def test_sso_identity_join_requires_corporate_email_and_unambiguous_gitlab_lookup(self):
        """Never infer a GitLab username from the GitHub handle or email prefix."""
        roster = {
            "ONE": {"nvidia_email": "one@nvidia.com"},
            "two": {"nvidia_email": "two@nvidia.com"},
            "external": {"nvidia_email": "external@example.com"},
        }
        with (
            patch("gitlab_review_pools.sso_roster", return_value=roster),
            patch(
                "gitlab_review_pools.api",
                side_effect=[
                    [{"username": "different_login"}],
                    [{"username": "two"}, {"username": "ambiguous"}],
                ],
            ) as request,
        ):
            identities = resolve_identities({"one", "two", "external", "missing"}, "example.test")
        self.assertEqual(identities, {"one": "different_login"})
        self.assertEqual(request.call_count, 2)
        self.assertIn("one%40nvidia.com", request.call_args_list[0].args[1])

    def test_reconcile_preserves_manual_rules_and_replaces_only_managed_optional_pools(self):
        """Changing membership never edits general or code-owner approval rules."""
        existing = [
            {"id": 1, "name": "Security", "approvals_required": 2, "rule_type": "regular"},
            {"id": 2, "name": "CODEOWNERS", "approvals_required": 1, "rule_type": "code_owner"},
            {
                "id": 3,
                "name": RULE_PREFIX + "@org/a",
                "approvals_required": 0,
                "rule_type": "regular",
                "users": [{"id": 7}],
                "groups": [],
            },
            {
                "id": 4,
                "name": RULE_PREFIX + "@org/old",
                "approvals_required": 0,
                "rule_type": "regular",
                "users": [{"id": 8}],
                "groups": [],
            },
        ]
        pool = {"name": RULE_PREFIX + "@org/a", "approvals_required": 0, "user_ids": [7], "group_ids": []}
        self.assertEqual(plan_changes([pool], existing), [{"method": "DELETE", "id": 4}])
        pool["user_ids"] = [9]
        self.assertEqual(plan_changes([pool], existing)[0], {"method": "PUT", "id": 3, "body": pool})
        existing[2]["approvals_required"] = 1
        with self.assertRaisesRegex(ValueError, "non-optional"):
            plan_changes([pool], existing)

    def test_explicit_site_convention_uses_audited_account_not_github_handle(self):
        """Private-email accounts resolve only through the selected site convention."""
        with (
            patch(
                "gitlab_review_pools.sso_roster",
                return_value={
                    "public-handle": {"nvidia_email": "employee@nvidia.com"},
                },
            ),
            patch("gitlab_review_pools.api", return_value=[{"username": "employee"}]) as request,
        ):
            self.assertEqual(
                resolve_identities({"public-handle"}, "example.test", username_from_email=True),
                {"public-handle": "employee"},
            )
        self.assertIn("username=employee", request.call_args.args[1])

    def test_head_change_stops_all_writes(self):
        """A new push invalidates a previously computed pool update."""
        snapshot = {"sha": "a", "target_branch": "internal"}
        with patch(
            "gitlab_review_pools.api",
            return_value={
                "sha": "b",
                "target_branch": "internal",
                "state": "opened",
                "draft": False,
            },
        ) as request:
            with self.assertRaisesRegex(RuntimeError, "MR changed"):
                apply_changes("example.test", "projects/1/merge_requests/2", snapshot, [{"method": "POST", "body": {}}])
        self.assertEqual(request.call_count, 1)

    def test_required_rule_collision_stops_write_at_apply_time(self):
        """Recheck a rule before updating or deleting it."""
        snapshot = {"sha": "a", "target_branch": "internal", "state": "opened", "draft": False}
        with patch(
            "gitlab_review_pools.api",
            side_effect=[
                snapshot,
                {
                    "name": RULE_PREFIX + "@org/a",
                    "approvals_required": 1,
                    "rule_type": "regular",
                },
            ],
        ) as request:
            with self.assertRaisesRegex(RuntimeError, "required rule"):
                apply_changes("example.test", "projects/1/merge_requests/2", snapshot, [{"method": "DELETE", "id": 3}])
        self.assertEqual(request.call_count, 2)

    def test_complete_diff_and_rename_destination_match_github_contract(self):
        """Exclude deleted paths and use renamed destinations, as GitHub does."""
        mr = {
            "overflow": False,
            "sha": "a",
            "diff_refs": {"head_sha": "a"},
            "changes": [
                {"new_path": "new/file", "old_path": "old/file", "deleted_file": False},
                {"new_path": "removed/file", "deleted_file": True},
            ],
        }
        self.assertEqual(mr_changed_paths(mr), ["new/file"])
        mr["overflow"] = True
        with self.assertRaisesRegex(ValueError, "incomplete"):
            mr_changed_paths(mr)

    def test_cli_pagination_and_structured_request_body(self):
        """Decode both CLI pagination formats and pass payloads only via stdin."""
        with patch(
            "gitlab_review_pools.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, '[{"login":"one"}]\n[{"login":"two"}]', ""),
        ):
            self.assertEqual(api("gh", "teams", paginate=True), [{"login": "one"}, {"login": "two"}])
        with patch(
            "gitlab_review_pools.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "{}", "")
        ) as run:
            api("glab", "rules", "example.test", method="POST", body={"name": "$(id)\n'"})
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"name": "$(id)\n'"})
        self.assertNotIn("$(id)", " ".join(run.call_args.args[0]))
        self.assertIn("Content-Type: application/json", run.call_args.args[0])

    def test_incomplete_identity_map_blocks_apply_before_any_mutation(self):
        """Even explicit apply must resolve all identities before touching an MR."""
        mr = {
            "sha": "a",
            "target_branch": "internal",
            "state": "opened",
            "draft": False,
            "overflow": False,
            "diff_refs": {"head_sha": "a"},
            "author": {"id": 1},
            "changes": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            identity_map = Path(directory) / "identities.json"
            identity_map.write_text("{}")
            argv = [
                "script",
                "--gitlab-host",
                "example.test",
                "--project",
                "1",
                "--mr",
                "2",
                "--target-branch",
                "internal",
                "--github-repository",
                "org/repo",
                "--identity-map",
                str(identity_map),
                "--apply",
            ]
            with (
                patch("sys.argv", argv),
                patch("gitlab_review_pools.api", side_effect=[mr, [], []]) as request,
                patch("gitlab_review_pools.trusted_rules", return_value=("sha", "")),
                patch("gitlab_review_pools.suggested_owners", return_value=["@org/a"]),
                patch("gitlab_review_pools.github_members", return_value=["missing"]),
                patch("builtins.print"),
            ):
                with self.assertRaisesRegex(SystemExit, "no writes"):
                    main()
            self.assertTrue(all(call.kwargs.get("method", "GET") == "GET" for call in request.call_args_list))


if __name__ == "__main__":
    unittest.main()
