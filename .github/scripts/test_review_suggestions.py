# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Exercise shared reviewer resolution and GitLab write boundaries."""

import json
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from codeowners_suggestions import enforced_owner_groups, match_rules, suggested_owners
from gitlab_review_pools import (
    REQUIRED_PREFIX,
    RULE_PREFIX,
    api,
    apply_changes,
    approval_inheritance_problems,
    assign_reviewers_if_empty,
    event_mr,
    github_members,
    main,
    mr_changed_paths,
    parse_args,
    plan_changes,
    plan_sides,
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

    def test_inherited_required_rules_block_creating_optional_pools(self):
        """Regular and any-approver project requirements must not disappear."""
        for rule_type in ("regular", "any_approver"):
            with self.subTest(rule_type=rule_type):
                state = {
                    "approval_rules_overwritten": False,
                    "rules": [{"name": "Security", "rule_type": rule_type, "approvals_required": 2}],
                }
                self.assertEqual(
                    approval_inheritance_problems(state),
                    ["Creating review pools would override inherited required rule: Security"],
                )
                state["approval_rules_overwritten"] = True
                self.assertEqual(approval_inheritance_problems(state), [])

    def test_optional_defaults_and_native_codeowners_do_not_block_pools(self):
        """The documented inheritance limit does not remove CODEOWNERS rules."""
        state = {
            "approval_rules_overwritten": False,
            "rules": [
                {"name": "Suggestions", "rule_type": "regular", "approvals_required": 0},
                {"name": "CODEOWNERS", "rule_type": "code_owner", "approvals_required": 1},
            ],
        }
        self.assertEqual(approval_inheritance_problems(state), [])

    def test_new_required_project_rule_blocks_apply_before_first_pool(self):
        """A required default added after planning prevents the pending write."""
        snapshot = {"sha": "a", "target_branch": "internal", "state": "opened", "draft": False}
        state = {
            "approval_rules_overwritten": False,
            "rules": [{"name": "Security", "rule_type": "regular", "approvals_required": 1}],
        }
        with patch("gitlab_review_pools.api", side_effect=[snapshot, state]) as request:
            with self.assertRaisesRegex(RuntimeError, "inherited required rule"):
                apply_changes("example.test", "projects/1/merge_requests/2", snapshot, [{"method": "POST", "body": {}}])
        self.assertTrue(all(call.kwargs.get("method", "GET") == "GET" for call in request.call_args_list))

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
        pools, problems, warnings = resolve_pools(
            {"@org/team": ["GH-ONE", "gh-two", "missing"]},
            {"gh-one": "one", "gh-two": "two"},
            members,
            1,
        )
        self.assertEqual(pools[0]["user_ids"], [2])
        self.assertEqual(pools[0]["approvals_required"], 0)
        self.assertEqual(problems, ["@org/team: no GitLab identity for missing"])
        self.assertEqual(warnings, [])

    def test_empty_mr_gets_distinct_pool_reviewers_without_author(self):
        """Assign the union of eligible pools and verify the persisted reviewers."""
        mr = {"sha": "a", "target_branch": "internal", "state": "opened", "author": {"id": 1}, "reviewers": []}
        pools = [{"user_ids": [1, 2, 3]}, {"user_ids": [2, 4]}]
        actual = dict(mr, reviewers=[{"id": 2}, {"id": 3}, {"id": 4}])
        with patch("gitlab_review_pools.api", side_effect=[mr, actual, actual]) as request:
            result = assign_reviewers_if_empty("example.test", "projects/1/merge_requests/2", mr, pools)
        self.assertEqual(result, {"assigned_reviewer_ids": [2, 3, 4]})
        self.assertEqual(request.call_args_list[1].kwargs, {"method": "PUT", "body": {"reviewer_ids": [2, 3, 4]}})

    def test_existing_reviewer_prevents_assignment(self):
        """Do not add reviewers when the MR already has any reviewer."""
        with patch("gitlab_review_pools.api") as request:
            result = assign_reviewers_if_empty("example.test", "mr", {"reviewers": [{"id": 9}]}, [{"user_ids": [2]}])
        request.assert_not_called()
        self.assertIn("skipped", result)

    def test_reviewer_added_during_resolution_prevents_assignment(self):
        """A fresh read preserves a manual assignment made after the snapshot."""
        mr = {"sha": "a", "target_branch": "internal", "state": "opened", "author": {"id": 1}, "reviewers": []}
        current = dict(mr, reviewers=[{"id": 9}])
        with patch("gitlab_review_pools.api", return_value=current) as request:
            result = assign_reviewers_if_empty("example.test", "mr", mr, [{"user_ids": [2]}])
        self.assertEqual(request.call_count, 1)
        self.assertIn("skipped", result)

    def test_stale_head_prevents_reviewer_assignment(self):
        """Never assign reviewers from a stale diff."""
        mr = {"sha": "a", "target_branch": "internal", "state": "opened", "author": {"id": 1}, "reviewers": []}
        with (
            patch("gitlab_review_pools.api", return_value=dict(mr, sha="b")) as request,
            self.assertRaisesRegex(RuntimeError, "MR changed"),
        ):
            assign_reviewers_if_empty("example.test", "mr", mr, [{"user_ids": [2]}])
        self.assertEqual(request.call_count, 1)

    def test_existing_reviewers_do_not_skip_optional_pool_reconciliation(self):
        """Pool updates still apply when reviewer assignment is skipped."""
        mr = {
            "sha": "a",
            "target_branch": "internal",
            "state": "opened",
            "draft": False,
            "author": {"id": 1},
            "reviewers": [{"id": 9}],
        }
        args = Namespace(
            event_file=None,
            mr=2,
            project="1",
            gitlab_host="example.test",
            target_branch="internal",
            github_repository="org/repo",
            github_ref="main",
            identity_map=None,
            gitlab_username_from_email=False,
            require_codeowners=False,
            codeowners_fallback=None,
            apply=True,
        )
        pool = {"name": RULE_PREFIX + "@org/a", "approvals_required": 0, "user_ids": [2], "group_ids": []}
        with (
            patch("gitlab_review_pools.parse_args", return_value=args),
            patch("gitlab_review_pools.api", side_effect=[mr, [], [], {"approval_rules_overwritten": True}, []]),
            patch("gitlab_review_pools.mr_changed_paths", return_value=["file"]),
            patch("gitlab_review_pools.trusted_rules", return_value=("sha", "")),
            patch("gitlab_review_pools.suggested_owners", return_value=["@org/a"]),
            patch("gitlab_review_pools.github_members", return_value=["alice"]),
            patch("gitlab_review_pools.resolve_identities", return_value=({"alice": "alice"}, [])),
            patch("gitlab_review_pools.resolve_pools", return_value=([pool], [], [])),
            patch("gitlab_review_pools.plan_changes", side_effect=[[{"method": "POST", "body": pool}], []]),
            patch("gitlab_review_pools.apply_changes") as apply,
            patch("builtins.print"),
        ):
            main()
        apply.assert_called_once_with(
            "example.test", "projects/1/merge_requests/2", mr, [{"method": "POST", "body": pool}]
        )

    def test_ineligible_members_warn_without_blocking_eligible_reviewers(self):
        """Missing membership, low access, and inactive accounts do not block a pool."""
        members = [
            {"id": 1, "username": "author", "access_level": 40, "state": "active"},
            {"id": 2, "username": "reviewer", "access_level": 30, "state": "active"},
            {"id": 3, "username": "reporter", "access_level": 20, "state": "active"},
            {"id": 4, "username": "blocked", "access_level": 30, "state": "blocked"},
        ]
        logins = ["author", "reviewer", "reporter", "blocked", "nonmember"]
        pools, problems, warnings = resolve_pools({"@org/team": logins}, {login: login for login in logins}, members, 1)
        self.assertEqual(pools[0]["user_ids"], [2])
        self.assertEqual(problems, [])
        self.assertEqual(
            warnings,
            [
                "@org/team: skipping blocked, ineligible GitLab account blocked",
                "@org/team: skipping nonmember, ineligible GitLab account nonmember",
                "@org/team: skipping reporter, ineligible GitLab account reporter",
            ],
        )

    def test_ineligible_members_cannot_leave_an_empty_pool(self):
        """An author and a nonmember cannot satisfy a review pool."""
        pools, problems, warnings = resolve_pools(
            {"@org/team": ["author", "nonmember"]},
            {"author": "author", "nonmember": "nonmember"},
            [{"id": 1, "username": "author", "access_level": 40, "state": "active"}],
            1,
        )
        self.assertEqual(pools[0]["user_ids"], [])
        self.assertEqual(problems, ["@org/team: no eligible reviewers other than the MR author"])
        self.assertEqual(warnings, ["@org/team: skipping nonmember, ineligible GitLab account nonmember"])

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
                    [{"id": 1, "username": "different_login"}],
                    {"id": 1, "username": "different_login", "public_email": "ONE@nvidia.com"},
                    [{"username": "two"}, {"username": "ambiguous"}],
                ],
            ) as request,
        ):
            identities = resolve_identities({"one", "two", "external", "missing"}, "example.test")
        self.assertEqual(identities, ({"one": "different_login"}, []))
        self.assertEqual(request.call_count, 3)
        self.assertIn("one%40nvidia.com", request.call_args_list[0].args[1])

    def test_generic_search_requires_exact_email_evidence(self):
        """A matching display name or one search hit cannot establish identity."""
        for fields, expected in (
            ({"public_email": "other@nvidia.com"}, {}),
            ({"public_email": None}, {}),
            ({}, {}),
            ({"email": "ALICE@nvidia.com"}, {"alice": "different_login"}),
        ):
            with (
                self.subTest(fields=fields),
                patch("gitlab_review_pools.sso_roster", return_value={"alice": {"nvidia_email": "alice@nvidia.com"}}),
                patch(
                    "gitlab_review_pools.api",
                    side_effect=[
                        [{"id": 10, "username": "different_login", "name": "alice@nvidia.com"}],
                        {"id": 10, "username": "different_login", **fields},
                    ],
                ),
            ):
                self.assertEqual(resolve_identities({"alice"}, "example.test"), (expected, []))

    def test_unverified_search_hit_blocks_every_apply_mutation(self):
        """Even an active project Developer cannot substitute a name for email."""
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
        user = {"id": 10, "username": "other", "name": "alice@nvidia.com", "state": "active", "access_level": 30}
        args = Namespace(
            event_file=None,
            mr=2,
            project="1",
            gitlab_host="example.test",
            target_branch="internal",
            github_repository="org/repo",
            github_ref="main",
            identity_map=None,
            gitlab_username_from_email=False,
            require_codeowners=False,
            codeowners_fallback=None,
            apply=True,
        )
        with (
            patch("gitlab_review_pools.parse_args", return_value=args),
            patch("gitlab_review_pools.api", side_effect=[mr, [user], [user], user, []]) as request,
            patch("gitlab_review_pools.trusted_rules", return_value=("sha", "")),
            patch("gitlab_review_pools.suggested_owners", return_value=["@org/a"]),
            patch("gitlab_review_pools.github_members", return_value=["alice"]),
            patch("gitlab_review_pools.sso_roster", return_value={"alice": {"nvidia_email": "alice@nvidia.com"}}),
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(SystemExit, "no writes"):
                main()
        self.assertTrue(all(call.kwargs.get("method", "GET") == "GET" for call in request.call_args_list))

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
        self.assertEqual(
            plan_changes([pool], existing), [{"method": "DELETE", "id": 4, "name": RULE_PREFIX + "@org/old"}]
        )
        pool["user_ids"] = [9]
        self.assertEqual(
            plan_changes([pool], existing)[0],
            {"method": "PUT", "id": 3, "body": {**pool, "remove_hidden_groups": True}, "name": pool["name"]},
        )
        existing[2]["approvals_required"] = 1
        with self.assertRaisesRegex(ValueError, "made required by hand"):
            plan_changes([pool], existing)

    def test_hidden_or_visible_groups_prevent_pool_convergence(self):
        """Managed pools must contain only the desired explicit users."""
        pool = {"name": RULE_PREFIX + "@org/a", "approvals_required": 0, "user_ids": [7], "group_ids": []}
        current = {
            "id": 3,
            "name": pool["name"],
            "approvals_required": 0,
            "rule_type": "regular",
            "users": [{"id": 7}],
            "groups": [],
            "contains_hidden_groups": False,
        }
        expected = [{"method": "PUT", "id": 3, "body": {**pool, "remove_hidden_groups": True}, "name": pool["name"]}]
        for groups, hidden in (([], True), ([{"id": 9}], False), ([{"id": 9}], True)):
            with self.subTest(groups=groups, hidden=hidden):
                actual = dict(current, groups=groups, contains_hidden_groups=hidden)
                self.assertEqual(plan_changes([pool], [actual]), expected)
                cleaned = dict(actual, groups=[], contains_hidden_groups=False)
                self.assertEqual(plan_changes([pool], [cleaned]), [])
        self.assertEqual(plan_changes([pool], [current]), [])
        self.assertEqual(plan_changes([pool], []), [{"method": "POST", "body": pool}])

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
                ({"public-handle": "employee"}, []),
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
            with self.assertRaisesRegex(RuntimeError, "edited by hand"):
                apply_changes(
                    "example.test",
                    "projects/1/merge_requests/2",
                    snapshot,
                    [{"method": "DELETE", "id": 3, "name": RULE_PREFIX + "@org/a"}],
                )
        self.assertEqual(request.call_count, 2)

    def test_renamed_rule_stops_write_even_when_rewritable(self):
        """A rule renamed into another managed namespace since planning is never touched."""
        snapshot = {"sha": "a", "target_branch": "internal", "state": "opened", "draft": False}
        renamed = {"name": REQUIRED_PREFIX + "@org/critical", "approvals_required": 1, "rule_type": "regular"}
        with patch("gitlab_review_pools.api", side_effect=[snapshot, renamed]) as request:
            with self.assertRaisesRegex(RuntimeError, "refusing write"):
                apply_changes(
                    "example.test",
                    "projects/1/merge_requests/2",
                    snapshot,
                    [{"method": "DELETE", "id": 3, "name": RULE_PREFIX + "@org/stale"}],
                )
        self.assertTrue(all(call.kwargs.get("method", "GET") == "GET" for call in request.call_args_list))

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


class RequiredCodeOwnerPoolTests(unittest.TestCase):
    """Mirror GitHub's required code-owner review as one-approval GitLab rules."""

    MR = {
        "sha": "a",
        "target_branch": "internal",
        "state": "opened",
        "draft": False,
        "overflow": False,
        "diff_refs": {"head_sha": "a"},
        "author": {"id": 1},
        "reviewers": [],
        "changes": [{"new_path": "bio/b.py", "deleted_file": False}, {"new_path": "README.md", "deleted_file": False}],
    }
    RULES = "\n".join(["* @org/arch", "/bio/ @org/bio @org/maint", "# suggest: /bio/ @org/bio"])
    TEAMS = {"@org/arch": ["ann"], "@org/bio": ["bob"], "@org/maint": ["mia"]}

    @staticmethod
    def member(user_id: int, username: str) -> dict:
        return {"id": user_id, "username": username, "state": "active", "access_level": 30}

    def lookup_team(self, owner: str) -> list[str]:
        """Stand-in for github_members; tests override it to inject failures."""
        return self.TEAMS[owner]

    def fake_gitlab(
        self, mr: dict, members: list[dict], rules: dict | None = None, *, overwritten: bool = True, fail: tuple = ()
    ):
        """Stateful GitLab stand-in recording every write; `fail` names one (method, id) write that errors."""
        rules, writes = rules if rules is not None else {}, []

        def api(provider, endpoint, hostname="", *, method="GET", body=None, paginate=False):
            if method != "GET":
                writes.append((method, endpoint.rsplit("/", 1)[-1], body))
                if (method, endpoint.rsplit("/", 1)[-1]) == fail:
                    raise RuntimeError(f"glab {method} approval_rules failed (exit 1)")
            if endpoint.endswith("/changes?access_raw_diffs=true"):
                return mr
            if "/members/all" in endpoint:
                return members
            if endpoint.endswith("/approval_rules?per_page=100"):
                return list(rules.values())
            if endpoint.endswith("/approval_state"):
                required = [{"name": "Security", "rule_type": "regular", "approvals_required": 1}]
                return {"approval_rules_overwritten": overwritten, "rules": [] if overwritten else required}
            if "/approval_rules/" in endpoint:
                rule_id = int(endpoint.rsplit("/", 1)[-1])
                if method == "DELETE":
                    return rules.pop(rule_id)
                if method == "PUT":
                    rules[rule_id].update(body, users=[{"id": user} for user in body["user_ids"]])
                return rules[rule_id]
            if endpoint.endswith("/approval_rules") and method == "POST":
                rule_id = max(rules, default=0) + 1
                rules[rule_id] = {
                    **body,
                    "id": rule_id,
                    "rule_type": "regular",
                    "users": [{"id": user} for user in body["user_ids"]],
                    "groups": [],
                }
                return rules[rule_id]
            if endpoint.endswith("/merge_requests/2"):
                if method == "PUT":
                    mr["reviewers"] = [{"id": user} for user in body["reviewer_ids"]]
                return mr
            raise AssertionError(f"unexpected {method} {endpoint}")

        return api, rules, writes

    def run_main(
        self,
        identity_map: dict | None,
        members: list[dict],
        *extra: str,
        rules: dict | None = None,
        overwritten: bool = True,
        enforce: bool = True,
        fail: tuple = (),
    ) -> tuple[dict, list, str | None]:
        """Run main() against the fake; return the printed plan, the writes, and the exit message."""
        mr = json.loads(json.dumps(self.MR))
        api, self.rules, writes = self.fake_gitlab(mr, members, rules, overwritten=overwritten, fail=fail)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identities.json"
            path.write_text(json.dumps(identity_map))
            argv = ["script", "--gitlab-host", "example.test", "--project", "1", "--mr", "2"]
            argv += ["--target-branch", "internal", "--github-repository", "org/repo"]
            argv += ["--identity-map", str(path)] if identity_map is not None else []
            argv += (["--require-codeowners"] if enforce else []) + list(extra)
            with (
                patch("sys.argv", argv),
                patch("gitlab_review_pools.api", side_effect=api),
                patch("gitlab_review_pools.trusted_rules", return_value=("sha", self.RULES)),
                patch("gitlab_review_pools.github_members", side_effect=self.lookup_team),
                patch("builtins.print") as printed,
            ):
                failure = None
                try:
                    main()
                except SystemExit as error:
                    failure = str(error)
        plan = json.loads(printed.call_args_list[0].args[0])
        return plan, writes, failure

    def test_enforced_owner_groups_follow_last_match_and_merge_identical_owner_sets(self):
        """One group per winning enforced rule; suggestions never become required."""
        self.assertEqual(
            enforced_owner_groups(self.RULES, ["bio/b.py", "README.md", "docs/x.md", "bio/c/d.py"]),
            [["@org/arch"], ["@org/bio", "@org/maint"]],
        )
        self.assertEqual(enforced_owner_groups("# suggest: * @org/x", ["a.py"]), [])

    def test_required_pools_need_one_approval_from_any_owner_of_the_rule(self):
        """Enforced lines become one-approval rules; suggestions stay optional."""
        identities = {"ann": "ann", "bob": "bob", "mia": "mia"}
        members = [self.member(10, "ann"), self.member(11, "bob"), self.member(12, "mia")]
        plan, writes, failure = self.run_main(identities, members)
        self.assertIsNone(failure)
        self.assertEqual(plan["required_owners"], [["@org/arch"], ["@org/bio", "@org/maint"]])
        bodies = {change["body"]["name"]: change["body"] for change in plan["changes"]}
        self.assertEqual(
            bodies[REQUIRED_PREFIX + "@org/bio @org/maint"],
            {
                "name": REQUIRED_PREFIX + "@org/bio @org/maint",
                "approvals_required": 1,
                "user_ids": [11, 12],
                "group_ids": [],
            },
        )
        self.assertEqual(bodies[REQUIRED_PREFIX + "@org/arch"]["approvals_required"], 1)
        self.assertNotIn(RULE_PREFIX + "@org/bio", bodies, "suggestion duplicates an enforced owner")
        self.assertEqual(writes, [], "dry run never writes")

    def test_unresolved_identity_falls_back_to_configured_team_and_fails_the_job(self):
        """Fail closed: the MR stays gated by the fallback team until identities resolve."""
        identities = {"ann": "ann", "mia": "mia"}
        members = [self.member(10, "ann"), self.member(12, "mia")]
        plan, writes, failure = self.run_main(identities, members, "--codeowners-fallback", "@org/maint", "--apply")
        self.assertTrue(plan["fallback_applied"])
        self.assertEqual(plan["problems"], ["@org/bio @org/maint: no GitLab identity for bob"])
        posted = [body for method, _, body in writes if method == "POST"]
        self.assertEqual(
            posted,
            [
                {
                    "name": REQUIRED_PREFIX + "fallback @org/maint",
                    "approvals_required": 1,
                    "user_ids": [12],
                    "group_ids": [],
                }
            ],
        )
        self.assertIn("fell back", failure)

    def test_unresolvable_fallback_blocks_all_writes_and_explains_why(self):
        """Without an eligible fallback approver nothing is written and both problems are listed."""
        plan, writes, failure = self.run_main(
            {"ann": "ann"}, [self.member(10, "ann")], "--codeowners-fallback", "@org/maint", "--apply"
        )
        self.assertFalse(plan["fallback_applied"])
        self.assertIn("fallback @org/maint: no GitLab identity for mia", plan["problems"])
        self.assertEqual(writes, [])
        self.assertIn("no writes", failure)

    def test_weakened_required_rule_is_restored_and_prefixes_limit_the_scope(self):
        """Reconciliation restores one approval; only rules under the given prefixes are managed."""
        existing = [
            {
                "id": 1,
                "name": RULE_PREFIX + "@org/a",
                "approvals_required": 0,
                "rule_type": "regular",
                "users": [{"id": 7}],
                "groups": [],
            },
            {
                "id": 2,
                "name": REQUIRED_PREFIX + "@org/a",
                "approvals_required": 0,
                "rule_type": "regular",
                "users": [{"id": 7}],
                "groups": [],
            },
        ]
        required = {"name": REQUIRED_PREFIX + "@org/a", "approvals_required": 1, "user_ids": [7], "group_ids": []}
        self.assertEqual(
            plan_changes([required], existing, prefixes=(REQUIRED_PREFIX,)),
            [{"method": "PUT", "id": 2, "body": {**required, "remove_hidden_groups": True}, "name": required["name"]}],
        )
        self.assertEqual(
            plan_changes([required], existing),
            [
                {
                    "method": "PUT",
                    "id": 2,
                    "body": {**required, "remove_hidden_groups": True},
                    "name": required["name"],
                },
                {"method": "DELETE", "id": 1, "name": RULE_PREFIX + "@org/a"},
            ],
        )
        snapshot = {"sha": "a", "target_branch": "internal", "state": "opened", "draft": False}
        with patch("gitlab_review_pools.api", side_effect=[snapshot, existing[1], None]) as request:
            apply_changes(
                "example.test",
                "projects/1/merge_requests/2",
                snapshot,
                [{"method": "PUT", "id": 2, "body": required, "name": required["name"]}],
            )
        self.assertEqual(request.call_args_list[-1].kwargs["method"], "PUT")

    def test_owner_less_enforced_line_requires_nothing(self):
        """GitHub's un-own syntax (pattern without owners) must not force a fallback."""
        rules = "\n".join(["* @org/arch", "/docs/generated/"])
        self.assertEqual(enforced_owner_groups(rules, ["docs/generated/a.md"]), [])
        self.assertEqual(enforced_owner_groups(rules, ["docs/generated/a.md", "x.py"]), [["@org/arch"]])

    def test_degraded_run_replaces_stale_required_pools_and_reconciles_optional_pools(self):
        """The fallback supersedes exact required pools; healthy optional pools reconcile as usual."""
        stale = {
            1: {"id": 1, "name": REQUIRED_PREFIX + "@org/old", "approvals_required": 1, "rule_type": "regular"},
            2: {"id": 2, "name": RULE_PREFIX + "@org/old", "approvals_required": 0, "rule_type": "regular"},
        }
        for rule in stale.values():
            rule.update(users=[], groups=[])
        _, writes, failure = self.run_main(
            {"ann": "ann", "mia": "mia"},
            [self.member(10, "ann"), self.member(12, "mia")],
            "--codeowners-fallback",
            "@org/maint",
            "--apply",
            rules=stale,
        )
        self.assertIn("fell back", failure)
        self.assertEqual(
            sorted(ident for method, ident, _ in writes if method == "DELETE"), ["1", "2"], "both stale pools go"
        )
        self.assertEqual([rule["name"] for rule in self.rules.values()], [REQUIRED_PREFIX + "fallback @org/maint"])

    def test_suggest_only_problem_never_weakens_the_required_gate(self):
        """An advisory pool problem skips optional writes and reviewers; exact required pools still land."""
        identities = {"ann": "ann", "bob": "bob", "mia": "mia"}
        members = [self.member(10, "ann"), self.member(11, "bob"), self.member(12, "mia")]
        stale = {1: {"id": 1, "name": RULE_PREFIX + "@org/old", "approvals_required": 0, "rule_type": "regular"}}
        stale[1].update(users=[], groups=[])
        with (
            patch.object(self, "RULES", self.RULES + "\n# suggest: /README.md @org/docs"),
            patch.dict(self.TEAMS, {"@org/docs": ["dan"]}),
        ):
            plan, writes, failure = self.run_main(
                identities, members, "--codeowners-fallback", "@org/maint", "--apply", rules=stale
            )
        self.assertIn("@org/docs: no GitLab identity for dan", plan["problems"])
        self.assertFalse(plan["fallback_applied"])
        self.assertEqual(plan["reviewer_ids_to_assign"], [])
        self.assertEqual(
            sorted(rule["name"] for rule in self.rules.values()),
            [REQUIRED_PREFIX + "@org/arch", REQUIRED_PREFIX + "@org/bio @org/maint", RULE_PREFIX + "@org/old"],
        )
        self.assertEqual([body for method, _, body in writes if "reviewer_ids" in body], [], "no reviewer assignment")
        self.assertIn("reconciled with problems", failure)

    def test_unreadable_fallback_team_is_listed_but_does_not_abort_a_healthy_run(self):
        """The fallback is only needed when required pools fail; otherwise its lookup error is reported."""
        identities = {"ann": "ann", "bob": "bob", "mia": "mia"}
        members = [self.member(10, "ann"), self.member(11, "bob"), self.member(12, "mia")]

        def teams(owner):
            if owner == "@org/ops":
                raise RuntimeError("gh GET orgs/org/teams/ops/members timed out")
            return self.TEAMS[owner]

        with patch.object(self, "lookup_team", teams):
            plan, writes, failure = self.run_main(identities, members, "--codeowners-fallback", "@org/ops", "--apply")
        self.assertEqual(plan["problems"], ["@org/ops: gh GET orgs/org/teams/ops/members timed out"])
        self.assertEqual(
            sorted(body["name"] for method, _, body in writes if method == "POST"),
            [REQUIRED_PREFIX + "@org/arch", REQUIRED_PREFIX + "@org/bio @org/maint"],
        )
        self.assertIn("reconciled with problems", failure)

    def test_degraded_run_still_respects_inherited_required_rules(self):
        """Creating the fallback rule is blocked by the same inheritance guard as any other POST."""
        plan, writes, failure = self.run_main(
            {"ann": "ann", "mia": "mia"},
            [self.member(10, "ann"), self.member(12, "mia")],
            "--codeowners-fallback",
            "@org/maint",
            "--apply",
            overwritten=False,
        )
        self.assertFalse(plan["fallback_applied"])
        self.assertIn("Creating review pools would override inherited required rule: Security", plan["problems"])
        self.assertEqual(writes, [])
        self.assertIn("no writes", failure)

    def test_runs_without_the_flag_leave_required_pools_alone(self):
        """Optional-only runs must never delete another consumer's required rules."""
        required = {1: {"id": 1, "name": REQUIRED_PREFIX + "@org/a", "approvals_required": 1, "rule_type": "regular"}}
        required[1].update(users=[], groups=[])
        plan, writes, failure = self.run_main(
            {"ann": "ann", "bob": "bob"},
            [self.member(10, "ann"), self.member(11, "bob")],
            "--apply",
            rules=required,
            enforce=False,
        )
        self.assertIsNone(failure)
        self.assertEqual(plan["required_owners"], [])
        self.assertNotIn("DELETE", [method for method, _, _ in writes])
        self.assertIn(REQUIRED_PREFIX + "@org/a", [rule["name"] for rule in self.rules.values()])

    def test_reviewers_come_from_suggested_pools_only(self):
        """Required pools gate approval; they must not page every code owner as a reviewer."""
        identities = {"ann": "ann", "bob": "bob", "mia": "mia", "dan": "dan"}
        members = [self.member(10, "ann"), self.member(11, "bob"), self.member(12, "mia"), self.member(13, "dan")]
        with (
            patch.object(self, "RULES", self.RULES + "\n# suggest: /README.md @org/docs"),
            patch.dict(self.TEAMS, {"@org/docs": ["dan"]}),
        ):
            plan, _, _ = self.run_main(identities, members)
        self.assertEqual(plan["reviewer_ids_to_assign"], [13], "only the # suggest: pool member")

    def test_inherited_required_rule_is_reported_and_blocks_writes(self):
        """An inheritance problem must appear in the printed plan, not only in the exit message."""
        identities = {"ann": "ann", "bob": "bob", "mia": "mia"}
        members = [self.member(10, "ann"), self.member(11, "bob"), self.member(12, "mia")]
        plan, writes, failure = self.run_main(identities, members, "--apply", overwritten=False)
        self.assertEqual(plan["problems"], ["Creating review pools would override inherited required rule: Security"])
        self.assertEqual(writes, [])
        self.assertIn("no writes", failure)

    def test_deleting_an_owned_file_still_requires_its_owner(self):
        """Deletions count for required review even though suggestions ignore them."""
        identities = {"ann": "ann", "bob": "bob", "mia": "mia"}
        members = [self.member(10, "ann"), self.member(11, "bob"), self.member(12, "mia")]
        mr = json.loads(json.dumps(self.MR))
        mr["changes"] = [{"old_path": "bio/gone.py", "new_path": "bio/gone.py", "deleted_file": True}]
        with patch.object(self, "MR", mr):
            plan, _, failure = self.run_main(identities, members)
        self.assertIsNone(failure)
        self.assertEqual(plan["required_owners"], [["@org/bio", "@org/maint"]])
        self.assertEqual(plan["owners"], [], "suggestions keep the all_changed_files convention")

    def test_directory_without_trailing_slash_and_inline_comment_resolve_like_github(self):
        """`/bio @org/bio # note` owns bio/** and the comment never becomes an owner."""
        rules = "\n".join(["* @org/arch", "/bio @org/bio @org/maint # sensitive", "/docs/gen # unowned"])
        files = ["bio/deep/b.py", "docs/gen/x.md", "README.md"]
        self.assertEqual(enforced_owner_groups(rules, files), [["@org/arch"], ["@org/bio", "@org/maint"]])

    def test_failed_team_lookup_falls_back_instead_of_crashing(self):
        """A deleted or unreadable team is a problem the fallback handles, not an unhandled error."""
        identities = {"ann": "ann", "mia": "mia"}
        members = [self.member(10, "ann"), self.member(12, "mia")]

        def teams(owner):
            if owner == "@org/bio":
                raise RuntimeError("gh GET orgs/org/teams/bio/members failed (exit 1)")
            return self.TEAMS[owner]

        with patch.object(self, "lookup_team", teams):
            plan, writes, failure = self.run_main(identities, members, "--codeowners-fallback", "@org/maint", "--apply")
        self.assertIn("@org/bio: gh GET orgs/org/teams/bio/members failed (exit 1)", plan["problems"])
        self.assertTrue(plan["fallback_applied"])
        self.assertEqual(
            [body["name"] for method, _, body in writes if method == "POST"], [REQUIRED_PREFIX + "fallback @org/maint"]
        )
        self.assertIn("fell back", failure)

    def test_required_matching_follows_gitignore_semantics(self):
        """Directory-only `*/`, literal braces and basename `**` match as GitHub does."""
        sec = [["@org/sec"]]
        cases = [
            ("* @org/sec\n*/", ["README.md"], sec),
            ("* @org/sec\n*/", ["src/a.py"], []),
            ("* @org/sec\n/src/{public,docs}/", ["src/public/a.py", "src/docs/a.py"], sec),
            ("* @org/sec\n/src/{public,docs}/", ["src/{public,docs}/a.py"], []),
            ("/src/sec** @org/sec", ["src/secret/a.py"], sec),
            ("/src/sec** @org/sec", ["src/section/a.py"], sec),
            ("/src/sec** @org/sec", ["src/other.py"], []),
            ("/src/** @org/sec", ["src/a/b.py"], sec),
            ("* @org/a\n/src/ @org/sec", ["src"], [["@org/a"]]),
            ("* @org/a\ndocker/ @org/sec", ["x/docker"], [["@org/a"]]),
            ("* @org/sec\n**/", ["README.md"], sec),
            ("* @org/sec\n**/", ["a/b.py"], []),
            ("* @org/sec\n/src/(public|docs)/", ["src/public/a.py", "src/docs/a.py"], sec),
            ("* @org/sec\n/src/(public|docs)/", ["src/(public|docs)/a.py"], []),
            ("/src/(public)/ @org/sec", ["src/(public)/a.py"], sec),
            ("/src/(public)/ @org/sec", ["src/public/a.py"], []),
        ]
        for rules, files, expected in cases:
            with self.subTest(rules=rules, files=files):
                self.assertEqual(enforced_owner_groups(rules, files), expected)

    def test_line_terminator_paths_stop_the_run_without_touching_rules(self):
        """A glob engine cannot match such names; stop instead of un-owning the file."""
        existing = {
            1: {"id": 1, "name": REQUIRED_PREFIX + "@org/arch", "rule_type": "regular", "approvals_required": 1}
        }
        for name in ("src/secret\n.py", "src/secret\r.py", "src/secret\u2028.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                enforced_owner_groups("* @org/sec", [name])
        mr = json.loads(json.dumps(self.MR))
        mr["changes"] = [{"new_path": "src/secret\n.py", "deleted_file": False}]
        with patch.object(self, "MR", mr), self.assertRaises(ValueError):
            self.run_main(
                {"ann": "ann"},
                [self.member(10, "ann")],
                "--codeowners-fallback",
                "@org/maint",
                "--apply",
                rules=existing,
            )
        self.assertEqual(list(self.rules), [1], "no rule was deleted")

    def test_one_failed_identity_lookup_does_not_hide_the_fallback(self):
        """A GitLab lookup error for one account is a listed problem; the fallback still resolves."""
        roster = {"ann": {"nvidia_email": "ann@nvidia.com"}, "mia": {"nvidia_email": "mia@nvidia.com"}}
        with (
            patch("gitlab_review_pools.sso_roster", return_value=roster),
            patch(
                "gitlab_review_pools.api",
                side_effect=[
                    RuntimeError("glab GET users failed (exit 1)"),
                    [{"id": 12, "username": "mia"}],
                    {"id": 12, "username": "mia", "public_email": "mia@nvidia.com"},
                ],
            ),
        ):
            self.assertEqual(
                resolve_identities({"ann", "mia"}, "example.test"),
                ({"mia": "mia"}, ["ann: glab GET users failed (exit 1)"]),
            )
        members = [self.member(10, "ann"), self.member(11, "bob"), self.member(12, "mia")]
        resolved = ({"bob": "bob", "mia": "mia"}, ["ann: glab GET users/20 failed (exit 1)"])
        with patch("gitlab_review_pools.resolve_identities", return_value=resolved):
            plan, writes, failure = self.run_main(None, members, "--codeowners-fallback", "@org/maint", "--apply")
        self.assertIn("ann: glab GET users/20 failed (exit 1)", plan["problems"])
        self.assertTrue(plan["fallback_applied"])
        self.assertEqual(
            [body["name"] for method, _, body in writes if method == "POST"], [REQUIRED_PREFIX + "fallback @org/maint"]
        )
        self.assertIn("fell back", failure)

    def test_cli_timeouts_are_lookup_failures(self):
        """A hung gh/glab call surfaces like any other failed lookup, so the fallback can handle it."""
        with patch("gitlab_review_pools.subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 120)):
            with self.assertRaisesRegex(RuntimeError, "gh GET orgs/org/teams/bio/members timed out"):
                api("gh", "orgs/org/teams/bio/members?per_page=100")

    def test_required_problem_without_fallback_blocks_all_writes(self):
        """Fail closed: with no fallback configured, a required-pool problem writes nothing."""
        plan, writes, failure = self.run_main({"ann": "ann", "mia": "mia"}, [self.member(10, "ann")], "--apply")
        self.assertFalse(plan["fallback_applied"])
        self.assertIn("@org/bio @org/maint: no GitLab identity for bob", plan["problems"])
        self.assertEqual(writes, [])
        self.assertIn("no writes", failure)

    def test_plan_sides_isolates_each_side(self):
        """The optional side is advisory and the required side fails closed, independently."""
        opt, req, fb = (
            [{"name": RULE_PREFIX + "a"}],
            [{"name": REQUIRED_PREFIX + "b"}],
            [{"name": REQUIRED_PREFIX + "fallback"}],
        )
        both = {REQUIRED_PREFIX: req, RULE_PREFIX: opt}
        cases = {
            "healthy": (((opt, []), (req, []), None), (both, [], False)),
            "optional problem": (((opt, ["x"]), (req, []), None), ({REQUIRED_PREFIX: req}, [], False)),
            "required problem, no fallback": (((opt, []), (req, ["y"]), None), (both, ["y"], False)),
            "required problem, fallback ok": (
                ((opt, []), (req, ["y"]), (fb, [])),
                ({REQUIRED_PREFIX: fb, RULE_PREFIX: opt}, [], True),
            ),
            "required problem, fallback broken": (((opt, []), (req, ["y"]), (fb, ["z"])), (both, ["y"], False)),
            "both sides broken, fallback ok": (
                ((opt, ["x"]), (req, ["y"]), (fb, [])),
                ({REQUIRED_PREFIX: fb}, [], True),
            ),
            "both sides broken, no fallback": (
                ((opt, ["x"]), (req, ["y"]), None),
                ({REQUIRED_PREFIX: req}, ["y"], False),
            ),
            "both sides broken, fallback broken": (
                ((opt, ["x"]), (req, ["y"]), (fb, ["z"])),
                ({REQUIRED_PREFIX: req}, ["y"], False),
            ),
        }
        for name, (inputs, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(plan_sides(*inputs, enforce=True), expected)
        sides, _, _ = plan_sides((opt, []), (req, []), None, enforce=True)
        self.assertEqual(list(sides), [REQUIRED_PREFIX, RULE_PREFIX], "required side is reconciled first")
        self.assertEqual(plan_sides((opt, ["x"]), ([], []), None, enforce=False), ({}, ["x"], False))
        self.assertEqual(plan_sides((opt, []), ([], []), None, enforce=False), ({RULE_PREFIX: opt}, [], False))

    def test_required_rules_with_other_types_or_counts(self):
        """A hand-made non-regular rule under the prefix stops the run; two approvals are reset to one."""
        required = {"name": REQUIRED_PREFIX + "@org/a", "approvals_required": 1, "user_ids": [7], "group_ids": []}
        foreign = {"id": 1, "name": REQUIRED_PREFIX + "@org/a", "approvals_required": 1, "rule_type": "any_approver"}
        with self.assertRaisesRegex(ValueError, "not a regular rule"):
            plan_changes([required], [foreign])
        strict = {**foreign, "rule_type": "regular", "approvals_required": 2, "users": [{"id": 7}], "groups": []}
        self.assertEqual([change["method"] for change in plan_changes([required], [strict])], ["PUT"])

    def test_changed_paths_cover_renames_and_deletions(self):
        """Renamed files use their new path; deletions count only for required review."""
        mr = {
            "sha": "a",
            "overflow": False,
            "diff_refs": {"head_sha": "a"},
            "changes": [
                {"old_path": "old.py", "new_path": "new.py", "deleted_file": False},
                {"old_path": "gone.py", "new_path": "gone.py", "deleted_file": True},
                {"old_path": "added.py", "new_path": "added.py", "deleted_file": False},
            ],
        }
        self.assertEqual(mr_changed_paths(mr), ["added.py", "new.py"])
        self.assertEqual(mr_changed_paths(mr, include_deleted=True), ["added.py", "gone.py", "new.py"])

    def test_author_as_the_only_eligible_owner_falls_back(self):
        """The most common trigger: the author is the sole mapped member of a required owner set."""
        identities = {"ann": "ann", "bob": "bob", "mia": "mia"}
        members = [self.member(1, "ann"), self.member(11, "bob"), self.member(12, "mia")]
        plan, writes, failure = self.run_main(identities, members, "--codeowners-fallback", "@org/maint", "--apply")
        self.assertIn("@org/arch: no eligible reviewers other than the MR author", plan["problems"])
        self.assertTrue(plan["fallback_applied"])
        self.assertEqual(
            [body["name"] for method, _, body in writes if method == "POST"], [REQUIRED_PREFIX + "fallback @org/maint"]
        )
        self.assertIn("fell back", failure)

    def test_email_owner_is_an_explicit_problem(self):
        """GitHub accepts email owners; the mirror cannot resolve them, so it says so instead of guessing."""
        identities = {"ann": "ann", "mia": "mia"}
        members = [self.member(10, "ann"), self.member(12, "mia")]

        def teams(owner):
            return self.TEAMS[owner] if owner in self.TEAMS else github_members(owner)

        with (
            patch.object(self, "RULES", "* @org/arch\n/bio/ docs@example.com"),
            patch.object(self, "lookup_team", teams),
        ):
            plan, _, failure = self.run_main(identities, members, "--codeowners-fallback", "@org/maint")
        self.assertIn("docs@example.com: email owners are not supported; use a GitHub login or team", plan["problems"])
        self.assertIn("fell back", failure)

    def test_hand_required_optional_pool_is_ignored_when_only_required_rules_are_managed(self):
        """Reconciling the required side alone neither touches nor trips over a hand-required optional pool."""
        hand_required = {"id": 1, "name": RULE_PREFIX + "@org/a", "approvals_required": 1, "rule_type": "regular"}
        self.assertEqual(plan_changes([], [hand_required], prefixes=(REQUIRED_PREFIX,)), [])

    def test_required_side_converges_before_an_optional_write_fails(self):
        """Required repairs land and verify first; a failing optional write then fails the run."""
        identities = {"ann": "ann", "bob": "bob", "mia": "mia", "dan": "dan"}
        members = [self.member(10, "ann"), self.member(11, "bob"), self.member(12, "mia"), self.member(13, "dan")]
        existing = {
            1: {"id": 1, "name": REQUIRED_PREFIX + "@org/arch", "approvals_required": 0, "rule_type": "regular"},
            2: {"id": 2, "name": RULE_PREFIX + "@org/docs", "approvals_required": 0, "rule_type": "regular"},
        }
        for rule in existing.values():
            rule.update(users=[], groups=[])
        with (
            patch.object(self, "RULES", self.RULES + "\n# suggest: /README.md @org/docs"),
            patch.dict(self.TEAMS, {"@org/docs": ["dan"]}),
            self.assertRaisesRegex(RuntimeError, "glab PUT approval_rules failed"),
        ):
            self.run_main(identities, members, "--apply", rules=existing, fail=("PUT", "2"))
        self.assertEqual(self.rules[1]["approvals_required"], 1, "required rule restored before the optional write")
        self.assertEqual(self.rules[1]["user_ids"], [10])

    def test_hand_required_optional_pool_blocks_only_the_optional_side(self):
        """A human-required optional rule is reported; required pools still converge; no reviewers assigned."""
        identities = {"ann": "ann", "bob": "bob", "mia": "mia", "dan": "dan"}
        members = [self.member(10, "ann"), self.member(11, "bob"), self.member(12, "mia"), self.member(13, "dan")]
        existing = {1: {"id": 1, "name": RULE_PREFIX + "@org/docs", "approvals_required": 1, "rule_type": "regular"}}
        existing[1].update(users=[], groups=[])
        with (
            patch.object(self, "RULES", self.RULES + "\n# suggest: /README.md @org/docs"),
            patch.dict(self.TEAMS, {"@org/docs": ["dan"]}),
        ):
            plan, writes, failure = self.run_main(identities, members, "--apply", rules=existing)
        self.assertIn("Refusing to change rule github-review-pool: @org/docs", "".join(plan["problems"]))
        self.assertEqual(plan["reviewer_ids_to_assign"], [])
        self.assertEqual(
            sorted(rule["name"] for rule in self.rules.values()),
            [REQUIRED_PREFIX + "@org/arch", REQUIRED_PREFIX + "@org/bio @org/maint", RULE_PREFIX + "@org/docs"],
        )
        self.assertEqual([body for method, _, body in writes if "reviewer_ids" in body], [])
        self.assertIn("reconciled with problems", failure)

    def test_fallback_requires_the_enforcement_flag(self):
        argv = ["script", "--gitlab-host", "h", "--project", "1", "--mr", "2", "--target-branch", "b"]
        argv += ["--github-repository", "o/r", "--codeowners-fallback", "@org/maint"]
        with patch("sys.argv", argv), patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                parse_args()


if __name__ == "__main__":
    unittest.main()
