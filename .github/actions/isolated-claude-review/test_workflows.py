# Copyright (c) 2026, NVIDIA CORPORATION. All rights reserved.
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

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
FULL_HEADER = "Licensed under the Apache License, Version 2.0 (the \"License\")"


class WorkflowBoundaryTests(unittest.TestCase):
    def text(self, name):
        return (WORKFLOWS / name).read_text(encoding="utf-8")

    def test_analysis_has_no_github_or_oidc_permission(self):
        value = self.text("_isolated_review_analyze.yml")
        self.assertIn("permissions: {}", value)
        self.assertNotIn("id-token: write", value)
        self.assertNotIn("pull-requests: write", value)
        self.assertNotIn("claude-code-action", value)
        self.assertNotIn("uses: anthropics/", value)
        self.assertIn("review_components.py analyze", value)
        self.assertIn("--max-turns 128", value)
        self.assertIn("governing_base", value)
        self.assertIn("trusted_base_read", value)
        self.assertIn("retrieval-audit.jsonl", value)
        self.assertIn("ANTHROPIC_API_KEY: ${{ secrets.NVIDIA_INFERENCE_KEY }}", value)

    def test_publisher_has_no_model_or_checkout(self):
        value = self.text("_isolated_review_publish.yml")
        self.assertIn("id-token: write", value)
        self.assertNotIn("NVIDIA_INFERENCE", value)
        self.assertNotIn("claude-code-action", value)
        self.assertNotIn("actions/checkout", value)
        self.assertIn("publish-incomplete", value)

    def test_third_party_actions_use_full_commit_ids(self):
        for path in WORKFLOWS.glob("_isolated_review_*.yml"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "uses:" not in line or line.split("uses:", 1)[1].strip().startswith("./"):
                    continue
                reference = line.split("uses:", 1)[1].strip().split()[0]
                self.assertRegex(reference, r"@[0-9a-f]{40}$", f"{path}: {reference}")

    def test_workflows_have_full_license_headers_and_budgets(self):
        for name in ("_isolated_review_context.yml", "_isolated_review_analyze.yml", "_isolated_review_publish.yml"):
            value = self.text(name)
            self.assertIn(FULL_HEADER, value)
            self.assertRegex(value, r"timeout-minutes: [1-9]")


    def test_trusted_tool_artifact_round_trip_layout_is_executable(self):
        composition = self.text("_claude_review.yml")
        publisher = self.text("_isolated_review_publish.yml")
        self.assertIn("mkdir trusted-tools", composition)
        self.assertIn("cp components/.github/actions/isolated-claude-review/review_components.py trusted-tools/", composition)
        self.assertIn("cp -R components/.github/actions/isolated-claude-review/reviewlib trusted-tools/", composition)
        self.assertIn("path: trusted-tools", composition)
        self.assertIn("python3 trusted-tools/review_components.py publish", publisher)
        self.assertNotIn("trusted-tools/components/.github", publisher)

        component_root = ROOT / ".github/actions/isolated-claude-review"
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            download = Path(directory) / "download" / "trusted-tools"
            staging.mkdir()
            shutil.copy2(component_root / "review_components.py", staging)
            shutil.copy2(component_root / "review-output-v1.schema.json", staging)
            shutil.copytree(component_root / "reviewlib", staging / "reviewlib")
            shutil.copytree(staging, download)
            result = subprocess.run(
                [sys.executable, str(download / "review_components.py"), "--help"],
                cwd=download.parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_review_modules_have_explicit_dependencies(self):
        component_root = ROOT / ".github/actions/isolated-claude-review"
        self.assertLess(len((component_root / "review_components.py").read_text().splitlines()), 60)
        for path in [component_root / "review_components.py", *(component_root / "reviewlib").glob("*.py")]:
            self.assertNotIn("import *", path.read_text(), str(path))
        contracts = (component_root / "reviewlib/contracts.py").read_text()
        self.assertNotIn("import subprocess", contracts)
        self.assertNotIn("import urllib", contracts)
        self.assertIn("class ChangedFile", contracts)
        self.assertIn("class ReviewOutput", contracts)
        self.assertIn("from .utils import", (component_root / "reviewlib/context.py").read_text())
        self.assertIn("from .contracts import (", (component_root / "reviewlib/retrieval.py").read_text())

    def test_reference_composition_supports_manual_and_automatic_modes(self):
        value = self.text("_claude_review.yml")
        self.assertIn("review_mode", value)
        self.assertIn("inputs.review_mode == 'manual'", value)
        self.assertIn("inputs.review_mode == 'automatic'", value)
        self.assertIn("concurrency:", value)
        self.assertIn("cancel-in-progress: true", value)
        self.assertIn("content=eyes", value)
        self.assertIn("publish-incomplete", value)
        self.assertNotIn("startsWith(github.event.comment.body", value)
        self.assertIn("^[[0-9a-f]{40}$".replace("[[", "["), value)
        self.assertIn("isCrossRepository", value)
        self.assertIn("_isolated_review_context.yml", value)
        self.assertIn("_isolated_review_analyze.yml", value)
        self.assertIn("_isolated_review_publish.yml", value)

    def test_reference_prompt_is_tool_compatible_and_model_cannot_publish(self):
        value = self.text("_isolated_review_analyze.yml")
        self.assertIn("immutable pull-request snapshot", value)
        self.assertIn("untrusted data", value)
        self.assertIn("Return only the requested structured JSON", value)
        self.assertNotIn("gh pr comment", value)
        self.assertNotIn("github_inline_comment", value)
        self.assertNotIn("--allowedTools", value)
        self.assertNotIn("Bash", value)


if __name__ == "__main__":
    unittest.main()
