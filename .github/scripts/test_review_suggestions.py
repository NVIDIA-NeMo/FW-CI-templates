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
    main,
    mr_changed_paths,
    parse_args,
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
            patch("gitlab_review_pools.resolve_identities", return_value={"alice": "alice"}),
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
        self.assertEqual(identities, {"one": "different_login"})
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
                self.assertEqual(resolve_identities({"alice"}, "example.test"), expected)

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
        self.assertEqual(plan_changes([pool], existing), [{"method": "DELETE", "id": 4}])
        pool["user_ids"] = [9]
        self.assertEqual(
            plan_changes([pool], existing)[0],
            {"method": "PUT", "id": 3, "body": {**pool, "remove_hidden_groups": True}},
        )
        existing[2]["approvals_required"] = 1
        with self.assertRaisesRegex(ValueError, "non-optional"):
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
        expected = [{"method": "PUT", "id": 3, "body": {**pool, "remove_hidden_groups": True}}]
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

    def fake_gitlab(self, mr: dict, members: list[dict]):
        """Stateful GitLab stand-in recording every write."""
        rules, writes = {}, []

        def api(provider, endpoint, hostname="", *, method="GET", body=None, paginate=False):
            if method != "GET":
                writes.append((method, endpoint.rsplit("/", 1)[-1], body))
            if endpoint.endswith("/changes?access_raw_diffs=true"):
                return mr
            if "/members/all" in endpoint:
                return members
            if endpoint.endswith("/approval_rules?per_page=100"):
                return list(rules.values())
            if endpoint.endswith("/approval_state"):
                return {"approval_rules_overwritten": True, "rules": []}
            if endpoint.endswith("/approval_rules") and method == "POST":
                rule_id = len(rules) + 1
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

    def run_main(self, identity_map: dict, members: list[dict], *extra: str) -> tuple[dict, list, list]:
        mr = json.loads(json.dumps(self.MR))
        api, rules, writes = self.fake_gitlab(mr, members)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identities.json"
            path.write_text(json.dumps(identity_map))
            argv = ["script", "--gitlab-host", "example.test", "--project", "1", "--mr", "2"]
            argv += ["--target-branch", "internal", "--github-repository", "org/repo", "--identity-map", str(path)]
            argv += ["--require-codeowners", *extra]
            with (
                patch("sys.argv", argv),
                patch("gitlab_review_pools.api", side_effect=api),
                patch("gitlab_review_pools.trusted_rules", return_value=("sha", self.RULES)),
                patch("gitlab_review_pools.github_members", side_effect=lambda owner: self.TEAMS[owner]),
                patch("builtins.print") as printed,
            ):
                exit_code = None
                try:
                    main()
                except SystemExit as error:
                    exit_code = error
        plan = json.loads(printed.call_args_list[0].args[0])
        return plan, writes, [exit_code] if exit_code else []

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
        plan, writes, exits = self.run_main(identities, members)
        self.assertEqual(exits, [])
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
        plan, writes, exits = self.run_main(identities, members, "--codeowners-fallback", "@org/maint", "--apply")
        self.assertTrue(plan["fallback"])
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
        self.assertEqual(len(exits), 1)
        self.assertIn("fell back", str(exits[0]))

    def test_unresolvable_fallback_blocks_all_writes(self):
        """Without an eligible fallback approver nothing is written and the job fails."""
        plan, writes, exits = self.run_main(
            {"ann": "ann"}, [self.member(10, "ann")], "--codeowners-fallback", "@org/maint", "--apply"
        )
        self.assertFalse(plan["fallback"])
        self.assertEqual(writes, [])
        self.assertIn("no writes", str(exits[0]))

    def test_weakened_required_rule_is_restored_and_optional_rules_stay_untouched(self):
        """Reconciliation restores one approval; prefixes limit what is managed."""
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
            plan_changes([required], existing, (REQUIRED_PREFIX,)),
            [{"method": "PUT", "id": 2, "body": {**required, "remove_hidden_groups": True}}],
        )
        self.assertEqual(
            plan_changes([required], existing),
            [
                {"method": "PUT", "id": 2, "body": {**required, "remove_hidden_groups": True}},
                {"method": "DELETE", "id": 1},
            ],
        )
        snapshot = {"sha": "a", "target_branch": "internal", "state": "opened", "draft": False}
        with patch("gitlab_review_pools.api", side_effect=[snapshot, existing[1], None]) as request:
            apply_changes(
                "example.test", "projects/1/merge_requests/2", snapshot, [{"method": "PUT", "id": 2, "body": required}]
            )
        self.assertEqual(request.call_args_list[-1].kwargs["method"], "PUT")

    def test_fallback_requires_the_enforcement_flag(self):
        argv = ["script", "--gitlab-host", "h", "--project", "1", "--mr", "2", "--target-branch", "b"]
        argv += ["--github-repository", "o/r", "--codeowners-fallback", "@org/maint"]
        with patch("sys.argv", argv), patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                parse_args()


if __name__ == "__main__":
    unittest.main()
