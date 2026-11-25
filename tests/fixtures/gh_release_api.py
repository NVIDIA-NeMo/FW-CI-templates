# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Implement the gh api commands used by the release publication tests."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote


def emit(response, projection=None):
    """Return JSON or the requested gh --jq projection."""
    if projection:
        subprocess.run(
            ["jq", "-r", projection], input=json.dumps(response), text=True, check=True
        )
    else:
        print(json.dumps(response))


def main():
    """Handle one request, preserving releases across runner processes."""
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["api"])
    parser.add_argument("endpoint")
    parser.add_argument("--method", "-X", default="GET")
    parser.add_argument("--jq", "-q")
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    state_file = Path(os.environ["RELEASE_API_STATE"])
    state = json.loads(state_file.read_text())
    base = f"repos/{os.environ['GITHUB_REPOSITORY']}/"
    tag = quote(state["tag"], safe="")
    route = f"{args.method} {args.endpoint.removeprefix(base)}"
    assert args.endpoint.startswith(base), args.endpoint
    state["calls"].append(route)
    state_file.write_text(json.dumps(state))
    if route in state["errors"]:
        emit(state["errors"][route])
        return 1
    if route == "POST releases":
        if state["releases"]:
            emit(
                {
                    "message": "Validation Failed",
                    "status": "422",
                    "errors": [
                        {
                            "resource": "Release",
                            "field": "tag_name",
                            "code": "already_exists",
                        }
                    ],
                }
            )
            return 1
        payload = json.loads(args.input.read_text())
        assert payload["tag_name"] == state["tag"]
        response = {
            **payload,
            "id": 101,
            "html_url": "https://example.test/release/101",
        }
        state["releases"].append(response)
        state_file.write_text(json.dumps(state))
        if state["lose_first_response"]:
            print("gh: EOF", file=sys.stderr)
            return 1
    elif route == f"GET releases/tags/{tag}":
        response = state["releases"][0]
    elif route == f"GET commits/refs%2Ftags%2F{tag}":
        response = {"sha": state["tag_sha"]}
    else:
        raise AssertionError(f"Unexpected gh API request: {route}")
    emit(response, args.jq)
    return 0


if __name__ == "__main__":
    sys.exit(main())
