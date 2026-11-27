# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0
"""Verify the installation token boundary for GitHub team review requests."""

from pathlib import Path

import yaml


def test_team_token_limits_pr_writes_to_the_calling_repository():
    """Keep organization membership reads and restrict repository access."""
    workflow_path = (
        Path(__file__).resolve().parents[1] / ".github/workflows/_suggest_reviewers.yml"
    )
    workflow = yaml.load(workflow_path.read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["suggest-reviewers"]["steps"]
    (mint,) = [
        step
        for step in steps
        if step.get("uses", "").startswith("actions/create-github-app-token@")
    ]
    assert mint["with"] == {
        "app-id": "${{ inputs.app-id }}",
        "private-key": "${{ secrets.BOT_KEY }}",
        "repositories": "${{ github.event.repository.name }}",
        "permission-members": "read",
        "permission-pull-requests": "write",
    }
