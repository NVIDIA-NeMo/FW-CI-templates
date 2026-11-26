# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0
"""Verify catalog credential selection in the reusable Guardwords workflow."""

import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github/workflows/_guardwords_check.yml"
)
DOCUMENT = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
STEPS = DOCUMENT["jobs"]["guardwords-check"]["steps"]


@pytest.mark.parametrize(
    ("deploy_key", "app_id", "app_key", "method"),
    [
        (False, False, False, None),
        (False, False, True, None),
        (False, True, False, None),
        (False, True, True, "app"),
        (True, False, False, "ssh"),
        (True, False, True, "ssh"),
        (True, True, False, "ssh"),
        (True, True, True, "ssh"),
    ],
)
def test_select_guardwords_credential(tmp_path, deploy_key, app_id, app_key, method):
    """Run the workflow selector with each combination of credential inputs."""
    select = STEPS[0]
    assert select["id"] == "auth"
    assert select["env"] == {
        "HAS_DEPLOY_KEY": "${{ secrets.GUARDWORDS_DEPLOY_KEY != '' }}",
        "HAS_APP": "${{ inputs.bot-app-id != '' && secrets.BOT_KEY != '' }}",
    }
    output = tmp_path / "output"
    output.touch()
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", select["run"]],
        env={
            "HAS_DEPLOY_KEY": str(deploy_key).lower(),
            "HAS_APP": str(app_id and app_key).lower(),
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == (1 if method is None else 0)
    assert output.read_text() == (f"method={method}\n" if method else "")


def test_guardwords_checkout_credentials():
    """Check the public inputs and bind each raw key only to its consuming action."""
    contract = DOCUMENT["on"]["workflow_call"]
    assert contract["inputs"]["bot-app-id"]["required"] == "false"
    for name in ("BOT_KEY", "GUARDWORDS_DEPLOY_KEY"):
        assert contract["secrets"][name]["required"] == "false"

    mint = next(step for step in STEPS if step.get("id") == "guardwords-token")
    assert mint["if"] == "steps.auth.outputs.method == 'app'"
    assert mint["with"] == {
        "app-id": "${{ inputs.bot-app-id }}",
        "private-key": "${{ secrets.BOT_KEY }}",
        "owner": "NVIDIA-NeMo",
        "repositories": "Guardwords",
        "permission-contents": "read",
    }
    checkouts = [
        step["with"]
        for step in STEPS
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    catalog = next(
        options
        for options in checkouts
        if options.get("repository") == "NVIDIA-NeMo/Guardwords"
    )
    assert catalog["ref"] == "main"
    assert catalog["ssh-key"] == "${{ secrets.GUARDWORDS_DEPLOY_KEY }}"
    assert (
        catalog["token"]
        == "${{ steps.auth.outputs.method == 'ssh' && github.token || steps.guardwords-token.outputs.token }}"
    )
    assert all(options["persist-credentials"] == "false" for options in checkouts)
    assert all(
        "ssh-key" not in options and "token" not in options
        for options in checkouts
        if options is not catalog
    )
