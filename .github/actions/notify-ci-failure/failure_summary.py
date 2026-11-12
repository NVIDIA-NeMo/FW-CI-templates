# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Build a bounded Slack payload from dependency results, never their outputs."""

from html import escape
import json
import os
import re
import sys
from urllib.parse import urlsplit


RESULTS = {"success", "failure", "skipped", "cancelled"}
TEXT_LIMIT = 2800  # Leave room below Slack's 3000-character section limit.


def bounded(text, limit=TEXT_LIMIT):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_payload(raw_needs, context):
    """Return None when no job failed; reject invalid input without echoing it."""
    try:
        needs = json.loads(raw_needs)
    except (ValueError, TypeError):
        raise ValueError("needs-json must be a JSON object of job results") from None
    if not isinstance(needs, dict):
        raise ValueError("needs-json must be a JSON object of job results")

    jobs = []
    for job_id, job in needs.items():
        if not isinstance(job, dict) or not isinstance(job.get("result"), str):
            raise ValueError("Each dependency must have a valid result")
        result = job["result"]
        if result not in RESULTS:
            raise ValueError("Each dependency must have a valid result")
        jobs.append((job_id, result))
    if not any(result == "failure" for _, result in jobs):
        return None

    server = context.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repository = context.get("GITHUB_REPOSITORY", "")
    run_id = context.get("GITHUB_RUN_ID", "")
    attempt = context.get("GITHUB_RUN_ATTEMPT", "1")
    parsed = urlsplit(server)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or any(char in server for char in "<>|\n\r")
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or not re.fullmatch(r"[1-9][0-9]*", run_id)
        or not re.fullmatch(r"[1-9][0-9]*", attempt)
    ):
        raise ValueError("Invalid GitHub run coordinates")

    # Use plain_text for all metadata: no injected mentions, links or markup.
    workflow = context.get("GITHUB_WORKFLOW", "CI")
    details = "\n".join(
        [
            f"Repository: {repository}",
            f"Ref: {bounded(context.get('GITHUB_REF_NAME', ''), 200)}",
            f"Commit: {bounded(context.get('GITHUB_SHA', ''), 100)}",
            f"Trigger: {bounded(context.get('GITHUB_EVENT_NAME', ''), 100)}",
            f"Run attempt: {attempt}",
        ]
    )
    # Failures first, so very large matrices cannot hide the failing dependency.
    jobs.sort(key=lambda job: (job[1] != "failure", job[0]))
    lines = [f"{bounded(job_id, 200)}: {result}" for job_id, result in jobs]
    shown = []
    for line in lines:
        if len("\n".join(shown + [line])) > TEXT_LIMIT - 100:
            break
        shown.append(line)
    if len(shown) < len(lines):
        shown.append(f"… {len(lines) - len(shown)} more jobs; see the run for details.")
    run_url = f"{server}/{repository}/actions/runs/{run_id}/attempts/{attempt}"
    return {
        # Slack may parse the accessibility fallback even when blocks are plain text.
        "text": escape(
            bounded(f"CI failed: {workflow} ({repository})", 150), quote=False
        ),
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": bounded(f"🚨 CI failed: {workflow}", 150),
                },
            },
            {
                "type": "section",
                "text": {"type": "plain_text", "text": bounded(details)},
            },
            {
                "type": "section",
                "text": {"type": "plain_text", "text": "\n".join(shown)},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"<{run_url}|View failed run>"},
            },
        ],
    }


def main():
    try:
        payload = build_payload(os.environ.get("NEEDS_JSON", ""), os.environ)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    if payload is not None:
        print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
