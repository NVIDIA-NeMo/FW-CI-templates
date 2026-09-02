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

"""Shared review limits, errors, and typed wire records."""

from __future__ import annotations

import re
from typing import NotRequired, TypedDict

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
SEARCH_UNAVAILABLE_SAMPLE_LIMIT = 100
CLAUDE_OIDC_AUDIENCE = "claude-code-github-action"
CLAUDE_TOKEN_EXCHANGE_URL = "https://api.anthropic.com/api/github/github-app-token-exchange"
GITHUB_API_VERSION = "2022-11-28"


class ReviewError(RuntimeError):
    """A fail-closed review component error."""


class TreeEntry(TypedDict):
    mode: str
    type: str
    oid: str
    size: int | None


class SnapshotInfo(TypedDict, total=False):
    available: bool
    reason: str | None
    mode: str
    type: str
    oid: str
    size: int | None
    sha256: str
    symlink_target: str
    symlink_chain: list[str]
    symlink_error: str


class ChangedStatus(TypedDict):
    status: str
    old_path: str | None
    new_path: str | None


class LineRanges(TypedDict):
    LEFT: list[list[int]]
    RIGHT: list[list[int]]


class ChangedFile(ChangedStatus):
    base: SnapshotInfo
    head: SnapshotInfo
    line_ranges: LineRanges


class DiffHunk(TypedDict):
    file_index: int
    left_start: int
    left_count: int
    right_start: int
    right_count: int


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


class ModelCoverage(TypedDict):
    changed_files_reviewed: int
    changed_files_total: int
    diff_complete: bool
    notes: str


class InlineFinding(TypedDict):
    path: str
    side: str
    line: int
    severity: str
    category: str
    body: str


class GeneralFinding(TypedDict):
    severity: str
    category: str
    body: str


class ReviewOutput(TypedDict):
    schema_version: str
    repository: str
    pull_request: int
    review_id: str
    review_mode: str
    base_sha: str
    merge_base_sha: str
    head_sha: str
    context_digest: str
    status: str
    coverage: ModelCoverage
    inline_findings: list[InlineFinding]
    general_findings: list[GeneralFinding]
    summary: str
    clean_review: bool
    failure_reason: NotRequired[str | None]
