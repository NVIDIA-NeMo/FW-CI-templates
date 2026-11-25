# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0
"""Check how reusable workflows resolve helpers from the templates repository."""

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
TEMPLATES = "nvidia-nemo/fw-ci-templates"


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("_*.yml")), ids=lambda p: p.name)
def test_template_helpers_follow_workflow_revision(path):
    """Only an explicit workflow_ref may select a different templates revision."""
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    contract = workflow["on"]["workflow_call"] or {}
    override = contract.get("inputs", {}).get("workflow_ref")
    expected_ref = "${{ job.workflow_sha }}"
    if override is not None:
        assert override.get("default", "") == ""
        expected_ref = "${{ inputs.workflow_ref || job.workflow_sha }}"

    for job in workflow["jobs"].values():
        assert not job.get("uses", "").lower().startswith(TEMPLATES + "/")
        for step in job.get("steps", []):
            options = step.get("with", {})
            repository = options.get("repository", "").lower()
            if repository in {TEMPLATES, "${{ job.workflow_repository }}"}:
                assert options.get("ref") == expected_ref, step["name"]
            uses = step.get("uses", "")
            assert not uses.lower().startswith(TEMPLATES + "/")
            if uses.startswith("$/"):
                action = ROOT / uses[2:]
                assert any(
                    (action / name).is_file() for name in ("action.yml", "action.yaml")
                ), uses
