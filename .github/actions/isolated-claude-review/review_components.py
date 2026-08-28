#!/usr/bin/env python3
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

"""Build, inspect, validate, and publish isolated pull-request reviews."""

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
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"
MANIFEST_VERSION = "1.0"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SAFE_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
MAX_CHANGED_FILES = 2_000
MAX_TREE_ENTRIES = 20_000
MAX_FILE_BYTES = 512 * 1024
MAX_CONTEXT_BYTES = 8 * 1024 * 1024
MAX_DIFF_BYTES = 4 * 1024 * 1024
MAX_DIFF_HUNKS = 20_000
MAX_OUTPUT_BYTES = 256 * 1024
MAX_INLINE_FINDINGS = 50
MAX_GENERAL_FINDINGS = 20
MAX_COMMENT_BODY_BYTES = 16 * 1024
MAX_SUMMARY_BYTES = 32 * 1024
RETRIEVER_MAX_CALLS = 256
RETRIEVER_MAX_BYTES = 4 * 1024 * 1024
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


def parse_ls_tree(repo: Path, sha: str, limit: int) -> tuple[dict[str, dict[str, Any]], bool]:
    raw = git(repo, "ls-tree", "-r", "-z", "-l", sha)
    entries: dict[str, dict[str, Any]] = {}
    truncated = False
    for record in raw.split(b"\0"):
        if not record:
            continue
        if len(entries) >= limit:
            truncated = True
            break
        header, raw_path = record.split(b"\t", 1)
        parts = header.decode("ascii").split()
        if len(parts) != 4:
            raise ReviewError("unexpected git tree entry")
        mode, object_type, oid, size_text = parts
        path = raw_path.decode("utf-8", "surrogateescape")
        try:
            normalized = normalize_repo_path(path)
        except ReviewError:
            # Unsafe names are represented but are never materialized or retrievable.
            normalized = path
        size = None if size_text == "-" else int(size_text)
        entries[normalized] = {"mode": mode, "type": object_type, "oid": oid, "size": size}
    return entries, truncated


def parse_name_status(raw: bytes) -> list[dict[str, Any]]:
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    changed: list[dict[str, Any]] = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("ascii")
        index += 1
        code = status[0]
        if code in ("R", "C"):
            if index + 1 >= len(fields):
                raise ReviewError("truncated rename/copy status")
            old_path = fields[index].decode("utf-8", "surrogateescape")
            new_path = fields[index + 1].decode("utf-8", "surrogateescape")
            index += 2
        else:
            if index >= len(fields):
                raise ReviewError("truncated changed-file status")
            path = fields[index].decode("utf-8", "surrogateescape")
            index += 1
            old_path = path if code != "A" else None
            new_path = path if code != "D" else None
        changed.append({"status": status, "old_path": old_path, "new_path": new_path})
    return changed


def parse_hunks(diff: bytes, changed: list[dict[str, Any]]) -> list[dict[str, int]]:
    hunks: list[dict[str, int]] = []
    file_index = -1
    for raw_line in diff.decode("utf-8", "replace").splitlines():
        if raw_line.startswith("diff --git "):
            file_index += 1
            continue
        match = HUNK_RE.match(raw_line)
        if match and 0 <= file_index < len(changed):
            left_start = int(match.group(1))
            left_count = int(match.group(2) or "1")
            right_start = int(match.group(3))
            right_count = int(match.group(4) or "1")
            hunks.append(
                {
                    "file_index": file_index,
                    "left_start": left_start,
                    "left_count": left_count,
                    "right_start": right_start,
                    "right_count": right_count,
                }
            )
    return hunks


def ranges_for_file(hunks: Iterable[dict[str, int]], file_index: int, side: str) -> list[list[int]]:
    result = []
    for hunk in hunks:
        if hunk["file_index"] != file_index:
            continue
        start = hunk["left_start"] if side == "LEFT" else hunk["right_start"]
        count = hunk["left_count"] if side == "LEFT" else hunk["right_count"]
        if count:
            result.append([start, start + count - 1])
    return result


def is_binary_blob(repo: Path, oid: str, size: int | None) -> bool:
    if size == 0:
        return False
    sample = git(repo, "cat-file", "blob", oid)
    return b"\0" in sample[:8_000]


