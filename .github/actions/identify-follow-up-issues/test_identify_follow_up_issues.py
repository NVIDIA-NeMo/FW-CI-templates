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
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("identify_follow_up_issues.py")
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
