# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Exercise the copyright workflow's Git comparison and filename transport."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1] / ".github"
ACTION = ROOT / "actions/copyright-checker"
CHECK = yaml.safe_load((ACTION / "action.yml").read_text())["runs"]["steps"][0]["run"]
STEPS = yaml.safe_load((ROOT / "workflows/_copyright_check.yml").read_text())["jobs"][
    "main"
]["steps"]
FIND = next(step["run"] for step in STEPS if step.get("id") == "changed-files")


@pytest.mark.parametrize("mirror", [False, True])
def test_changed_python_paths_arrive_intact(tmp_path, mirror):
    """Check the merged PR or push delta, including renamed and removed files."""
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GITHUB_ACTION_PATH": str(ACTION),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "RUNNER_TEMP": str(tmp_path),
        "IS_PR": str(mirror).lower(),
        "BASE_REF": "release/next.1",
    }

    def git(*args):
        """Run local Git using an isolated identity and configuration."""
        subprocess.run(
            ["git", *args], cwd=tmp_path, env=env, check=True, capture_output=True
        )

    git("init", "-b", "release/next.1")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.org")
    git("remote", "add", "origin", ".")
    for name in ("old.py", "removed.py"):
        (tmp_path / name).write_text(f"# Copyright\n# {name}\n")
    git("add", "--", "*.py")
    git("commit", "-m", "base")
    git("checkout", "-b", "feature")
    git("mv", "old.py", "renamed module.py")
    git("rm", "removed.py")
    names = ["renamed module.py", 'double"quote.py', "-leading.py", "two\nlines.py"]
    for name in names[1:]:
        (tmp_path / name).write_text("# Copyright\n")
    git("add", "--", "*.py")
    git("commit", "-m", "feature")
    if mirror:
        git("checkout", "release/next.1")
        (tmp_path / "base only.py").write_text("base only\n")
        git("add", "--", "base only.py")
        git("commit", "-m", "update base")
        git("checkout", "feature")
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", FIND], cwd=tmp_path, env=env, check=True
    )
    file_list = Path(
        Path(env["GITHUB_OUTPUT"]).read_text().strip().removeprefix("file_list=")
    )
    assert set(file_list.read_bytes().split(b"\0")) == {b"", *map(os.fsencode, names)}
    env.update(CHANGED_FILES_LIST=str(file_list), CHANGED_FILES="")
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", CHECK], cwd=tmp_path, env=env, check=True
    )
    (tmp_path / "renamed module.py").write_text("missing header\n")
    checked = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", CHECK],
        cwd=tmp_path,
        env=env,
        capture_output=True,
    )
    assert checked.returncode != 0
    assert b"renamed module.py" in checked.stdout


def test_legacy_quoted_file_list(tmp_path):
    """Continue accepting shell-quoted filenames from direct composite action callers."""
    (tmp_path / "one module.py").write_text("missing header\n")
    env = {
        **os.environ,
        "GITHUB_ACTION_PATH": str(ACTION),
        "CHANGED_FILES_LIST": "",
        "CHANGED_FILES": "'one module.py'",
    }
    checked = subprocess.run(
        ["bash", "-c", CHECK], cwd=tmp_path, env=env, capture_output=True
    )
    assert checked.returncode != 0
    assert b"one module.py" in checked.stdout


def test_bash_quoted_file_list(tmp_path):
    """Accept literal paths from an existing caller's Bash printf %q formatter."""
    names = [
        "one module.py",
        "-option.py",
        "quote'and\tspace.py",
        'double"quote.py',
        "two\nlines.py",
        "café.py",
        "cost$5.py",
        "literal\\backslash.py",
    ]
    for name in names:
        (tmp_path / name).write_text("missing header\n")
    quoted = subprocess.check_output(
        ["bash", "-c", 'printf "%q " "$@"', "_", *names],
        env={**os.environ, "LC_ALL": "C"},
        text=True,
    )
    env = {
        **os.environ,
        "GITHUB_ACTION_PATH": str(ACTION),
        "CHANGED_FILES_LIST": "",
        "CHANGED_FILES": quoted,
    }
    checked = subprocess.run(
        ["bash", "-c", CHECK], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert checked.returncode == 1
    for name in names:
        assert f"Header missing: {name!r}" in checked.stdout


def test_legacy_filename_globs_preserve_quoted_patterns(tmp_path):
    """Expand unquoted patterns and keep quoted or escaped pattern characters literal."""
    for name in [
        "example a.py",
        "example b.py",
        "module[1].py",
        "module1.py",
        "bracket*.py",
        "brackets.py",
    ]:
        (tmp_path / name).write_text("missing header\n")
    env = {
        **os.environ,
        "GITHUB_ACTION_PATH": str(ACTION),
        "CHANGED_FILES_LIST": "",
        "CHANGED_FILES": r"""'example '*.py "module[1].py" bracket\*.py""",
    }
    checked = subprocess.run(
        ["bash", "-c", CHECK], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert checked.returncode == 1
    assert "4 file(s) missing copyright header" in checked.stdout
    for name in ("example a.py", "example b.py", "module[1].py", "bracket*.py"):
        assert f"Header missing: {name!r}" in checked.stdout