def materialize_snapshot(
    repo: Path,
    output: Path,
    snapshot: str,
    path: str,
    tree_entry: dict[str, Any] | None,
    budget: list[int],
) -> dict[str, Any]:
    result: dict[str, Any] = {"available": False, "reason": "missing"}
    if tree_entry is None:
        return result
    result.update(tree_entry)
    mode = tree_entry["mode"]
    size = tree_entry["size"]
    if mode == "120000":
        result["reason"] = "symlink"
        return result
    if mode == "160000" or tree_entry["type"] == "commit":
        result["reason"] = "submodule"
        return result
    if tree_entry["type"] != "blob" or not mode.startswith("100"):
        result["reason"] = "special"
        return result
    try:
        safe_path = normalize_repo_path(path)
    except ReviewError:
        result["reason"] = "unsafe_path"
        return result
    if size is None or size > MAX_FILE_BYTES:
        result["reason"] = "large_file"
        return result
    if budget[0] + size > MAX_CONTEXT_BYTES:
        result["reason"] = "context_budget"
        return result
    data = git(repo, "cat-file", "blob", tree_entry["oid"])
    if len(data) != size:
        raise ReviewError(f"blob size changed for {path}")
    if b"\0" in data[:8_000]:
        result["reason"] = "binary"
        result["sha256"] = sha256_bytes(data)
        return result
    target = contained_path(output / "snapshots" / snapshot, safe_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    budget[0] += len(data)
    result.update({"available": True, "reason": None, "sha256": sha256_bytes(data)})
    return result


def validate_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    manifest = read_json(manifest_path, max_bytes=2 * 1024 * 1024)
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ReviewError("unsupported context manifest")
    expected_digest = manifest.get("context_digest")
    require_sha("base_sha", manifest.get("base_sha"))
    require_sha("merge_base_sha", manifest.get("merge_base_sha"))
    require_sha("head_sha", manifest.get("head_sha"))
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ReviewError("invalid context digest")
    unsigned = dict(manifest)
    unsigned.pop("context_digest", None)
    if sha256_bytes(canonical_json(unsigned)) != expected_digest:
        raise ReviewError("context manifest digest mismatch")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ReviewError("context artifact table is missing")
    for relative, expected in artifacts.items():
        path = contained_path(root, relative)
        if not path.is_file() or path.is_symlink():
            raise ReviewError(f"context artifact is missing or unsafe: {relative}")
        if sha256_bytes(path.read_bytes()) != expected:
            raise ReviewError(f"context artifact digest mismatch: {relative}")
    for record in read_json(root / "changed-files.json", max_bytes=2 * 1024 * 1024):
        for snapshot in ("base", "head"):
            item = record[snapshot]
            if not item.get("available"):
                continue
            relative_path = record["old_path"] if snapshot == "base" else record["new_path"]
            data = contained_path(root / "snapshots" / snapshot, relative_path).read_bytes()
            if len(data) != item["size"] or sha256_bytes(data) != item["sha256"]:
                raise ReviewError(f"snapshot artifact digest mismatch: {relative_path}")
    manifest["_context_root"] = str(root.resolve())
    return manifest


load_context = validate_manifest


def build_context(args: argparse.Namespace) -> None:
    repository = require_repository(args.repository)
    base_sha = require_sha("base_sha", args.base_sha)
    merge_base_sha = require_sha("merge_base_sha", args.merge_base_sha)
    head_sha = require_sha("head_sha", args.head_sha)
    if not isinstance(args.pr_number, int) or args.pr_number <= 0:
        raise ReviewError("pr_number must be positive")
    if not args.review_id or len(args.review_id.encode()) > 200:
        raise ReviewError("review_id is required and bounded")
    if args.review_mode not in {"manual", "automatic", "light", "strict"}:
        raise ReviewError("unsupported review_mode")

    repo = Path(args.repository_dir).resolve()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    for name, sha in (("base", base_sha), ("merge-base", merge_base_sha), ("head", head_sha)):
        git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
    actual_merge_base = git(repo, "merge-base", base_sha, head_sha).decode("ascii").strip()
    if actual_merge_base != merge_base_sha:
        raise ReviewError("MERGE_BASE_SHA does not match BASE_SHA and HEAD_SHA")

    raw_metadata = read_json(Path(args.metadata), max_bytes=256 * 1024)
    if not isinstance(raw_metadata, dict):
        raise ReviewError("metadata must be a JSON object")
    metadata = {
        "repository": repository,
        "pull_request": args.pr_number,
        "review_id": args.review_id,
        "review_mode": args.review_mode,
        "base_sha": base_sha,
        "merge_base_sha": merge_base_sha,
        "head_sha": head_sha,
        "title": str(raw_metadata.get("title") or "")[:2_000],
        "body": str(raw_metadata.get("body") or "")[:16_000],
        "author": str(raw_metadata.get("author") or "")[:200],
        "head_repository": require_repository(raw_metadata.get("head_repository")),
        "is_cross_repository": raw_metadata.get("is_cross_repository"),
        "labels": sorted({str(label)[:200] for label in raw_metadata.get("labels", []) if isinstance(label, str)})[:100],
    }
    if type(metadata["is_cross_repository"]) is not bool:
        raise ReviewError("is_cross_repository must be a JSON boolean")

    base_tree, base_tree_truncated = parse_ls_tree(repo, base_sha, MAX_TREE_ENTRIES)
    head_tree, head_tree_truncated = parse_ls_tree(repo, head_sha, MAX_TREE_ENTRIES)
    name_status = git(repo, "diff", "--name-status", "-z", "--find-renames", merge_base_sha, head_sha)
    changed = parse_name_status(name_status)
    configured_max_files = min(getattr(args, "max_files", MAX_CHANGED_FILES), MAX_CHANGED_FILES)
    if len(changed) > configured_max_files:
        raise ReviewError(f"review context exceeds configured limits ({configured_max_files} files)")
    expected_changed_files = raw_metadata.get("changed_files")
    if type(expected_changed_files) is not int or expected_changed_files < 0:
        raise ReviewError("changed_files metadata must be a non-negative integer")
    if expected_changed_files != len(changed):
        raise ReviewError(f"changed-file count mismatch: expected {expected_changed_files}, generated {len(changed)}")

    full_diff = git(repo, "diff", "--no-ext-diff", "--binary", "--find-renames", "--unified=3", merge_base_sha, head_sha)
    range_diff = git(repo, "diff", "--no-ext-diff", "--find-renames", "--unified=0", merge_base_sha, head_sha)
    hunks = parse_hunks(range_diff, changed)
    if len(hunks) > MAX_DIFF_HUNKS:
        raise ReviewError(f"diff hunk count exceeds {MAX_DIFF_HUNKS}")
    configured_max_diff_bytes = min(getattr(args, "max_diff_bytes", MAX_DIFF_BYTES), MAX_DIFF_BYTES)
    if len(full_diff) > configured_max_diff_bytes:
        raise ReviewError(f"review context exceeds configured limits ({configured_max_diff_bytes} diff bytes)")
    diff_truncated = len(full_diff) > MAX_DIFF_BYTES
    stored_diff = full_diff[:MAX_DIFF_BYTES]
    (output / "review.diff").write_bytes(stored_diff)

    budget = [0]
    changed_records = []
    for index, entry in enumerate(changed):
        old_path = entry["old_path"]
        new_path = entry["new_path"]
        safe_old = None
        safe_new = None
        try:
            safe_old = normalize_repo_path(old_path) if old_path is not None else None
            safe_new = normalize_repo_path(new_path) if new_path is not None else None
        except ReviewError:
            pass
        base_entry = base_tree.get(safe_old) if safe_old is not None else None
        head_entry = head_tree.get(safe_new) if safe_new is not None else None
        base_snapshot = materialize_snapshot(repo, output, "base", safe_old or old_path or "unsafe", base_entry, budget)
        head_snapshot = materialize_snapshot(repo, output, "head", safe_new or new_path or "unsafe", head_entry, budget)
        changed_records.append(
            {
                **entry,
                "base": base_snapshot,
                "head": head_snapshot,
                "line_ranges": {"LEFT": ranges_for_file(hunks, index, "LEFT"), "RIGHT": ranges_for_file(hunks, index, "RIGHT")},
            }
        )

    write_json(output / "metadata.json", metadata)
    write_json(output / "changed-files.json", changed_records)
    write_json(output / "diff-hunks.json", hunks)
    write_json(output / "trees.json", {"base": base_tree, "head": head_tree})
    tools_dir = output / "tools"
    tools_dir.mkdir()
    source = Path(__file__).resolve()
    schema = source.with_name("review-output-v1.schema.json")
    (tools_dir / source.name).write_bytes(source.read_bytes())
    (tools_dir / schema.name).write_bytes(schema.read_bytes())

    artifact_paths = [
        "metadata.json",
        "changed-files.json",
        "diff-hunks.json",
        "trees.json",
        "review.diff",
        "tools/review_components.py",
        "tools/review-output-v1.schema.json",
    ]
    artifact_paths.extend(
        sorted(str(path.relative_to(output)) for path in (output / "snapshots").rglob("*") if path.is_file())
        if (output / "snapshots").exists()
        else []
    )
    artifacts = {relative: sha256_bytes((output / relative).read_bytes()) for relative in artifact_paths}
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "repository": repository,
        "pull_request": args.pr_number,
        "review_id": args.review_id,
        "review_mode": args.review_mode,
        "base_sha": base_sha,
        "merge_base_sha": merge_base_sha,
        "head_sha": head_sha,
        "changed_files": len(changed_records),
        "tree_entries": {"base": len(base_tree), "head": len(head_tree)},
        "coverage": {
            "base_tree_truncated": base_tree_truncated,
            "head_tree_truncated": head_tree_truncated,
            "diff_truncated": diff_truncated,
            "full_diff_bytes": len(full_diff),
            "stored_diff_bytes": len(stored_diff),
            "materialized_bytes": budget[0],
            "diff_hunks": len(hunks),
        },
        "artifacts": artifacts,
    }
    manifest["context_digest"] = sha256_bytes(canonical_json(manifest))
    write_json(output / "manifest.json", manifest)
    print(json.dumps({"context_digest": manifest["context_digest"], "changed_files": len(changed_records)}))

