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

"""Model-free OIDC exchange, revision verification, and atomic publication."""

from __future__ import annotations

from .contracts import *  # noqa: F403
from .context import validate_manifest
from .validation import validate_output_data

def github_request(method: str, url: str, token: str, payload: Any | None = None, *, timeout: int = 20) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "fw-ci-isolated-review",
    }
    data = None if payload is None else canonical_json(payload)
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_OUTPUT_BYTES + 1)
    except urllib.error.HTTPError as error:
        body = error.read(4_000).decode("utf-8", "replace")
        raise ReviewError(f"GitHub API returned HTTP {error.code}: {body}") from error
    if len(body) > MAX_OUTPUT_BYTES:
        raise ReviewError("GitHub API response exceeds limit")
    return json.loads(body) if body else None


def get_oidc_token() -> str:
    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not request_url or not request_token:
        raise ReviewError("GitHub OIDC request variables are unavailable")
    separator = "&" if "?" in request_url else "?"
    url = request_url + separator + urllib.parse.urlencode({"audience": CLAUDE_OIDC_AUDIENCE})
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {request_token}"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            value = json.loads(response.read(MAX_OUTPUT_BYTES + 1))
    except (urllib.error.URLError, json.JSONDecodeError) as error:
        raise ReviewError(f"unable to obtain publisher identity token: {error}") from error
    token = value.get("value") if isinstance(value, dict) else None
    if not isinstance(token, str) or not token:
        raise ReviewError("OIDC response did not contain a token")
    return token


def exchange_publisher_token(oidc_token: str) -> str:
    # This is intentionally publisher-only. It mirrors the exact exchange used by the
    # reviewed Claude Code Action revision and is covered by mocked contract tests.
    request = urllib.request.Request(
        CLAUDE_TOKEN_EXCHANGE_URL,
        data=canonical_json({"permissions": {"contents": "read", "pull_requests": "write", "issues": "write"}}),
        headers={"Authorization": f"Bearer {oidc_token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            value = json.loads(response.read(MAX_OUTPUT_BYTES + 1))
    except urllib.error.HTTPError as error:
        body = error.read(4_000).decode("utf-8", "replace")
        raise ReviewError(f"publisher token exchange returned HTTP {error.code}: {body}") from error
    except (urllib.error.URLError, json.JSONDecodeError) as error:
        raise ReviewError(f"publisher token exchange failed: {error}") from error
    token = value.get("token") or value.get("app_token") if isinstance(value, dict) else None
    if not isinstance(token, str) or not token:
        raise ReviewError("publisher token exchange did not return a token")
    print(f"::add-mask::{token}")
    return token


def live_revision(api_url: str, repository: str, pr_number: int, token: str) -> tuple[str, str]:
    value = github_request("GET", f"{api_url}/repos/{repository}/pulls/{pr_number}", token)
    if not isinstance(value, dict):
        raise ReviewError("pull request response is not an object")
    return require_sha("live base SHA", value.get("base", {}).get("sha")), require_sha(
        "live head SHA", value.get("head", {}).get("sha")
    )


def fixed_result_body(output: dict[str, Any]) -> str:
    if output["status"] == "incomplete":
        return f"Review incomplete: {output['failure_reason']}"
    body_parts = [output["summary"].strip()]
    for finding in output["general_findings"]:
        body_parts.append(f"- **{finding['severity']} / {finding['category']}**: {finding['body']}")
    body = "\n\n".join(part for part in body_parts if part)
    if output["clean_review"]:
        body = "LGTM — no actionable findings were identified."
    body = body or "Review completed."
    if len(body.encode("utf-8")) > MAX_REVIEW_BODY_BYTES:
        raise ReviewError("composed review body exceeds the GitHub comment limit")
    return body


def review_payload(output: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "commit_id": manifest["head_sha"],
        "event": "COMMENT",
        "body": fixed_result_body(output),
        "comments": [
            {"path": item["path"], "side": item["side"], "line": item["line"], "body": item["body"]}
            for item in output["inline_findings"]
        ] if output["status"] == "complete" else [],
    }
    if len(canonical_json(payload)) > MAX_REVIEW_PAYLOAD_BYTES:
        raise ReviewError("composed review request exceeds the publication limit")
    return payload


def publish(args: argparse.Namespace) -> None:
    root = Path(args.context).resolve()
    manifest = validate_manifest(root)
    changed = read_json(root / "changed-files.json", max_bytes=2 * 1024 * 1024)
    audited = retrieval_coverage(root, Path(args.audit).resolve())
    output = validate_output_document(
        read_json(Path(args.output), max_bytes=MAX_OUTPUT_BYTES), manifest, changed, audited
    )
    payload = review_payload(output, manifest)  # Preflight every body and inline comment before authentication.
    api_url = args.api_url.rstrip("/")
    oidc_token = get_oidc_token()
    token = exchange_publisher_token(oidc_token)
    try:
        live_base, live_head = live_revision(api_url, manifest["repository"], manifest["pull_request"], token)
        if live_base != manifest["base_sha"] or live_head != manifest["head_sha"]:
            github_request(
                "POST",
                f"{api_url}/repos/{manifest['repository']}/issues/{manifest['pull_request']}/comments",
                token,
                {"body": "Review incomplete: the pull request revision changed before publication."},
            )
            print(json.dumps({"published": "stale"}))
            return
        github_request(
            "POST",
            f"{api_url}/repos/{manifest['repository']}/pulls/{manifest['pull_request']}/reviews",
            token,
            payload,
        )
        print(json.dumps({"published": "complete" if output["status"] == "complete" else "incomplete"}))
    finally:
        try:
            github_request("DELETE", f"{api_url}/installation/token", token)
        except ReviewError as error:
            print(f"warning: publisher token revocation failed: {error}", file=sys.stderr)


def publish_incomplete(args: argparse.Namespace) -> None:
    repository = require_repository(args.repository)
    if args.pr_number <= 0:
        raise ReviewError("pr_number must be positive")
    base_sha = require_sha("base_sha", args.base_sha)
    head_sha = require_sha("head_sha", args.head_sha)
    reason = validate_text("reason", args.reason, 1_000)
    body = f"Review incomplete: {reason}"
    if len(body.encode("utf-8")) > MAX_REVIEW_BODY_BYTES:
        raise ReviewError("incomplete status exceeds the GitHub comment limit")
    api_url = args.api_url.rstrip("/")
    token = exchange_publisher_token(get_oidc_token())
    try:
        live_base, live_head = live_revision(api_url, repository, args.pr_number, token)
        if live_base != base_sha or live_head != head_sha:
            body = "Review incomplete: the pull request revision changed before publication."
        github_request("POST", f"{api_url}/repos/{repository}/issues/{args.pr_number}/comments", token, {"body": body})
        print(json.dumps({"published": "incomplete"}))
    finally:
        try:
            github_request("DELETE", f"{api_url}/installation/token", token)
        except ReviewError as error:
            print(f"warning: publisher token revocation failed: {error}", file=sys.stderr)
