# Copyright (c) 2026, NVIDIA CORPORATION. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Exercise the real approval workflow's classification without API mutations."""

import ast
from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.load(
    (ROOT / ".github/workflows/_test_approval_queue.yml").read_text(),
    Loader=yaml.BaseLoader,
)
SCRIPT = next(
    step["run"]
    for step in WORKFLOW["jobs"]["approve-queue"]["steps"]
    if step["name"] == "Approve waiting deployments"
)
FUNCTIONS = {
    "get_pr_info", "is_internal_contributor", "branch_matches", "matches_queue"
}


def queue_functions(sso_users=None):
    """Load only pure queue functions; never execute workflow side effects."""
    nodes = [
        node for node in ast.parse(SCRIPT).body
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS
    ]
    if {node.name for node in nodes} != FUNCTIONS:
        raise AssertionError("Approval queue functions changed or are missing")
    namespace = {
        "re": re,
        "sso_users": sso_users or {},
        "internal_org_roles": {"NVIDIA:Member", "NVIDIA-NeMo:Member"},
        "excluded_other_branches": {"main", "dev"},
        "TARGET_BRANCH": "main",
        "CONTRIBUTOR_TYPE": "internal",
        "pr_info_cache": {},
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "approval-queue", "exec"), namespace)
    return namespace


class ContributorClassificationTests(unittest.TestCase):
    def test_trusted_service_accounts_need_no_sso_record(self):
        classify = queue_functions()["is_internal_contributor"]
        for login in ["svcnemo-autobot", "svcnvidia-nemo-ci"]:
            with self.subTest(login=login):
                self.assertTrue(classify({"user": {"login": login}}))

    def test_bot_like_accounts_are_not_trusted(self):
        classify = queue_functions()["is_internal_contributor"]
        for login in [
            "svcnemo-autobot-fork", "svcnemo-autobot[bot]", "prefix-svcnemo-autobot",
            "svcnvidia-nemo-ci-fork", "dependabot[bot]", "github-actions[bot]",
        ]:
            with self.subTest(login=login):
                self.assertFalse(classify({"user": {"login": login}}))

    def test_internal_human_org_roles_are_preserved(self):
        for role in ["NVIDIA:Member", "NVIDIA-NeMo:Member"]:
            with self.subTest(role=role):
                classify = queue_functions({"person": {"org_roles": [role]}})[
                    "is_internal_contributor"
                ]
                self.assertTrue(classify({"user": {"login": "person"}}))

    def test_external_and_missing_authors_remain_external(self):
        classify = queue_functions({"person": {"org_roles": ["Other:Member"]}})[
            "is_internal_contributor"
        ]
        for pr in [{}, {"user": {}}, {"user": {"login": "person"}},
                   {"user": {"login": "unknown"}}]:
            with self.subTest(pr=pr):
                self.assertFalse(classify(pr))

    def test_service_account_policy_matches_sso_action(self):
        action = (ROOT / ".github/actions/check-nvidia-sso/action.yml").read_text()
        line = next(line for line in action.splitlines() if '"$USERNAME" ==' in line)
        trusted = set(re.findall(r'== "([^"]+)"', line))
        classify = queue_functions()["is_internal_contributor"]
        self.assertEqual(trusted, {"svcnemo-autobot", "svcnvidia-nemo-ci"})
        for login in trusted:
            self.assertTrue(classify({"user": {"login": login}}))


class QueueSelectionTests(unittest.TestCase):
    def setUp(self):
        self.namespace = queue_functions()
        self.pr = {"user": {"login": "svcnemo-autobot"}, "base": {"ref": "main"}}
        self.namespace["make_request"] = lambda endpoint: self.pr
        self.run = {
            "head_branch": "pull-request/123",
            "actor": {"login": "copy-pr-bot[bot]"},
        }

    def test_trusted_author_joins_only_internal_queue(self):
        select = self.namespace["matches_queue"]
        self.assertEqual(select(self.run), (True, 123, "main", True))
        self.namespace["CONTRIBUTOR_TYPE"] = "external"
        self.assertEqual(select(self.run), (False, 123, "main", True))

    def test_trusted_workflow_actor_cannot_promote_external_author(self):
        self.pr["user"]["login"] = "external-person"
        self.run["actor"]["login"] = "svcnemo-autobot"
        self.run["triggering_actor"] = {"login": "svcnvidia-nemo-ci"}
        select = self.namespace["matches_queue"]
        self.assertEqual(select(self.run), (False, 123, "main", False))
        self.namespace["CONTRIBUTOR_TYPE"] = "external"
        self.assertEqual(select(self.run), (True, 123, "main", False))

    def test_branch_filters_still_apply_to_trusted_accounts(self):
        select = self.namespace["matches_queue"]
        self.pr["base"]["ref"] = "dev"
        self.assertEqual(select(self.run), (False, 123, "dev", None))
        self.namespace["TARGET_BRANCH"] = "others"
        self.assertEqual(select(self.run), (False, 123, "dev", None))
        self.pr["base"]["ref"] = "release"
        self.assertEqual(select(self.run), (True, 123, "release", True))
        self.pr["base"]["ref"] = "main"
        self.assertEqual(select(self.run), (False, 123, "main", None))

    def test_non_pr_and_unavailable_pr_do_not_join_any_queue(self):
        select = self.namespace["matches_queue"]
        self.assertEqual(select({"head_branch": "main"}), (False, None, None, None))
        self.namespace["make_request"] = lambda endpoint: None
        self.assertEqual(select(self.run), (False, 123, None, None))

    def test_ci_executes_these_regression_tests(self):
        workflow = yaml.load(
            (ROOT / ".github/workflows/pre-flight.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        steps = workflow["jobs"]["test-approval-queue-tests"]["steps"]
        command = next(step["run"] for step in steps if step["name"] == "Run tests")
        self.assertEqual(
            command,
            'python3 -m unittest discover -s .github/scripts -p "test_test_approval_queue.py"',
        )
        self.assertEqual(
            WORKFLOW["on"]["workflow_call"]["inputs"]["contributor_types_json"]["default"],
            '["internal", "external"]',
        )


if __name__ == "__main__":
    unittest.main()
