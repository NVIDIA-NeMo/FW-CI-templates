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

"""Exercise build environment delivery through the reusable release workflows."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"


def load_workflow(name):
    # BaseLoader preserves the GitHub Actions key "on" as a string.
    return yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)


class BuildWheelEnvironmentTests(unittest.TestCase):
    def test_release_forwards_declared_environment_input(self):
        release = load_workflow("_release_library.yml")
        wheel = load_workflow("_build_test_publish_wheel.yml")
        for workflow in (release, wheel):
            declaration = workflow["on"]["workflow_call"]["inputs"]["build-wheel-env"]
            self.assertEqual(declaration["default"], "{}")
            self.assertEqual(declaration["type"], "string")
        wheel_job = next(
            job
            for job in release["jobs"].values()
            if "_build_test_publish_wheel.yml" in job.get("uses", "")
        )
        self.assertEqual(
            wheel_job["with"]["build-wheel-env"], "${{ inputs.build-wheel-env }}"
        )

    def run_environment_step(self, values):
        steps = load_workflow("_build_test_publish_wheel.yml")["jobs"]["build-wheel"][
            "steps"
        ]
        environment_step = next(
            step for step in steps if step.get("name") == "Set build environment"
        )
        build_step = next(step for step in steps if step.get("id") == "build")
        self.assertLess(steps.index(environment_step), steps.index(build_step))
        self.assertEqual(
            environment_step["env"]["BUILD_WHEEL_ENV"], "${{ inputs.build-wheel-env }}"
        )
        with tempfile.TemporaryDirectory() as directory:
            github_env = Path(directory) / "environment"
            result = subprocess.run(
                ["bash", "-e", "-c", environment_step["run"]],
                env={
                    **os.environ,
                    "BUILD_WHEEL_ENV": json.dumps(values),
                    "GITHUB_ENV": str(github_env),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            return result, github_env.read_text() if github_env.exists() else ""

    def test_cuda_major_reaches_github_environment(self):
        result, exported = self.run_environment_step({"TMS_CUDA_MAJOR": "13"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(exported, "TMS_CUDA_MAJOR=13\n")

    def test_default_has_no_environment_effect(self):
        result, exported = self.run_environment_step({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(exported, "")

    def test_invalid_input_does_not_export_variables(self):
        for values in (
            [],
            {"bad-name": "13"},
            {"TMS_CUDA_MAJOR": 13},
            {"X": "13\nOTHER=value"},
        ):
            with self.subTest(values=values):
                result, exported = self.run_environment_step(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(exported, "")


if __name__ == "__main__":
    unittest.main()
