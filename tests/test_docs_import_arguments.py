# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0

"""Exercise argument handling for docs dependencies and the import check."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/check-imports"


def record_command(path):
    """Create an offline executable that records its working directory and argv."""
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['COMMAND_LOG'], 'a') as stream:\n"
        "    print(json.dumps([os.getcwd(), *sys.argv[1:]]), file=stream)\n"
    )
    path.chmod(0o755)


@pytest.mark.parametrize(
    "selector,flags",
    [
        ("local-python", []),
        ("./local python", []),
        ("'./local python'", []),
        ('"./local python" -I -X dev', ["-I", "-X", "dev"]),
        ("local-python -W 'ignore::UserWarning'", ["-W", "ignore::UserWarning"]),
    ],
)
def test_import_interpreter_and_startup_arguments(tmp_path, selector, flags):
    """Keep interpreter arguments and paths intact as both action steps change directory."""
    record_command(tmp_path / "local python")
    (tmp_path / "local-python").symlink_to(tmp_path / "local python")
    manifest = yaml.safe_load((ACTION / "action.yml").read_text())
    log = tmp_path / "commands"
    for step in manifest["runs"]["steps"]:
        subprocess.run(
            ["bash", "-eu", "-o", "pipefail", "-c", step["run"]],
            cwd=tmp_path,
            env={
                **os.environ,
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "COMMAND_LOG": str(log),
                "GITHUB_ACTION_PATH": str(ACTION),
                "PACKAGE_NAME": "sample.package",
                "PYTHON_BINARY": selector,
            },
            check=True,
        )
    assert [json.loads(line) for line in log.read_text().splitlines()] == [
        [
            str(tmp_path),
            *flags,
            "-m",
            "pip",
            "install",
            "-r",
            str(ACTION / "requirements.txt"),
        ],
        [
            "/tmp",
            *flags,
            str(ACTION / "check_imports.py"),
            "--package-name",
            "sample.package",
        ],
    ]


@pytest.mark.parametrize(
    "no_extras,expected",
    [
        ("", []),
        ("--no-extra cu12", ["--no-extra", "cu12"]),
        (
            "--no-extra cu12\n--no-extra=other",
            ["--no-extra", "cu12", "--no-extra=other"],
        ),
        (
            "--no-extra a.b\t--no-extra other-test",
            ["--no-extra", "a.b", "--no-extra", "other-test"],
        ),
    ],
)
def test_docs_excluded_extras(tmp_path, no_extras, expected):
    """Keep the caller's space, tab, and multiline flag lists as separate arguments."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/_build_docs.yml").read_text())
    install = next(
        step
        for step in workflow["jobs"]["main"]["steps"]
        if step["name"] == "Install dependencies"
    )
    record_command(tmp_path / "uv")
    log = tmp_path / "commands"
    subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", install["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "COMMAND_LOG": str(log),
            "NO_EXTRAS": no_extras,
            "SYNC_ALL": "true",
            "REQUIREMENTS_FILE": "",
        },
        check=True,
    )
    assert [json.loads(line) for line in log.read_text().splitlines()] == [
        [str(tmp_path), "venv"],
        [str(tmp_path), "sync", "--all-extras", "--all-groups", *expected],
    ]
