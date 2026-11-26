# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Run the approval queue script against a local GitHub API and check which token each call uses."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/_test_approval_queue.yml"
JOB = yaml.safe_load(WORKFLOW.read_text())["jobs"]["approve-queue"]
SCRIPT = next(step["run"] for step in JOB["steps"] if step["name"] == "Approve waiting deployments")
CALLER = "NVIDIA-NeMo/caller"


def workflow_run(run_id, pull_request, name="CICD NeMo"):
    """Build the workflow-run fields the queue reads."""
    return {
        "id": run_id,
        "name": name,
        "head_branch": f"pull-request/{pull_request}",
        "display_title": f"PR {pull_request}",
        "created_at": f"2026-11-25T23:0{run_id % 10}:00Z",
        "actor": {"login": "author"},
    }


def github_routes(repository):
    """Map each GET path of one repository to its JSON response."""
    base = f"/repos/{repository}"
    pull = {"base": {"ref": "main"}, "user": {"login": "author"}}
    return {
        f"{base}/actions/runs?status=queued&per_page=100": {"workflow_runs": []},
        f"{base}/actions/runs?status=in_progress&per_page=100": {
            "workflow_runs": [workflow_run(11, 1)]
        },
        f"{base}/actions/runs?status=waiting&per_page=100": {
            "workflow_runs": [workflow_run(12, 2), workflow_run(13, 3, name="Other")]
        },
        f"{base}/pulls/1": pull,
        f"{base}/pulls/2": pull,
        f"{base}/actions/runs/12/pending_deployments": [
            {"id": 5, "environment": {"id": 7, "name": "test"}}
        ],
    }


@pytest.fixture
def github_api():
    """Serve a recording GitHub API on localhost for one test."""
    requests_seen = []
    routes = {}

    class Handler(BaseHTTPRequestHandler):
        """Answer the queue's reads and record every request with its credential."""

        def _record(self, body=None):
            """Keep the method, path, bearer token and JSON body of one request."""
            requests_seen.append(
                (self.command, self.path, self.headers.get("Authorization"), body)
            )

        def _reply(self, status, payload):
            """Send one JSON response."""
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            """Return the routed resource or 404."""
            self._record()
            path = urlsplit(self.path)
            key = path.path + (f"?{path.query}" if path.query else "")
            if key in routes:
                self._reply(200, routes[key])
            else:
                self._reply(404, {"message": "Not Found"})

        def do_POST(self):
            """Accept an approval and record its body."""
            length = int(self.headers.get("Content-Length", "0"))
            self._record(json.loads(self.rfile.read(length) or b"{}"))
            self._reply(200, [])

        def log_message(self, *args):
            """Keep pytest output clean."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", routes, requests_seen
    server.shutdown()
    thread.join()


def run_queue(tmp_path, api_url, target_repository=""):
    """Run one internal/main queue cell of the embedded script."""
    sso = tmp_path / "users_sso.json"
    sso.write_text(json.dumps({"author": {"org_roles": ["NVIDIA-NeMo:Member"]}}))
    env = {
        "HOME": str(tmp_path),
        "NO_PROXY": "*",
        "PATH": os.environ["PATH"],
        "APPROVAL_TOKEN": "pat",
        "APPROVE_COMMENT": "Automatically approved by queue manager",
        "CONTRIBUTOR_TYPE": "internal",
        "EXCLUDED_OTHER_BRANCHES": "main,dev",
        "GITHUB_API_URL": api_url,
        "GITHUB_REPOSITORY": CALLER,
        "INTERNAL_ORG_ROLES": "NVIDIA:Member,NVIDIA-NeMo:Member",
        "MATRIX_BRANCH": "main",
        "MAX_CONCURRENCY_EXTERNAL": "3",
        "MAX_CONCURRENCY_INTERNAL": "3",
        "PRIMARY_BRANCH": "main",
        "READ_TOKEN": "github-token",
        "SSO_USERS_FILE": str(sso),
        "TARGET_DEPLOYMENT_ENVIRONMENT": "test",
        "TARGET_REPOSITORY": target_repository,
        "WORKFLOW_NAME": "CICD NeMo",
    }
    return subprocess.run(
        [sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=60
    )


def test_job_grants_the_read_scopes_its_github_token_needs():
    """The job token can list runs, read pull requests and read pending deployments."""
    assert JOB["permissions"] == {
        "actions": "read",
        "contents": "read",
        "pull-requests": "read",
    }
    step = next(step for step in JOB["steps"] if step["name"] == "Approve waiting deployments")
    assert step["env"]["READ_TOKEN"] == "${{ github.token }}"
    assert step["env"]["APPROVAL_TOKEN"] == "${{ secrets.PAT }}"


@pytest.mark.parametrize("target_repository", ["", CALLER, CALLER.lower()])
def test_reads_use_the_job_token_and_only_the_approval_uses_the_pat(
    tmp_path, github_api, target_repository
):
    """Polling the caller's queue spends no PAT quota; the approval itself stays on the PAT."""
    api_url, routes, requests_seen = github_api
    repository = target_repository or CALLER
    routes.update(github_routes(repository))

    result = run_queue(tmp_path, api_url, target_repository=target_repository)

    assert result.returncode == 0, result.stdout + result.stderr
    pat_requests = [request for request in requests_seen if request[2] == "Bearer pat"]
    assert pat_requests == [
        (
            "POST",
            f"/repos/{repository}/actions/runs/12/pending_deployments",
            "Bearer pat",
            {
                "environment_ids": [7],
                "state": "approved",
                "comment": "Automatically approved by queue manager",
            },
        )
    ]
    reads = [request for request in requests_seen if request[0] == "GET"]
    assert len(reads) == 6
    assert {request[2] for request in reads} == {"Bearer github-token"}


def test_a_cross_repository_target_reads_with_the_pat(tmp_path, github_api):
    """The job token cannot read another repository, so that target keeps the PAT for reads."""
    api_url, routes, requests_seen = github_api
    target = "NVIDIA-NeMo/target"
    routes.update(github_routes(target))

    result = run_queue(tmp_path, api_url, target_repository=target)

    assert result.returncode == 0, result.stdout + result.stderr
    assert {request[2] for request in requests_seen} == {"Bearer pat"}
    assert ("POST", f"/repos/{target}/actions/runs/12/pending_deployments") in {
        request[:2] for request in requests_seen
    }
