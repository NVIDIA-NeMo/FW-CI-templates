# Copyright (c) 2026, NVIDIA CORPORATION. All rights reserved.
# Licensed under the Apache License, Version 2.0.

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"


class WorkflowBoundaryTests(unittest.TestCase):
    def text(self, name):
        return (WORKFLOWS / name).read_text(encoding="utf-8")

    def test_analysis_has_no_github_or_oidc_permission(self):
        value = self.text("_isolated_review_analyze.yml")
        self.assertIn("permissions: {}", value)
        self.assertNotIn("id-token: write", value)
        self.assertNotIn("pull-requests: write", value)
        self.assertIn("show_full_output: false", value)
        self.assertIn("claude-code-action/base-action@536f2c32a39763739000b0e1ac69ca2647d97ce9", value)

    def test_publisher_has_no_model_or_checkout(self):
        value = self.text("_isolated_review_publish.yml")
        self.assertIn("id-token: write", value)
        self.assertNotIn("NVIDIA_INFERENCE", value)
        self.assertNotIn("claude-code-action", value)
        self.assertNotIn("actions/checkout", value)

    def test_third_party_actions_use_full_commit_ids(self):
        for path in WORKFLOWS.glob("_isolated_review_*.yml"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "uses:" not in line or line.split("uses:", 1)[1].strip().startswith("./"):
                    continue
                reference = line.split("uses:", 1)[1].strip().split()[0]
                self.assertRegex(reference, r"@[0-9a-f]{40}$", f"{path}: {reference}")

    def test_reference_composition_allows_manual_fork_context(self):
        value = self.text("_claude_review.yml")
        self.assertNotIn("isCrossRepository", value)
        self.assertNotIn("pull_request_target", value)
        self.assertIn("_isolated_review_context.yml", value)
        self.assertIn("_isolated_review_analyze.yml", value)
        self.assertIn("_isolated_review_publish.yml", value)


if __name__ == "__main__":
    unittest.main()
