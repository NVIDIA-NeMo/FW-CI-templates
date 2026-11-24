"""Check the public wheel workflow contract and execute its token upload script."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
WRAPPER = yaml.safe_load((WORKFLOWS / "_build_test_publish_wheel.yml").read_text())
BUILDER = yaml.safe_load((WORKFLOWS / "_build_test_wheel.yml").read_text())


def test_existing_call_contract_is_preserved():
    """Keep every existing input, default, secret, and output available."""
    baseline = json.loads(
        (ROOT / "tests/fixtures/wheel_workflow_contract.json").read_text()
    )
    actual = WRAPPER[True]["workflow_call"]
    for kind in ("inputs", "secrets"):
        for name, definition in baseline[kind].items():
            assert actual[kind][name] == definition
    assert baseline["outputs"].keys() <= actual["outputs"].keys()
    assert (
        actual["inputs"]["repository-url"]["default"]
        == "https://upload.pypi.org/legacy/"
    )


def test_builder_inputs_and_metadata_are_forwarded():
    """Preserve build options and expose the same metadata to old callers."""
    call = WRAPPER["jobs"]["build-test-wheel"]
    for name, definition in BUILDER[True]["workflow_call"]["inputs"].items():
        assert call["with"][name] == "${{ inputs." + name + " }}"
        assert WRAPPER[True]["workflow_call"]["inputs"][name] == definition
    for name in ("version", "pypi-name", "artifact-name"):
        assert WRAPPER[True]["workflow_call"]["outputs"][name]["value"] == (
            "${{ jobs.build-test-wheel.outputs." + name + " }}"
        )


def test_builder_cannot_publish_and_uses_legacy_artifact_name():
    """Require read-only builds with only the optional build credential."""
    assert BUILDER["permissions"] == {"contents": "read"}
    assert set(BUILDER[True]["workflow_call"]["secrets"]) == {"GH_TOKEN"}
    assert set(BUILDER["jobs"]) == {"build-wheel", "test-wheel", "validate-wheel"}
    artifact = "pip-wheel-${{ github.run_id }}-${{ inputs.python-package || 'none' }}"
    assert BUILDER["jobs"]["build-wheel"]["outputs"]["artifact-name"] == artifact
    for job in BUILDER["jobs"].values():
        for step in job["steps"]:
            if step.get("uses", "").startswith(
                ("actions/upload-artifact@", "actions/download-artifact@")
            ):
                assert step["with"]["name"] == artifact


def test_publish_waits_for_successful_builder():
    """Failed tests or validation must prevent the wrapper from uploading."""
    publish = WRAPPER["jobs"]["publish-wheel"]
    assert publish["needs"] == ["build-test-wheel"]
    assert "if" not in publish
    assert WRAPPER["jobs"]["notify"]["needs"] == ["build-test-wheel", "publish-wheel"]


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    "index", ["https://upload.pypi.org/legacy/", "https://test.pypi.org/legacy/"]
)
def test_token_upload_and_dry_run(tmp_path, dry_run, index):
    """Execute the upload script with a recorder in place of Python and Twine."""
    result, calls = run_upload(tmp_path, dry_run, index, "test-token")
    assert result.returncode == 0, result.stderr
    uploads = [call for call in calls if call[:3] == ["-m", "twine", "upload"]]
    assert len(uploads) == (0 if dry_run else 1)
    if uploads:
        upload = uploads[0]
        assert upload[upload.index("--repository-url") + 1] == index
        assert upload[upload.index("-u") + 1] == "__token__"
        assert upload[upload.index("-p") + 1] == "test-token"
        assert "--skip-existing" in upload
        assert upload[-1] == "dist/example-1.0-py3-none-any.whl"


def test_missing_token_prevents_upload(tmp_path):
    """Retain the existing requirement for an explicit token on the legacy path."""
    result, calls = run_upload(tmp_path, False, "https://test.pypi.org/legacy/", "")
    assert result.returncode != 0
    assert "TWINE_PASSWORD unset" in result.stdout
    assert not any(call[:3] == ["-m", "twine", "upload"] for call in calls)


def run_upload(tmp_path, dry_run, index, password):
    """Run the actual shell script without installing packages or contacting an index."""
    command = tmp_path / "python3"
    command.write_text(
        "#!/usr/bin/env python\n"
        "import json, os, sys\n"
        'with open(os.environ["CALLS"], "a") as stream:\n'
        '    stream.write(json.dumps(sys.argv[1:]) + "\\n")\n'
    )
    command.chmod(0o755)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/example-1.0-py3-none-any.whl").touch()
    step = next(
        step for step in WRAPPER["jobs"]["publish-wheel"]["steps"] if "run" in step
    )
    script = tmp_path / "upload.sh"
    script.write_text(step["run"])
    calls_file = tmp_path / "calls.jsonl"
    env = dict(
        os.environ,
        PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
        CALLS=str(calls_file),
        DRY_RUN=str(dry_run).lower(),
        TWINE_USERNAME="__token__",
        TWINE_PASSWORD=password,
        TWINE_REPOSITORY_URL=index,
    )
    result = subprocess.run(
        ["bash", "-euo", "pipefail", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result, [json.loads(line) for line in calls_file.read_text().splitlines()]
