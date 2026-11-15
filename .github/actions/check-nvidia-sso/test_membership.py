# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exercise the actual membership and runner-selection shell steps offline."""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ACTION = Path(__file__).with_name("action.yml")
PREFLIGHT = Path(__file__).resolve().parents[2] / "workflows/_cicd_preflight.yml"


def shell_step(path: Path, step_id: str) -> str:
    """Read a literal run block without adding a YAML dependency to SSO tests."""
    lines = path.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"id: {step_id}")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indentation = len(lines[run]) - len(lines[run].lstrip())
    body = []
    for line in lines[run + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= indentation:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


def execute(script: str, directory: Path, **environment: str) -> dict[str, str]:
    """Execute a native step and read its GitHub output file."""
    output = directory / "output"
    output.write_text("")
    subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        cwd=directory,
        env={**os.environ, "GITHUB_OUTPUT": str(output), **environment},
        check=True,
        capture_output=True,
        text=True,
    )
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


class MembershipTests(unittest.TestCase):
    """Keep trusted identities exact and preserve external runner isolation."""

    def membership(self, username: str, directory: Path) -> dict[str, str]:
        script = shell_step(ACTION, "check-membership")
        for expression, value in {
            "inputs.username": username,
            "inputs.sso_users_filename": "missing-roster.json",
            "steps.download-sso.outputs.sso_file_available": "false",
        }.items():
            script = script.replace("${{ " + expression + " }}", value)
        self.assertNotIn("${{", script)
        return execute(script, directory)

    def test_trusted_bots_use_internal_runners(self) -> None:
        for username in (
            "svcnvidia-nemo-ci",
            "svcnemo-autobot",
            "nemo-automation-bot[bot]",
        ):
            with self.subTest(username=username), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                membership = self.membership(username, directory)
                self.assertEqual(
                    membership,
                    {
                        "is_member": "true",
                        "is_org_member": "true",
                        "user_orgs": "NVIDIA,NVIDIA-NeMo",
                    },
                )
                self.assertEqual(self.route(membership, directory)["runner_prefix"], "internal")

    def test_untrusted_logins_keep_external_runners(self) -> None:
        for username in (
            "external-contributor",
            "nemo-automation-bot",
            "nemo-automation-botb",
            "nemo-automation-boto",
            "nemo-automation-bott",
            "nemo-automation-bot[bot]-other",
            "other-bot[bot]",
        ):
            with self.subTest(username=username), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                membership = self.membership(username, directory)
                self.assertEqual(
                    membership,
                    {"is_member": "false", "is_org_member": "false", "user_orgs": ""},
                )
                self.assertEqual(self.route(membership, directory)["runner_prefix"], "ephemeral")

    def route(self, membership: dict[str, str], directory: Path) -> dict[str, str]:
        script = shell_step(PREFLIGHT, "select_runner")
        for expression, value in {
            "inputs.default_runner_prefix": "internal",
            "inputs.non_nvidia_runner_prefix": "ephemeral",
            "inputs.default_test_data_path": "/internal-data",
            "inputs.non_nvidia_test_data_path": "/external-data",
            "inputs.default_registry": "internal-registry",
            "inputs.non_nvidia_registry": "external-registry",
        }.items():
            script = script.replace("${{ " + expression + " }}", value)
        self.assertNotIn("${{", script)
        return execute(
            script, directory, IS_MEMBER=membership["is_member"], IS_CI_WORKLOAD="false"
        )


if __name__ == "__main__":
    unittest.main()
