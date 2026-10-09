# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fetch a verified PR description into a file without logging its contents."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path


def fetch_description(*, repository: str, number: int, head: str, output: Path) -> None:
    """Write the body only when API metadata identifies the expected PR head."""
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or number <= 0
        or not re.fullmatch(r"[0-9a-f]{40}", head)
    ):
        raise ValueError("invalid PR identifiers")
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise ValueError("GitHub token unavailable")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/pulls/{number}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        metadata = json.load(response)
    if (
        metadata["number"] != number
        or metadata["base"]["repo"]["full_name"] != repository
        or metadata["head"]["sha"] != head
    ):
        raise ValueError("PR metadata does not match the checked commit")
    body = metadata["body"]
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise ValueError("invalid PR description")
    output.write_text(body, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--number", type=int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        fetch_description(
            repository=args.repository,
            number=args.number,
            head=args.head,
            output=args.output,
        )
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        print(
            "Guardwords check failed: PR description unavailable or metadata mismatch.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
