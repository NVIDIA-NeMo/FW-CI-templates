# Copyright (c) 2026, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import importlib.util
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("identify_follow_up_issues.py")
WORKFLOW_PATH = MODULE_PATH.parents[2] / "workflows" / "_community_bot.yml"
SPEC = importlib.util.spec_from_file_location("identify_follow_up_issues", MODULE_PATH)
identify_follow_up_issues = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(identify_follow_up_issues)


class LinkedPullRequestTests(unittest.TestCase):
    def test_only_open_labeled_linked_prs_suppress_an_issue(self):
        content = {
            "closedByPullRequestsReferences": {
                "nodes": [
                    {
                        "url": "https://github.com/NVIDIA-NeMo/NeMo/pull/1",
                        "state": "OPEN",
                        "labels": {"nodes": [{"name": "waiting-on-maintainers"}]},
                    },
                    {
                        "url": "https://github.com/NVIDIA-NeMo/NeMo/pull/2",
                        "state": "OPEN",
                        "labels": {"nodes": [{"name": "waiting-on-customer"}]},
                    },
                    {
                        "url": "https://github.com/NVIDIA-NeMo/NeMo/pull/3",
                        "state": "MERGED",
                        "labels": {"nodes": [{"name": "waiting-on-maintainers"}]},
                    },
                ]
            }
        }

        self.assertEqual(
            identify_follow_up_issues._get_waiting_linked_pr_urls(content),
            ["https://github.com/NVIDIA-NeMo/NeMo/pull/1"],
        )

    @mock.patch.object(identify_follow_up_issues, "remove_label_from_issue", return_value=True)
    @mock.patch.object(identify_follow_up_issues, "add_label_to_issue", return_value=True)
    @mock.patch.object(identify_follow_up_issues, "ensure_label_exists", return_value=True)
    def test_existing_issue_label_is_removed_while_pr_label_is_retained(
        self, ensure_label_exists, add_label, remove_label
    ):
        common = {
            "repo_name": "NeMo",
            "has_deprecated_label": False,
            "has_waiting_on_customer_label": False,
            "target_branch": "main",
            "is_draft": False,
            "classification": "waiting-on-maintainers",
        }
        issue = {
            **common,
            "item_type": "Issue",
            "issue_id": 10,
            "has_maintainers_label": True,
            "needs_attention": False,
        }
        pull_request = {
            **common,
            "item_type": "PullRequest",
            "issue_id": 11,
            "has_maintainers_label": True,
            "needs_attention": True,
        }

        identify_follow_up_issues.update_labels([issue, pull_request], "NVIDIA-NeMo", "token")

        remove_label.assert_called_once_with(
            "NVIDIA-NeMo", "NeMo", 10, "waiting-on-maintainers", "token"
        )
        ensure_label_exists.assert_not_called()
        add_label.assert_not_called()


class HumanActivityTests(unittest.TestCase):
    def test_activity_watermark_ignores_bots_and_service_accounts(self):
        content = {
            "__typename": "PullRequest",
            "createdAt": "2026-08-20T10:00:00Z",
            "comments": {
                "nodes": [
                    {
                        "author": {"__typename": "User", "login": "maintainer"},
                        "createdAt": "2026-08-21T10:00:00Z",
                        "body": "/ok to test abc",
                    },
                    {
                        "author": {"__typename": "Bot", "login": "ci-bot"},
                        "createdAt": "2026-08-24T10:00:00Z",
                        "body": "CI started",
                    },
                    {
                        "author": {
                            "__typename": "User",
                            "login": "svcnemo-autobot",
                        },
                        "createdAt": "2026-08-23T10:00:00Z",
                        "body": "Automated service-account update",
                    },
                ]
            },
            "reviewThreads": {"nodes": []},
            "reviews": {
                "nodes": [
                    {
                        "author": {"__typename": "User", "login": "reviewer"},
                        "submittedAt": "2026-08-22T10:00:00Z",
                        "body": "",
                        "state": "APPROVED",
                    }
                ]
            },
        }

        self.assertEqual(
            identify_follow_up_issues._latest_human_activity(content),
            ("2026-08-22T10:00:00Z", "reviewer"),
        )
        self.assertEqual(
            identify_follow_up_issues._latest_human_activity_date(content),
            "2026-08-22T10:00:00Z",
        )
        self.assertEqual(
            identify_follow_up_issues._collect_non_bot_activity(content),
            [("maintainer", "2026-08-21T10:00:00Z", "/ok to test abc")],
        )

    def test_activity_watermark_falls_back_to_the_item_author(self):
        content = {
            "__typename": "Issue",
            "createdAt": "2026-08-20T10:00:00Z",
            "author": {"__typename": "User", "login": "reporter"},
            "comments": {"nodes": []},
        }

        self.assertEqual(
            identify_follow_up_issues._latest_human_activity(content),
            ("2026-08-20T10:00:00Z", "reporter"),
        )

    def test_activity_watermark_survives_an_item_with_no_dates(self):
        self.assertEqual(
            identify_follow_up_issues._latest_human_activity({"__typename": "Issue"}),
            ("", ""),
        )