@dataclass
class RetrievalBudget:
    started: float
    calls: int = 0
    bytes: int = 0
    results: int = 0

    def charge(self, *, output_bytes: int, results: int) -> None:
        if time.monotonic() - self.started > RETRIEVER_MAX_SECONDS:
            raise ReviewError("retrieval time budget exhausted")
        self.calls += 1
        self.bytes += output_bytes
        self.results += results
        if self.calls > RETRIEVER_MAX_CALLS:
            raise ReviewError("retrieval call budget exhausted")
        if self.bytes > RETRIEVER_MAX_BYTES:
            raise ReviewError("retrieval byte budget exhausted")
        if self.results > RETRIEVER_MAX_RESULTS:
            raise ReviewError("retrieval result budget exhausted")


def audit_record(audit: Path, operation: str, request: dict[str, Any], outcome: str, output_bytes: int, results: int) -> None:
    record = {
        "operation": operation,
        "request": request,
        "outcome": outcome,
        "output_bytes": output_bytes,
        "results": results,
    }
    with audit.open("ab") as stream:
        stream.write(canonical_json(record) + b"\n")


def retriever(args: argparse.Namespace) -> None:
    root = Path(args.context).resolve()
    manifest = validate_manifest(root)
    changed = read_json(root / "changed-files.json", max_bytes=2 * 1024 * 1024)
    metadata = read_json(root / "metadata.json", max_bytes=256 * 1024)
    trees = read_json(root / "trees.json", max_bytes=16 * 1024 * 1024)
    hunks = read_json(root / "diff-hunks.json", max_bytes=4 * 1024 * 1024)
    audit = Path(args.audit).resolve()
    audit_root = root.resolve()
    try:
        audit.relative_to(audit_root)
    except ValueError as error:
        raise ReviewError("audit path escapes context root") from error
    prior_calls = prior_bytes = prior_results = 0
    if audit.exists():
        for line in audit.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            prior_calls += 1
            prior_bytes += int(record.get("output_bytes", 0))
            prior_results += int(record.get("results", 0))
    budget = RetrievalBudget(started=time.monotonic(), calls=prior_calls, bytes=prior_bytes, results=prior_results)

    def emit(operation: str, request: dict[str, Any], value: Any, results: int) -> None:
        data = canonical_json(value) + b"\n"
        budget.charge(output_bytes=len(data), results=results)
        audit_record(audit, operation, request, "ok", len(data), results)
        sys.stdout.buffer.write(data)

    operation = args.operation
    request = {key: value for key, value in vars(args).items() if key not in {"func", "context", "audit"} and value is not None}
    try:
        if operation == "metadata":
            emit(operation, request, {**metadata, "context_digest": manifest["context_digest"]}, 1)
        elif operation == "changed-files":
            offset = max(args.offset, 0)
            limit = min(max(args.limit, 1), 200)
            page = changed[offset : offset + limit]
            emit(
                operation,
                request,
                {"entries": page, "next_offset": offset + len(page) if offset + len(page) < len(changed) else None},
                len(page),
            )
        elif operation == "tree":
            if args.snapshot not in {"base", "head"}:
                raise ReviewError("tree snapshot must be base or head")
            prefix = "" if args.path is None else normalize_repo_path(args.path).rstrip("/") + "/"
            entries = [
                {"path": path, **entry}
                for path, entry in sorted(trees[args.snapshot].items())
                if path == prefix.rstrip("/") or path.startswith(prefix)
            ]
            offset = max(args.offset, 0)
            limit = min(max(args.limit, 1), 200)
            page = entries[offset : offset + limit]
            emit(
                operation,
                request,
                {"entries": page, "next_offset": offset + len(page) if offset + len(page) < len(entries) else None},
                len(page),
            )
        elif operation == "read":
            if args.snapshot not in {"base", "head"}:
                raise ReviewError("read snapshot must be base or head")
            path = normalize_repo_path(args.path)
            available = {
                (record["old_path"] if args.snapshot == "base" else record["new_path"])
                for record in changed
                if (record["base"] if args.snapshot == "base" else record["head"])["available"]
            }
            if path not in available:
                raise ReviewError("path is not an available changed-file snapshot")
            source = contained_path(root / "snapshots" / args.snapshot, path)
            if source.is_symlink() or not source.is_file():
                raise ReviewError("snapshot path is not a regular file")
            data = source.read_bytes()
            offset = max(args.offset, 0)
            limit = min(max(args.byte_limit, 1), MAX_FILE_BYTES)
            page = data[offset : offset + limit]
            emit(
                operation,
                request,
                {
                    "path": path,
                    "snapshot": args.snapshot,
                    "offset": offset,
                    "next_offset": offset + len(page) if offset + len(page) < len(data) else None,
                    "encoding": "base64",
                    "content": base64.b64encode(page).decode("ascii"),
                },
                1,
            )
        elif operation == "search":
            if args.snapshot not in {"base", "head"}:
                raise ReviewError("search snapshot must be base or head")
            query = args.query
            if not query or len(query.encode()) > 1_000:
                raise ReviewError("search query must be non-empty and bounded")
            query_bytes = query.encode("utf-8")
            matches = []
            for record in changed:
                path = record["old_path"] if args.snapshot == "base" else record["new_path"]
                info = record["base"] if args.snapshot == "base" else record["head"]
                if path is None or not info["available"]:
                    continue
                source = contained_path(root / "snapshots" / args.snapshot, path)
                for line_number, line in enumerate(source.read_bytes().splitlines(), 1):
                    if query_bytes in line:
                        matches.append(
                            {
                                "path": path,
                                "line": line_number,
                                "text": line[:1_000].decode("utf-8", "replace"),
                            }
                        )
                        if len(matches) >= min(max(args.limit, 1), 200):
                            break
                if len(matches) >= min(max(args.limit, 1), 200):
                    break
            emit(operation, request, {"matches": matches, "truncated": len(matches) == min(max(args.limit, 1), 200)}, len(matches))
        elif operation == "diff-hunks":
            offset = max(args.offset, 0)
            limit = min(max(args.limit, 1), 200)
            page = hunks[offset : offset + limit]
            emit(
                operation,
                request,
                {"entries": page, "next_offset": offset + len(page) if offset + len(page) < len(hunks) else None},
                len(page),
            )
        elif operation == "history":
            history = manifest.get("base_history", [])
            offset = max(args.offset, 0)
            limit = min(max(args.limit, 1), 50)
            page = history[offset : offset + limit]
            emit(
                operation,
                request,
                {"entries": page, "next_offset": offset + len(page) if offset + len(page) < len(history) else None},
                len(page),
            )
        else:
            raise ReviewError(f"unsupported retrieval operation: {operation}")
    except Exception as error:
        audit_record(audit, operation, request, f"error:{type(error).__name__}", 0, 0)
        raise


