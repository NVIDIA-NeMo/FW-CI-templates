# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0

"""Exercise file bump steps on generated files in a fresh checkout."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
SHA = "a1" * 20


def run_bump(workflow, workdir, **inputs):
    """Run the workflow's bump step with the supplied environment."""
    manifest = yaml.safe_load((WORKFLOWS / f"_bump_{workflow}.yml").read_text())
    job = next(iter(manifest["jobs"].values()))
    step = next(
        step
        for step in job["steps"]
        if step["name"] in {"Bump file", "Bump Dockerfile"}
    )
    subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", step["run"]],
        cwd=workdir,
        env={**os.environ, "VALUE": SHA, **inputs},
        check=True,
    )


@pytest.mark.parametrize(
    "workflow, pattern, expected_key",
    [
        ("dockerfile", "MCORE_TAG", "MCORE_TAG"),
        ("dockerfile", "MCORE_[T]AG", "MCORE_[T]AG"),
        ("file", r"MCORE_[A-Z]\+", "MCORE_TAG"),
    ],
)
def test_generated_text_file(tmp_path, workflow, pattern, expected_key):
    """Dockerfile and generic updates preserve their respective sed replacements."""
    checkout = tmp_path / "123"
    checkout.mkdir()
    target = checkout / "-generated file"
    target.write_text("untouched\nARG MCORE_TAG=old\n")
    pattern_input = "BUILD_ARG" if workflow == "dockerfile" else "ARGUMENT"
    run_bump(
        workflow,
        tmp_path,
        GITHUB_RUN_ID=checkout.name,
        FILE=target.name,
        **{pattern_input: pattern},
    )
    assert target.read_text() == f"untouched\nARG {expected_key}={SHA}\n"


@pytest.mark.parametrize(
    "selector",
    [
        '."vcs-dependencies"."megatron-lm"[1].ref',
        '.["vcs-" + "dependencies"]["megatron-lm"][1].ref',
        '(."vcs-dependencies"."megatron-lm"[] | select(.name == "stable")).ref',
    ],
)
def test_native_yq_selector(tmp_path, selector):
    """Use each native yq selector to update only the chosen JSON manifest entry."""
    yq = shutil.which("yq")
    assert yq, "mikefarah/yq v4 is required"
    (tmp_path / "yq").symlink_to(yq)
    target = tmp_path / "-new manifest.json"
    entries = [{"name": "next", "ref": "keep"}, {"name": "stable", "ref": "old"}]
    target.write_text(json.dumps({"vcs-dependencies": {"megatron-lm": entries}}))
    run_bump(
        "yamlfile",
        tmp_path,
        RUNNER_TEMP=str(tmp_path),
        YAMLPATH=selector,
        FILE=target.name,
    )
    assert json.loads(target.read_text())["vcs-dependencies"]["megatron-lm"] == [
        {"name": "next", "ref": "keep"},
        {"name": "stable", "ref": SHA},
    ]


def test_toml_arguments(tmp_path):
    """Pass the generated file and indexed TOML key to the CLI as individual arguments."""
    target = tmp_path / "-generated file.toml"
    target.write_text('[package]\nrefs = ["old"]\n')
    toml = tmp_path / "toml"
    toml.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > calls\n')
    toml.chmod(0o755)
    run_bump(
        "tomlfile",
        tmp_path,
        PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        TOMLPATH="package.refs[0]",
        FILE=target.name,
    )
    assert (tmp_path / "calls").read_text().splitlines() == [
        "set",
        f"--toml-path={target.name}",
        "--",
        "package.refs[0]",
        SHA,
    ]