def _preflight_script() -> str:
    """Extract the `Check pre-conditions` shell body from the reusable workflow."""
    lines = WORKFLOW_PATH.read_text().splitlines()
    step = next(i for i, line in enumerate(lines) if line.strip() == "id: pre-flight")
    run = next(i for i in range(step, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip()) + 2

    body = []
    for line in lines[run + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    return "\n".join(body)


class CommunityBotPreflightTests(unittest.TestCase):
    """Run the workflow's actor classifier as the runner would: `bash -e`, stubbed curl."""

    MAINTAINER_EVENT = {
        "GH_EVENT_NAME": "issue_comment",
        "IS_VALID_EVENT": "true",
        "IS_FOLLOW_UP_EVENT": "true",
        "ISSUE_AUTHOR": "community-user",
        "AUTHOR_ASSOCIATION": "NONE",
        "ACTOR_ASSOCIATION": "MEMBER",
        "ACTOR_TYPE": "User",
        "ACTOR_LOGIN": "maintainer",
    }

    def run_preflight(self, overrides, curl_status="404"):
        script = _preflight_script()
        script = script.replace("${{ github.actor }}", "$GH_ACTOR")
        script = script.replace("${{ github.event_name }}", "$GH_EVENT_NAME")
        # Any other expression would reach bash unrendered and silently change meaning.
        self.assertNotIn("${{", script)

        env = dict(self.MAINTAINER_EVENT)
        env.update(overrides)
        env.setdefault("GH_ACTOR", env["ACTOR_LOGIN"])

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            curl_log = tmp / "curl.log"
            (bin_dir / "curl").write_text(
                "#!/usr/bin/env bash\n"
                f'echo "${{@: -1}}" >> "{curl_log}"\n'
                f"printf '{curl_status}'\n"
            )
            (bin_dir / "curl").chmod(0o755)

            script_path = tmp / "preflight.sh"
            script_path.write_text(script)
            github_output = tmp / "github_output"
            github_output.touch()

            env.update(
                {
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "GITHUB_OUTPUT": str(github_output),
                    "REPO": "NVIDIA-NeMo/Automodel",
                    "REPO_GITHUB_TOKEN": "t",
                    "NVIDIA_GITHUB_TOKEN": "t",
                    "NVIDIA_NEMO_GITHUB_TOKEN": "t",
                }
            )

            result = subprocess.run(
                ["bash", "-e", str(script_path)],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            outputs = dict(
                line.split("=", 1)
                for line in github_output.read_text().splitlines()
                if "=" in line
            )
            lookups = curl_log.read_text().splitlines() if curl_log.exists() else []

        # Only the lookups for the event actor; the issue-author path runs regardless.
        actor_lookups = [url for url in lookups if url.endswith("/" + env["ACTOR_LOGIN"])]
        return outputs, actor_lookups

    def test_maintainer_comment_clears_the_label(self):
        outputs, _ = self.run_preflight({})

        self.assertEqual(outputs["is_actor_maintainer"], "true")
        self.assertEqual(outputs["actor"], "maintainer")

    def test_service_account_comment_does_not_clear_the_label(self):
        # `svcnemo-autobot` is a `User` account with write access, so neither the
        # account type nor the association rejects it.
        outputs, lookups = self.run_preflight(
            {"ACTOR_LOGIN": "svcnemo-autobot", "ACTOR_TYPE": "User"},
            curl_status="204",
        )

        self.assertEqual(outputs["is_actor_maintainer"], "false")
        self.assertEqual(lookups, [])

    def test_app_bot_comment_does_not_clear_the_label(self):
        for actor, actor_type in (("some-app[bot]", "User"), ("dependabot", "Bot")):
            with self.subTest(actor=actor):
                outputs, _ = self.run_preflight(
                    {"ACTOR_LOGIN": actor, "ACTOR_TYPE": actor_type},
                    curl_status="204",
                )
                self.assertEqual(outputs["is_actor_maintainer"], "false")

    def test_outside_contributor_comment_does_not_clear_the_label(self):
        outputs, lookups = self.run_preflight(
            {"ACTOR_LOGIN": "outsider", "ACTOR_ASSOCIATION": "CONTRIBUTOR"}
        )

        self.assertEqual(outputs["is_actor_maintainer"], "false")
        self.assertEqual(len(lookups), 3)

    def test_org_member_without_association_clears_the_label(self):
        outputs, _ = self.run_preflight(
            {"ACTOR_LOGIN": "nvidian", "ACTOR_ASSOCIATION": "CONTRIBUTOR"},
            curl_status="204",
        )

        self.assertEqual(outputs["is_actor_maintainer"], "true")

    def test_non_follow_up_event_skips_the_actor_lookups(self):
        outputs, lookups = self.run_preflight(
            {
                "GH_EVENT_NAME": "issues",
                "IS_FOLLOW_UP_EVENT": "false",
                "AUTHOR_ASSOCIATION": "MEMBER",
                "ACTOR_ASSOCIATION": "",
                "ACTOR_TYPE": "",
                "ACTOR_LOGIN": "maintainer",
            }
        )

        self.assertEqual(outputs["is_actor_maintainer"], "false")
        self.assertEqual(lookups, [])

    def test_push_test_mode_emits_every_output(self):
        outputs, _ = self.run_preflight({"GH_EVENT_NAME": "push"})

        self.assertEqual(outputs["is_maintainer"], "false")
        self.assertEqual(outputs["is_actor_maintainer"], "false")
        self.assertEqual(outputs["is_follow_up_event"], "false")
        self.assertEqual(outputs["is_valid_event"], "true")


class ScheduledLabelRaceTests(unittest.TestCase):
    def issue(self, **overrides):
        issue = {
            "item_type": "PullRequest",
            "issue_id": 3626,
            "repo_name": "Automodel",
            "classification": "waiting-on-maintainers",
            "needs_attention": True,
            "has_maintainers_label": False,
            "has_waiting_on_customer_label": False,
            "has_deprecated_label": False,
            "target_branch": "main",
            "is_draft": False,
            "issue_author": "community-user",
            "activity_watermark_date": "2026-08-22T06:48:45Z",
            "activity_watermark_login": "community-user",
        }
        issue.update(overrides)
        return issue

    @mock.patch.object(identify_follow_up_issues, "remove_label_from_issue")
    @mock.patch.object(identify_follow_up_issues, "add_label_to_issue", return_value=True)
    @mock.patch.object(identify_follow_up_issues, "ensure_label_exists", return_value=True)
    @mock.patch.object(
        identify_follow_up_issues,
        "fetch_latest_human_activity",
        return_value=("2026-08-24T16:14:08Z", "maintainer"),
    )
    def test_newer_live_activity_suppresses_stale_add(
        self, fetch_activity, ensure_label, add_label, remove_label
    ):
        identify_follow_up_issues.update_labels(
            [self.issue()], "NVIDIA-NeMo", "token"
        )

        fetch_activity.assert_called_once_with(
            "NVIDIA-NeMo", "Automodel", 3626, "token"
        )
        ensure_label.assert_not_called()
        add_label.assert_not_called()
        remove_label.assert_not_called()

    @mock.patch.object(identify_follow_up_issues, "remove_label_from_issue")
    @mock.patch.object(identify_follow_up_issues, "add_label_to_issue", return_value=True)
    @mock.patch.object(identify_follow_up_issues, "ensure_label_exists", return_value=True)
    @mock.patch.object(
        identify_follow_up_issues,
        "fetch_latest_human_activity",
        return_value=("2026-08-22T06:48:45Z", "community-user"),
    )
    def test_unchanged_live_activity_allows_add(
        self, fetch_activity, ensure_label, add_label, remove_label
    ):
        identify_follow_up_issues.update_labels(
            [self.issue()], "NVIDIA-NeMo", "token"
        )

        fetch_activity.assert_called_once()
        ensure_label.assert_called_once()
        add_label.assert_called_once_with(
            "NVIDIA-NeMo",
            "Automodel",
            3626,
            "waiting-on-maintainers",
            "token",
        )
        remove_label.assert_not_called()

    @mock.patch.object(identify_follow_up_issues, "remove_label_from_issue")
    @mock.patch.object(identify_follow_up_issues, "add_label_to_issue", return_value=True)
    @mock.patch.object(identify_follow_up_issues, "ensure_label_exists", return_value=True)
    @mock.patch.object(
        identify_follow_up_issues,
        "fetch_latest_human_activity",
        return_value=("2026-08-24T16:14:08Z", "community-user"),
    )
    def test_same_author_follow_up_still_adds(
        self, fetch_activity, ensure_label, add_label, remove_label
    ):
        # The requester chasing their own issue is exactly when the label is wanted.
        identify_follow_up_issues.update_labels(
            [self.issue()], "NVIDIA-NeMo", "token"
        )

        add_label.assert_called_once()

    @mock.patch.object(identify_follow_up_issues, "remove_label_from_issue", return_value=True)
    @mock.patch.object(identify_follow_up_issues, "add_label_to_issue", return_value=True)
    @mock.patch.object(identify_follow_up_issues, "ensure_label_exists", return_value=True)
    @mock.patch.object(
        identify_follow_up_issues,
        "fetch_latest_human_activity",
        return_value=("2026-08-24T16:14:08Z", "maintainer"),
    )
    def test_suppressed_add_still_reconciles_waiting_on_customer(
        self, fetch_activity, ensure_label, add_label, remove_label
    ):
        identify_follow_up_issues.update_labels(
            [self.issue(has_waiting_on_customer_label=True)], "NVIDIA-NeMo", "token"
        )

        add_label.assert_not_called()
        remove_label.assert_called_once_with(
            "NVIDIA-NeMo",
            "Automodel",
            3626,
            "waiting-on-customer",
            "token",
        )

    @mock.patch.object(identify_follow_up_issues, "remove_label_from_issue")
    @mock.patch.object(identify_follow_up_issues, "add_label_to_issue", return_value=True)
    @mock.patch.object(identify_follow_up_issues, "ensure_label_exists", return_value=True)
    @mock.patch.object(
        identify_follow_up_issues,
        "fetch_latest_human_activity",
        side_effect=RuntimeError("transient API failure"),
    )
    def test_failed_refresh_fails_closed(
        self, fetch_activity, ensure_label, add_label, remove_label
    ):
        identify_follow_up_issues.update_labels(
            [self.issue()], "NVIDIA-NeMo", "token"
        )

        fetch_activity.assert_called_once()
        ensure_label.assert_not_called()
        add_label.assert_not_called()
        remove_label.assert_not_called()


class FollowUpDecisionTests(unittest.TestCase):
    NOW = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
    STALE = "2026-08-11T12:00:00Z"

    def determine(self, **overrides):
        values = {
            "item_type": "Issue",
            "repo_name": "NeMo",
            "classification": "waiting-on-maintainers",
            "last_comment_date": self.STALE,
            "now": self.NOW,
        }
        values.update(overrides)
        return identify_follow_up_issues._determine_needs_attention(**values)

    def test_stale_issue_without_waiting_linked_pr_needs_attention(self):
        self.assertEqual(self.determine(), (True, True, False))

    def test_waiting_linked_pr_suppresses_stale_issue(self):
        self.assertEqual(self.determine(has_waiting_linked_pr=True), (False, True, False))

    def test_non_megatron_stale_approval_keeps_existing_override(self):
        self.assertEqual(
            self.determine(
                item_type="PullRequest",
                classification="waiting-on-author",
                last_approval_date=self.STALE,
                all_reviewers_approved=True,
            ),
            (True, True, True),
        )

    def test_megatron_stale_approval_with_approved_label_uses_override(self):
        self.assertEqual(
            self.determine(
                item_type="PullRequest",
                repo_name="Megatron-LM",
                classification="waiting-on-author",
                last_approval_date=self.STALE,
                all_reviewers_approved=True,
                has_approved_label=True,
            ),
            (True, True, True),
        )

    def test_megatron_stale_approval_without_approved_label_uses_classification(self):
        self.assertEqual(
            self.determine(
                item_type="PullRequest",
                repo_name="Megatron-LM",
                classification="waiting-on-author",
                last_approval_date=self.STALE,
                all_reviewers_approved=True,
                has_approved_label=False,
            ),
            (False, True, False),
        )

    def test_megatron_without_approved_label_can_still_follow_normal_stale_rule(self):
        self.assertEqual(
            self.determine(
                item_type="PullRequest",
                repo_name="Megatron-LM",
                last_approval_date=self.STALE,
                all_reviewers_approved=True,
                has_approved_label=False,
            ),
            (True, True, False),
        )


if __name__ == "__main__":
    unittest.main()
