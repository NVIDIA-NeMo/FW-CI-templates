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

"""Bounded retrieval, audit recording, and audit-derived completeness."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .context import validate_manifest
from .contracts import (
    MAX_FILE_BYTES,
    RETRIEVER_MAX_BYTES,
    RETRIEVER_MAX_CALLS,
    RETRIEVER_MAX_RESULTS,
    RETRIEVER_MAX_SECONDS,
    SEARCH_UNAVAILABLE_SAMPLE_LIMIT,
    RetrievalCoverage,
    ReviewError,
)
from .utils import canonical_json, contained_path, normalize_repo_path, read_json

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


def audit_record(
    audit: Path,
    operation: str,
    request: dict[str, Any],
    outcome: str,
    output_bytes: int,
    results: int,
    coverage: dict[str, Any] | None = None,
) -> None:
    record = {
        "operation": operation,
        "request": request,
        "outcome": outcome,
        "output_bytes": output_bytes,
        "results": results,
    }
    if coverage is not None:
        record["coverage"] = coverage
    with audit.open("ab") as stream:
        stream.write(canonical_json(record) + b"\n")


def retriever(args: argparse.Namespace) -> None:
    root = Path(args.context).resolve()
    manifest = validate_manifest(root)
    changed = read_json(root / "changed-files.json", max_bytes=2 * 1024 * 1024)
    metadata = read_json(root / "metadata.json", max_bytes=256 * 1024)
    trees = read_json(root / "trees.json", max_bytes=32 * 1024 * 1024)
    hunks = read_json(root / "diff-hunks.json", max_bytes=4 * 1024 * 1024)
    base_repository = read_json(root / "base-repository.json", max_bytes=32 * 1024 * 1024)
    governing = read_json(root / "governing-base.json", max_bytes=2 * 1024 * 1024)
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

    def emit(
        operation: str,
        request: dict[str, Any],
        value: Any,
        results: int,
        coverage: dict[str, Any] | None = None,
    ) -> None:
        data = canonical_json(value) + b"\n"
        budget.charge(output_bytes=len(data), results=results)
        audit_record(audit, operation, request, "ok", len(data), results, coverage)
        sys.stdout.buffer.write(data)

    def emit_text(operation: str, request: dict[str, Any], path: str, snapshot: str, data: bytes) -> None:
        if b"\0" in data[:8_000]:
            raise ReviewError("text retrieval refused binary content")
        offset = max(args.offset, 0)
        limit = min(max(args.byte_limit, 1), MAX_FILE_BYTES)
        page = data[offset : offset + limit]
        end = offset + len(page)
        emit(
            operation,
            request,
            {
                "path": path,
                "snapshot": snapshot,
                "offset": offset,
                "next_offset": end if end < len(data) else None,
                "content": page.decode("utf-8", "replace"),
            },
            1,
            {"kind": operation.replace("-", "_"), "path": path, "start": offset, "end": end, "total": len(data)},
        )

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
                {"kind": "changed_files", "path": "", "start": offset, "end": offset + len(page), "total": len(changed)},
            )
        elif operation == "governing-base":
            offset = max(args.offset, 0)
            limit = min(max(args.limit, 1), 200)
            page = governing[offset : offset + limit]
            emit(
                operation,
                request,
                {"entries": page, "next_offset": offset + len(page) if offset + len(page) < len(governing) else None},
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
            emit_text(operation, request, path, args.snapshot, source.read_bytes())
        elif operation == "diff":
            data = (root / "review.diff").read_bytes()
            emit_text(operation, request, "review.diff", "merge-base..head", data)
        elif operation == "trusted-base-read":
            path = normalize_repo_path(args.path)
            info = base_repository.get(path)
            if not isinstance(info, dict) or not info.get("available"):
                raise ReviewError("path is not an available trusted-base regular file")
            source = contained_path(root / "snapshots" / "trusted-base", path)
            if source.is_symlink() or not source.is_file():
                raise ReviewError("trusted-base path is not a regular file")
            emit_text(operation, request, path, "base", source.read_bytes())
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
        elif operation == "trusted-base-search":
            query = args.query
            if not query or len(query.encode()) > 1_000:
                raise ReviewError("search query must be non-empty and bounded")
            query_bytes = query.encode("utf-8")
            matches = []
            prefix = "" if args.path is None else normalize_repo_path(args.path).rstrip("/") + "/"
            result_limit = min(max(args.limit, 1), 200)
            scope = [
                (path, info)
                for path, info in sorted(base_repository.items())
                if not prefix or path == prefix.rstrip("/") or path.startswith(prefix)
            ]
            unavailable = [
                (path, str(info.get("reason") or "unavailable"))
                for path, info in scope
                if not isinstance(info, dict) or not info.get("available")
            ]
            unavailable_reasons = Counter(reason for _, reason in unavailable)
            unavailable_sample = [
                {"path": path, "reason": reason}
                for path, reason in unavailable[:SEARCH_UNAVAILABLE_SAMPLE_LIMIT]
            ]
            searched = 0
            result_truncated = False
            for path, info in scope:
                if not isinstance(info, dict) or not info.get("available"):
                    continue
                searched += 1
                source = contained_path(root / "snapshots" / "trusted-base", path)
                for line_number, line in enumerate(source.read_bytes().splitlines(), 1):
                    if query_bytes in line:
                        matches.append({"path": path, "line": line_number, "text": line[:1_000].decode("utf-8", "replace")})
                        if len(matches) >= result_limit:
                            result_truncated = True
                            break
                if result_truncated:
                    break
            scope_complete = not unavailable and searched == len(scope) and not result_truncated
            emit(
                operation,
                request,
                {
                    "matches": matches,
                    "truncated": result_truncated,
                    "files_total": len(scope),
                    "files_searched": searched,
                    "files_unavailable_count": len(unavailable),
                    "files_unavailable_by_reason": dict(sorted(unavailable_reasons.items())),
                    "files_unavailable_sample": unavailable_sample,
                    "scope_complete": scope_complete,
                },
                len(matches),
                {"kind": "trusted_base_search", "scope_complete": scope_complete},
            )
        elif operation == "coverage":
            emit(operation, request, retrieval_coverage(root, audit), 1)
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


def _covered_length(intervals: list[tuple[int, int]], total: int) -> int:
    cursor = covered = 0
    for start, end in sorted(intervals):
        start = max(0, min(start, total))
        end = max(start, min(end, total))
        if end <= cursor:
            continue
        start = max(start, cursor)
        covered += end - start
        cursor = end
    return covered


def retrieval_coverage(root: Path, audit: Path) -> RetrievalCoverage:
    """Derive review completeness exclusively from successful audited retrieval."""
    manifest = validate_manifest(root)
    changed_total = int(manifest["changed_files"])
    diff_total = int(manifest["coverage"]["stored_diff_bytes"])
    governing = read_json(root / "governing-base.json", max_bytes=2 * 1024 * 1024)
    intervals: dict[tuple[str, str], list[tuple[int, int]]] = {}
    if audit.exists():
        for raw in audit.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as error:
                raise ReviewError("retrieval audit contains invalid JSON") from error
            if not isinstance(record, dict) or record.get("outcome") != "ok":
                continue
            coverage = record.get("coverage")
            if not isinstance(coverage, dict):
                continue
            kind = coverage.get("kind")
            path = str(coverage.get("path") or "")
            start = coverage.get("start")
            end = coverage.get("end")
            if isinstance(kind, str) and type(start) is int and type(end) is int:
                intervals.setdefault((kind, path), []).append((start, end))
    changed_seen = _covered_length(intervals.get(("changed_files", ""), []), changed_total)
    diff_seen = _covered_length(intervals.get(("diff", "review.diff"), []), diff_total)
    governing_missing = []
    governing_unread = []
    for record in governing:
        path = record["path"]
        if not record.get("available"):
            governing_missing.append(path)
            continue
        size = int(record["size"])
        if _covered_length(intervals.get(("trusted_base_read", path), []), size) != size:
            governing_unread.append(path)
    diff_complete = not manifest["coverage"]["diff_truncated"] and diff_seen == diff_total
    listing_complete = changed_seen == changed_total
    governing_complete = not governing_missing and not governing_unread
    tree_complete = not manifest["coverage"]["base_tree_truncated"]
    complete = listing_complete and diff_complete and governing_complete and tree_complete
    return {
        "changed_files_reviewed": changed_total if complete else 0,
        "changed_files_total": changed_total,
        "diff_complete": diff_complete,
        "changed_files_list_complete": listing_complete,
        "governing_base_complete": governing_complete,
        "trusted_base_tree_complete": tree_complete,
        "governing_base_missing": governing_missing,
        "governing_base_unread": governing_unread,
        "complete": complete,
    }


retrieve = retriever
