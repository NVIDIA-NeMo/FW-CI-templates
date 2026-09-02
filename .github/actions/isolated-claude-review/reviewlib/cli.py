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

"""Command-line parsing and dispatch for isolated review components."""

from __future__ import annotations

import argparse
import sys

from .analyzer import analyze
from .context import build_context
from .contracts import ReviewError
from .mcp import mcp_server
from .publisher import publish, publish_incomplete
from .retrieval import retriever
from .validation import validate_output

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build-context")
    build.add_argument("--repository-dir", required=True)
    build.add_argument("--repository", required=True)
    build.add_argument("--pr-number", type=int, required=True)
    build.add_argument("--review-id", required=True)
    build.add_argument("--review-mode", required=True)
    build.add_argument("--base-sha", required=True)
    build.add_argument("--merge-base-sha", required=True)
    build.add_argument("--head-sha", required=True)
    build.add_argument("--metadata", required=True)
    build.add_argument("--output", required=True)
    build.set_defaults(func=build_context)

    retrieve = commands.add_parser("retrieve")
    retrieve.add_argument("--context", required=True)
    retrieve.add_argument("--audit", required=True)
    retrieve.add_argument(
        "operation",
        choices=[
            "metadata",
            "changed-files",
            "governing-base",
            "tree",
            "read",
            "search",
            "diff",
            "trusted-base-read",
            "trusted-base-search",
            "coverage",
            "diff-hunks",
            "history",
        ],
    )
    retrieve.add_argument("--snapshot", choices=["base", "head"])
    retrieve.add_argument("--path")
    retrieve.add_argument("--query")
    retrieve.add_argument("--offset", type=int, default=0)
    retrieve.add_argument("--limit", type=int, default=100)
    retrieve.add_argument("--byte-limit", type=int, default=64 * 1024)
    retrieve.set_defaults(func=retriever)

    analyze_command = commands.add_parser("analyze")
    analyze_command.add_argument("--context", required=True)
    analyze_command.add_argument("--audit", required=True)
    analyze_command.add_argument("--prompt", required=True)
    analyze_command.add_argument("--schema", required=True)
    analyze_command.add_argument("--output", required=True)
    analyze_command.add_argument("--base-url", required=True)
    analyze_command.add_argument("--model", required=True)
    analyze_command.add_argument("--max-turns", type=int, default=128)
    analyze_command.set_defaults(func=analyze)

    mcp = commands.add_parser("mcp-server")
    mcp.add_argument("--context", required=True)
    mcp.add_argument("--audit", required=True)
    mcp.set_defaults(func=mcp_server)

    validate = commands.add_parser("validate-output")
    validate.add_argument("--context", required=True)
    validate.add_argument("--output", required=True)
    validate.add_argument("--audit", required=True)
    validate.add_argument("--validated-output", required=True)
    validate.set_defaults(func=validate_output)

    publisher = commands.add_parser("publish")
    publisher.add_argument("--context", required=True)
    publisher.add_argument("--output", required=True)
    publisher.add_argument("--audit", required=True)
    publisher.add_argument("--api-url", default="https://api.github.com")
    publisher.set_defaults(func=publish)

    incomplete = commands.add_parser("publish-incomplete")
    incomplete.add_argument("--repository", required=True)
    incomplete.add_argument("--pr-number", type=int, required=True)
    incomplete.add_argument("--base-sha", required=True)
    incomplete.add_argument("--head-sha", required=True)
    incomplete.add_argument("--reason", required=True)
    incomplete.add_argument("--api-url", default="https://api.github.com")
    incomplete.set_defaults(func=publish_incomplete)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        args.func(args)
        return 0
    except ReviewError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
