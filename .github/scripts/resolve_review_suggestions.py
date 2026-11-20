#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Resolve advisory reviewers from a trusted Git ref and a PR diff."""

import argparse
import json
from pathlib import Path

from codeowners_suggestions import changed_files, read_file_at_ref, suggested_owners


def main() -> None:
    """Write resolved owners to a JSON file for the GitHub request adapter."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules-ref", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    text = read_file_at_ref(args.rules_ref, ".github/CODEOWNERS")
    owners = [] if text is None else suggested_owners(text, changed_files(args.base_sha, args.head_sha))
    args.output.write_text(json.dumps(owners) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
