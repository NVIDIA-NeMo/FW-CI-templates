# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0

"""Check the boundary between Actions expressions and executable scripts."""

import json
import re
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined


ROOT = Path(__file__).resolve().parents[1]
EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
RUNNER_IDENTIFIERS = {"github.run_id", "github.repository"}


class ShellInputTests(unittest.TestCase):
    """Ensure shared automation treats caller-controlled values as data."""

    def check_scripts(self, directory, render):
        """Inspect scripts in workflows, composite actions, and the retry action."""
        workflows = (directory / "workflows").glob("*")
        actions = (directory / "actions").rglob("action.*")
        manifests = sorted(
            path for path in (*workflows, *actions) if path.suffix in {".yaml", ".yml"}
        )
        self.assertTrue(manifests)
        for path in manifests:
            manifest = yaml.load(render(path.read_text()), Loader=yaml.BaseLoader)
            steps = [
                step
                for job in manifest.get("jobs", {}).values()
                for step in job.get("steps", [])
            ]
            steps.extend(manifest.get("runs", {}).get("steps", []))
            for step in steps:
                scripts = [step.get("run", "")]
                if step.get("uses", "").split("@")[0] == "nick-fields/retry":
                    scripts.extend(
                        step["with"].get(key, "")
                        for key in ("command", "on_retry_command")
                    )
                for script in scripts:
                    expressions = {
                        match.strip() for match in EXPRESSION.findall(script)
                    } - RUNNER_IDENTIFIERS
                    with self.subTest(
                        path=str(path.relative_to(ROOT)), step=step.get("name")
                    ):
                        self.assertFalse(
                            expressions,
                            f"Pass dynamic values through env or arguments: {expressions}",
                        )

    def test_shared_workflows_and_actions(self):
        """Check the runnable workflow and composite action manifests."""
        self.check_scripts(ROOT / ".github", str)

    def test_generated_workflows_and_actions(self):
        """Render the starter repository before checking its executable scripts."""
        template = ROOT / "github_repo_template"
        environment = Environment(undefined=StrictUndefined)
        context = {}

        def render(source):
            """Render a template using the default Cookiecutter configuration."""
            return environment.from_string(source).render(cookiecutter=context)

        for key, value in json.loads(
            (template / "cookiecutter.json").read_text()
        ).items():
            context[key] = render(value)
        self.check_scripts(
            template / "{{cookiecutter.project_slug}}" / ".github", render
        )
