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

"""Shared limits, typed wire records, and fail-closed helpers."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, NotRequired, TypedDict

SCHEMA_VERSION = "1.0"


MANIFEST_VERSION = "1.0"


SHA_RE = re.compile(r"^[0-9a-f]{40}$")


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


SAFE_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


MAX_CHANGED_FILES = 2_000


MAX_TREE_ENTRIES = 50_000


MAX_FILE_BYTES = 512 * 1024


MAX_CONTEXT_BYTES = 64 * 1024 * 1024


MAX_DIFF_BYTES = 4 * 1024 * 1024


MAX_DIFF_HUNKS = 20_000


MAX_OUTPUT_BYTES = 256 * 1024


MAX_INLINE_FINDINGS = 50


MAX_GENERAL_FINDINGS = 20


MAX_COMMENT_BODY_BYTES = 16 * 1024


MAX_SUMMARY_BYTES = 32 * 1024


MAX_REVIEW_BODY_BYTES = 60 * 1024


MAX_REVIEW_PAYLOAD_BYTES = 512 * 1024


RETRIEVER_MAX_CALLS = 256


RETRIEVER_MAX_BYTES = 16 * 1024 * 1024


RETRIEVER_MAX_RESULTS = 1_000


RETRIEVER_MAX_SECONDS = 600


CLAUDE_OIDC_AUDIENCE = "claude-code-github-action"


CLAUDE_TOKEN_EXCHANGE_URL = "https://api.anthropic.com/api/github/github-app-token-exchange"


GITHUB_API_VERSION = "2022-11-28"


class ReviewError(RuntimeError):
    """A fail-closed review component error."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_json(path: Path, *, max_bytes: int = MAX_OUTPUT_BYTES) -> Any:
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise ReviewError(f"JSON input exceeds {max_bytes} bytes: {path}")
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReviewError(f"Invalid JSON in {path}: {error}") from error


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def require_sha(name: str, value: Any) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise ReviewError(f"{name} must be a lowercase 40-character SHA")
    return value


def require_repository(value: Any) -> str:
    if not isinstance(value, str) or not SAFE_REPOSITORY_RE.fullmatch(value):
        raise ReviewError("repository must be an owner/name value")
    return value


def normalize_repo_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ReviewError("repository path is empty or contains a control character")
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("/") or any(part in ("", ".", "..") for part in path.parts):
        raise ReviewError(f"unsafe repository path: {value!r}")
    return path.as_posix()


def contained_path(root: Path, relative: str) -> Path:
    relative = normalize_repo_path(relative)
    root = root.resolve()
    candidate = (root / relative).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ReviewError(f"path escapes context root: {relative!r}") from error
    return candidate


def git(repo: Path, *args: str, input_bytes: bytes | None = None, max_bytes: int | None = None) -> bytes:
    command = ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "-c", "core.autocrlf=false", *args]
    result = subprocess.run(command, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace")[-2_000:]
        raise ReviewError(f"git command failed ({args[0]}): {stderr}")
    if max_bytes is not None and len(result.stdout) > max_bytes:
        raise ReviewError(f"git output exceeds {max_bytes} bytes ({args[0]})")
    return result.stdout


class TreeEntry(TypedDict):
    mode: str
    type: str
    oid: str
    size: int | None


class SnapshotInfo(TypedDict):
    available: bool
    size: NotRequired[int]
    sha256: NotRequired[str]
    reason: NotRequired[str]


class ChangedFile(TypedDict):
    path: str
    status: str
    old_path: NotRequired[str]


class DiffHunk(TypedDict):
    path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int


class RetrievalCoverage(TypedDict):
    changed_files_reviewed: int
    changed_files_total: int
    diff_complete: bool
    changed_files_list_complete: bool
    governing_base_complete: bool
    trusted_base_tree_complete: bool
    governing_base_missing: list[str]
    governing_base_unread: list[str]
    complete: bool


class InlineFinding(TypedDict):
    path: str
    line: int
    body: str


class GeneralFinding(TypedDict):
    body: str


class ReviewOutput(TypedDict):
    schema_version: str
    repository: str
    pull_request: int
    head_sha: str
    context_digest: str
    status: str
    summary: str
    inline_findings: list[InlineFinding]
    general_findings: list[GeneralFinding]
    coverage: RetrievalCoverage
