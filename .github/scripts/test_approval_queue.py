# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exercise the actual approval-queue workflow with a fake GitHub API."""

import ast
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.load(
    (ROOT / ".github/workflows/_test_approval_queue.yml").read_text(),
    Loader=yaml.BaseLoader,
)
STEP = next(
    step
    for step in WORKFLOW["jobs"]["approve-queue"]["steps"]
    if step["name"] == "Approve waiting deployments"
)
SCRIPT = STEP["run"]
TRUSTED_BOTS = {"svcnemo-autobot", "svcnvidia-nemo-ci"}
ROLES = {"NVIDIA:Member", "NVIDIA-NeMo:Member"}


def classifier(users, roles=ROLES):
    """Load the workflow's function verbatim, without executing its API calls."""
    function = next(
        node
        for node in ast.parse(SCRIPT).body
        if isinstance(node, ast.FunctionDef) and node.name == "is_internal_contributor"
    )
    scope = {"sso_users": users, "internal_org_roles": roles}
    exec(
        compile(
            ast.Module(body=[function], type_ignores=[]), "queue-classifier", "exec"
        ),
        scope,
    )
    return scope["is_internal_contributor"]


class ApprovalQueueTests(unittest.TestCase):
    def test_service_accounts_match_existing_sso_policy(self):
        action = yaml.load(
            (ROOT / ".github/actions/check-nvidia-sso/action.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        membership = next(
            step for step in action["runs"]["steps"] if step["id"] == "check-membership"
        )["run"]
        check = classifier({})
        for login in TRUSTED_BOTS:
            with self.subTest(login=login):
                self.assertIn(f'"$USERNAME" == "{login}"', membership)
                self.assertTrue(check({"user": {"login": login}}))

    def test_humans_still_require_allowed_sso_roles(self):
        users = {
            "member": {"org_roles": ["NVIDIA:Member"]},
            "nemo-member": {"org_roles": ["NVIDIA-NeMo:Member"]},
            "sso-only": {"org_roles": []},
            "other-org": {"org_roles": ["Other:Member"]},
        }
        check = classifier(users)
        for login, expected in (
            ("member", True),
            ("nemo-member", True),
            ("sso-only", False),
            ("other-org", False),
            ("unlisted", False),
        ):
            with self.subTest(login=login):
                self.assertEqual(check({"user": {"login": login}}), expected)
        self.assertFalse(
            classifier(users, {"Other:Member"})({"user": {"login": "member"}})
        )

    def test_lookalikes_and_missing_authors_remain_external(self):
        check = classifier({})
        for login in (
            "",
            "svcnemo-autobot-evil",
            "svcnvidia-nemo-ci-evil",
            "unknown[bot]",
        ):
            with self.subTest(login=login):
                self.assertFalse(check({"user": {"login": login}}))
        self.assertFalse(check({}))

    def run_queue(
        self,
        login="svcnemo-autobot",
        contributor_type="internal",
        base="main",
        queue_branch="main",
        active=0,
        environment="test",
        actor="external-user",
        head_branch="pull-request/42",
    ):
        posts = []
        run = {
            "id": 123,
            "name": "CICD NeMo",
            "display_title": "fixture",
            "head_branch": head_branch,
            "actor": {"login": actor},
            "created_at": "2026-01-01T00:00:00Z",
        }
        pr = {"user": {"login": login}, "base": {"ref": base}}
        deployment = {"id": 789, "environment": {"id": 456, "name": environment}}

        class Response:
            status_code = 200
            headers = {}
            text = "fixture"

            def __init__(self, value):
                self.value = value

            def raise_for_status(self):
                pass

            def json(self):
                return self.value

        class Session:
            def __init__(self):
                self.headers = {}

            def get(self, url, timeout):
                endpoint = url.split("/repos/NVIDIA-NeMo/Speech/", 1)[1]
                if endpoint == "pulls/42":
                    return Response(pr)
                if endpoint == "actions/runs/123/pending_deployments":
                    return Response([deployment])
                if endpoint.startswith("actions/runs?status="):
                    status = endpoint.split("status=", 1)[1].split("&", 1)[0]
                    return Response(
                        {
                            "workflow_runs": (
                                [run] * active
                                if status == "queued"
                                else [run] if status == "waiting" else []
                            )
                        }
                    )
                raise AssertionError(endpoint)

            def post(self, url, json, timeout):
                posts.append((url, json))
                return Response({})

        requests = types.ModuleType("requests")
        requests.Session = Session
        with tempfile.TemporaryDirectory() as temp:
            sso = Path(temp) / "users.json"
            sso.write_text("{}")
            env = {
                "TARGET_REPOSITORY": "NVIDIA-NeMo/Speech",
                "WORKFLOW_NAME": "CICD NeMo",
                "MATRIX_BRANCH": queue_branch,
                "TARGET_DEPLOYMENT_ENVIRONMENT": "test",
                "CONTRIBUTOR_TYPE": contributor_type,
                "APPROVAL_TOKEN": "fixture-token",
                "APPROVE_COMMENT": "fixture approval",
                "PRIMARY_BRANCH": "main",
                "MAX_CONCURRENCY_INTERNAL": "1",
                "MAX_CONCURRENCY_EXTERNAL": "1",
                "INTERNAL_ORG_ROLES": ",".join(sorted(ROLES)),
                "EXCLUDED_OTHER_BRANCHES": "main,dev",
                "SSO_USERS_FILE": str(sso),
            }
            with patch.dict(os.environ, env), patch.dict(
                "sys.modules", {"requests": requests}
            ), contextlib.redirect_stdout(io.StringIO()):
                try:
                    exec(compile(SCRIPT, "queue-workflow", "exec"), {})
                except SystemExit as error:
                    self.assertEqual(error.code, 0)
        return posts

    def test_bots_use_internal_slots_not_external_slots(self):
        for login in TRUSTED_BOTS:
            with self.subTest(login=login):
                posts = self.run_queue(login=login)
                self.assertEqual(len(posts), 1)
                self.assertEqual(posts[0][1]["environment_ids"], [456])
                self.assertEqual(posts[0][1]["state"], "approved")
                self.assertEqual(
                    self.run_queue(login=login, contributor_type="external"), []
                )

    def test_actor_cannot_promote_external_pr_author(self):
        self.assertEqual(
            self.run_queue(login="external-user", actor="svcnemo-autobot"), []
        )
        self.assertEqual(
            len(self.run_queue(login="external-user", contributor_type="external")), 1
        )

    def test_trusted_bots_do_not_bypass_branch_filters(self):
        self.assertEqual(self.run_queue(base="dev"), [])
        self.assertEqual(self.run_queue(base="dev", queue_branch="others"), [])
        self.assertEqual(self.run_queue(base="main", queue_branch="others"), [])
        self.assertEqual(len(self.run_queue(base="release", queue_branch="others")), 1)
        self.assertEqual(self.run_queue(head_branch="main"), [])

    def test_trusted_bots_do_not_bypass_concurrency_or_environment(self):
        self.assertEqual(self.run_queue(active=1), [])
        self.assertEqual(self.run_queue(environment="main"), [])


if __name__ == "__main__":
    unittest.main()