retrieve = retriever


MCP_TOOLS = [
    {"name": "metadata", "description": "Get normalized pull-request metadata and captured revision", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}},
    {"name": "changed_files", "description": "List changed files with status and coverage", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
    {"name": "tree", "description": "List a bounded captured repository tree", "inputSchema": {"type": "object", "additionalProperties": False, "required": ["snapshot"], "properties": {"snapshot": {"enum": ["base", "head"]}, "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
    {"name": "read", "description": "Read a bounded changed-file snapshot", "inputSchema": {"type": "object", "additionalProperties": False, "required": ["snapshot", "path"], "properties": {"snapshot": {"enum": ["base", "head"]}, "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "byte_limit": {"type": "integer", "minimum": 1, "maximum": 524288}}}},
    {"name": "search", "description": "Search captured changed-file snapshots for literal text", "inputSchema": {"type": "object", "additionalProperties": False, "required": ["snapshot", "query"], "properties": {"snapshot": {"enum": ["base", "head"]}, "query": {"type": "string", "minLength": 1, "maxLength": 1000}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
    {"name": "diff_hunks", "description": "List immutable diff hunks incrementally", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}},
    {"name": "history", "description": "List optional bounded trusted-base history", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}}},
]


def mcp_tool_call(context: str, audit: str, name: str, arguments: Any) -> Any:
    if not isinstance(arguments, dict):
        raise ReviewError("MCP tool arguments must be an object")
    operation = name.replace("_", "-")
    known = {item["name"]: set(item["inputSchema"].get("properties", {})) for item in MCP_TOOLS}
    if name not in known or set(arguments) - known[name]:
        raise ReviewError("unknown MCP tool or argument")
    values = {
        "context": context,
        "audit": audit,
        "operation": operation,
        "snapshot": arguments.get("snapshot"),
        "path": arguments.get("path"),
        "query": arguments.get("query"),
        "offset": arguments.get("offset", 0),
        "limit": arguments.get("limit", 100),
        "byte_limit": arguments.get("byte_limit", 64 * 1024),
    }
    raw = io.BytesIO()
    text = io.TextIOWrapper(raw, encoding="utf-8")
    previous = sys.stdout
    try:
        sys.stdout = text
        retriever(argparse.Namespace(**values))
        text.flush()
    finally:
        sys.stdout = previous
    return json.loads(raw.getvalue())


def mcp_server(args: argparse.Namespace) -> None:
    """Serve only the audited retriever through MCP over stdio."""
    validate_manifest(Path(args.context).resolve())
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ReviewError("MCP request must be an object")
            request_id = request.get("id")
            method = request.get("method")
            if request_id is None:
                continue
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "review-context", "version": "1.0"}}
            elif method == "tools/list":
                result = {"tools": MCP_TOOLS}
            elif method == "tools/call":
                parameters = request.get("params") or {}
                value = mcp_tool_call(args.context, args.audit, parameters.get("name"), parameters.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(value, sort_keys=True)}], "isError": False}
            elif method == "ping":
                result = {}
            else:
                raise ReviewError(f"unsupported MCP method: {method}")
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as error:
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None, "error": {"code": -32000, "message": str(error)}}
        print(json.dumps(response, separators=(",", ":")), flush=True)


def reject_unknown(value: dict[str, Any], allowed: set[str], location: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ReviewError(f"unknown fields at {location}: {sorted(unknown)}")


def validate_text(name: str, value: Any, max_bytes: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value) or len(value.encode("utf-8")) > max_bytes:
        raise ReviewError(f"{name} must be a bounded string")
    return value


def line_in_ranges(line: int, ranges: list[list[int]]) -> bool:
    return any(start <= line <= end for start, end in ranges)


def validate_output_document(output: Any, manifest: dict[str, Any], changed: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(output, dict):
        raise ReviewError("review output must be a JSON object")
    top_fields = {
        "schema_version",
        "repository",
        "pull_request",
        "review_id",
        "review_mode",
        "base_sha",
        "merge_base_sha",
        "head_sha",
        "context_digest",
        "status",
        "coverage",
        "inline_findings",
        "general_findings",
        "summary",
        "clean_review",
        "failure_reason",
    }
    reject_unknown(output, top_fields, "output")
    required = top_fields - {"failure_reason"}
    if not required.issubset(output):
        raise ReviewError(f"missing output fields: {sorted(required - set(output))}")
    expected = {
        "schema_version": SCHEMA_VERSION,
        "repository": manifest["repository"],
        "pull_request": manifest["pull_request"],
        "review_id": manifest["review_id"],
        "review_mode": manifest["review_mode"],
        "base_sha": manifest["base_sha"],
        "merge_base_sha": manifest["merge_base_sha"],
        "head_sha": manifest["head_sha"],
        "context_digest": manifest["context_digest"],
    }
    for field, expected_value in expected.items():
        if output.get(field) != expected_value:
            raise ReviewError(f"output {field} does not match captured context")
    if output["status"] not in {"complete", "incomplete"}:
        raise ReviewError("status must be complete or incomplete")
    if type(output["clean_review"]) is not bool:
        raise ReviewError("clean_review must be a JSON boolean")
    validate_text("summary", output["summary"], MAX_SUMMARY_BYTES, allow_empty=True)
    failure_reason = output.get("failure_reason")
    if output["status"] == "incomplete":
        validate_text("failure_reason", failure_reason, 4_000)
    elif failure_reason not in (None, ""):
        raise ReviewError("complete output cannot include a failure_reason")
    if output["clean_review"] and (output["status"] != "complete" or output["inline_findings"] or output["general_findings"]):
        raise ReviewError("clean_review conflicts with status or findings")

    coverage = output["coverage"]
    if not isinstance(coverage, dict):
        raise ReviewError("coverage must be an object")
    reject_unknown(coverage, {"changed_files_reviewed", "changed_files_total", "diff_complete", "notes"}, "coverage")
    if type(coverage.get("changed_files_reviewed")) is not int or type(coverage.get("changed_files_total")) is not int:
        raise ReviewError("coverage counts must be integers")
    if not 0 <= coverage["changed_files_reviewed"] <= coverage["changed_files_total"] == manifest["changed_files"]:
        raise ReviewError("coverage counts do not match captured context")
    if type(coverage.get("diff_complete")) is not bool:
        raise ReviewError("diff_complete must be a JSON boolean")
    validate_text("coverage.notes", coverage.get("notes"), 4_000, allow_empty=True)
    if output["status"] == "complete" and (
        coverage["changed_files_reviewed"] != coverage["changed_files_total"] or not coverage["diff_complete"]
    ):
        raise ReviewError("complete output must report complete coverage")

    by_key: dict[tuple[str, str], tuple[dict[str, Any], int]] = {}
    for index, record in enumerate(changed):
        if record["old_path"] is not None:
            by_key[(record["old_path"], "LEFT")] = (record, index)
        if record["new_path"] is not None:
            by_key[(record["new_path"], "RIGHT")] = (record, index)
    inline = output["inline_findings"]
    general = output["general_findings"]
    if not isinstance(inline, list) or len(inline) > MAX_INLINE_FINDINGS:
        raise ReviewError("inline_findings exceeds the bounded array")
    if not isinstance(general, list) or len(general) > MAX_GENERAL_FINDINGS:
        raise ReviewError("general_findings exceeds the bounded array")
    seen: set[tuple[str, str, int, str]] = set()
    for index, finding in enumerate(inline):
        if not isinstance(finding, dict):
            raise ReviewError("inline finding must be an object")
        reject_unknown(finding, {"path", "side", "line", "severity", "category", "body"}, f"inline_findings[{index}]")
        path = normalize_repo_path(finding.get("path"))
        side = finding.get("side")
        line = finding.get("line")
        if side not in {"LEFT", "RIGHT"} or type(line) is not int or line <= 0:
            raise ReviewError("inline finding side/line is invalid")
        item = by_key.get((path, side))
        if item is None or not line_in_ranges(line, item[0]["line_ranges"][side]):
            raise ReviewError(f"inline finding is outside the immutable diff: {path}:{side}:{line}")
        if finding.get("severity") not in {"critical", "high", "medium", "low", "info"}:
            raise ReviewError("inline finding severity is invalid")
        category = validate_text("inline finding category", finding.get("category"), 200)
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", category):
            raise ReviewError("inline finding category is invalid")
        body = validate_text("inline finding body", finding.get("body"), MAX_COMMENT_BODY_BYTES)
        key = (path, side, line, body)
        if key in seen:
            raise ReviewError("duplicate inline finding")
        seen.add(key)
    for index, finding in enumerate(general):
        if not isinstance(finding, dict):
            raise ReviewError("general finding must be an object")
        reject_unknown(finding, {"severity", "category", "body"}, f"general_findings[{index}]")
        if finding.get("severity") not in {"critical", "high", "medium", "low", "info"}:
            raise ReviewError("general finding severity is invalid")
        category = validate_text("general finding category", finding.get("category"), 200)
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", category):
            raise ReviewError("general finding category is invalid")
        validate_text("general finding body", finding.get("body"), MAX_COMMENT_BODY_BYTES)
    return output


def validate_output_data(output: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    root_value = manifest.get("_context_root")
    if not root_value:
        raise ReviewError("context root is unavailable")
    changed = read_json(Path(root_value) / "changed-files.json", max_bytes=2 * 1024 * 1024)
    return validate_output_document(output, manifest, changed)


def validate_output(args: argparse.Namespace) -> None:
    root = Path(args.context).resolve()
    manifest = validate_manifest(root)
    changed = read_json(root / "changed-files.json", max_bytes=2 * 1024 * 1024)
    output = read_json(Path(args.output), max_bytes=MAX_OUTPUT_BYTES)
    validated = validate_output_document(output, manifest, changed)
    write_json(Path(args.validated_output), validated)
    print(json.dumps({"status": validated["status"], "inline_findings": len(validated["inline_findings"])}))

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
    return body or "Review completed."


def publish(args: argparse.Namespace) -> None:
    root = Path(args.context).resolve()
    manifest = validate_manifest(root)
    changed = read_json(root / "changed-files.json", max_bytes=2 * 1024 * 1024)
    output = validate_output_document(read_json(Path(args.output), max_bytes=MAX_OUTPUT_BYTES), manifest, changed)
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
        if output["status"] == "complete":
            for finding in output["inline_findings"]:
                live_base, live_head = live_revision(api_url, manifest["repository"], manifest["pull_request"], token)
                if live_base != manifest["base_sha"] or live_head != manifest["head_sha"]:
                    github_request("POST", f"{api_url}/repos/{manifest['repository']}/issues/{manifest['pull_request']}/comments", token, {"body": "Review incomplete: the pull request revision changed before publication."})
                    print(json.dumps({"published": "stale"}))
                    return
                payload = {
                    "body": finding["body"],
                    "commit_id": manifest["head_sha"],
                    "path": finding["path"],
                    "side": finding["side"],
                    "line": finding["line"],
                    "subject_type": "line",
                }
                github_request(
                    "POST",
                    f"{api_url}/repos/{manifest['repository']}/pulls/{manifest['pull_request']}/comments",
                    token,
                    payload,
                )
        # Check again immediately before the top-level result.
        live_base, live_head = live_revision(api_url, manifest["repository"], manifest["pull_request"], token)
        if live_base != manifest["base_sha"] or live_head != manifest["head_sha"]:
            body = "Review incomplete: the pull request revision changed before publication."
        else:
            body = fixed_result_body(output)
        github_request(
            "POST",
            f"{api_url}/repos/{manifest['repository']}/issues/{manifest['pull_request']}/comments",
            token,
            {"body": body},
        )
        print(json.dumps({"published": "complete" if output["status"] == "complete" else "incomplete"}))
    finally:
        try:
            github_request("DELETE", f"{api_url}/installation/token", token)
        except ReviewError as error:
            print(f"warning: publisher token revocation failed: {error}", file=sys.stderr)


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
        "operation", choices=["metadata", "changed-files", "tree", "read", "search", "diff-hunks", "history"]
    )
    retrieve.add_argument("--snapshot", choices=["base", "head"])
    retrieve.add_argument("--path")
    retrieve.add_argument("--query")
    retrieve.add_argument("--offset", type=int, default=0)
    retrieve.add_argument("--limit", type=int, default=100)
    retrieve.add_argument("--byte-limit", type=int, default=64 * 1024)
    retrieve.set_defaults(func=retriever)

    mcp = commands.add_parser("mcp-server")
    mcp.add_argument("--context", required=True)
    mcp.add_argument("--audit", required=True)
    mcp.set_defaults(func=mcp_server)

    validate = commands.add_parser("validate-output")
    validate.add_argument("--context", required=True)
    validate.add_argument("--output", required=True)
    validate.add_argument("--validated-output", required=True)
    validate.set_defaults(func=validate_output)

    publisher = commands.add_parser("publish")
    publisher.add_argument("--context", required=True)
    publisher.add_argument("--output", required=True)
    publisher.add_argument("--api-url", default="https://api.github.com")
    publisher.set_defaults(func=publish)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        args.func(args)
        return 0
    except ReviewError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
