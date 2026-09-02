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

"""Immutable Git context construction and manifest verification."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .contracts import (
    HUNK_RE,
    MANIFEST_VERSION,
    MAX_CHANGED_FILES,
    MAX_CONTEXT_BYTES,
    MAX_DIFF_BYTES,
    MAX_DIFF_HUNKS,
    MAX_FILE_BYTES,
    MAX_TREE_ENTRIES,
    ChangedFile,
    ChangedStatus,
    DiffHunk,
    ReviewError,
    SnapshotInfo,
    TreeEntry,
)
from .utils import (
    canonical_json,
    contained_path,
    git,
    normalize_repo_path,
    read_json,
    require_repository,
    require_sha,
    sha256_bytes,
    write_json,
)

def parse_ls_tree(repo: Path, sha: str, limit: int) -> tuple[dict[str, TreeEntry], bool]:
    raw = git(repo, "ls-tree", "-r", "-z", "-l", sha)
    entries: dict[str, TreeEntry] = {}
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


def parse_name_status(raw: bytes) -> list[ChangedStatus]:
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    changed: list[ChangedStatus] = []
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


def parse_hunks(diff: bytes, changed: list[ChangedStatus]) -> list[DiffHunk]:
    hunks: list[DiffHunk] = []
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


def ranges_for_file(hunks: Iterable[DiffHunk], file_index: int, side: str) -> list[list[int]]:
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


def is_governing_base_path(path: str) -> bool:
    """Return whether a trusted-base path can govern repository review."""
    parts = PurePosixPath(path).parts
    name = parts[-1].lower() if parts else ""
    if name in {"agents.md", "claude.md", "codeowners", "contributing.md"}:
        return True
    if path in {".github/copilot-instructions.md", ".github/instructions.md"}:
        return True
    return len(parts) >= 2 and parts[-1] == "SKILL.md" and ("skills" in parts or ".claude" in parts)


def resolve_trusted_symlink(
    repo: Path,
    path: str,
    tree: dict[str, TreeEntry],
    *,
    max_depth: int = 8,
) -> tuple[str, TreeEntry, list[str]]:
    """Resolve only captured, relative symlinks without touching the filesystem."""
    current = normalize_repo_path(path)
    seen: set[str] = set()
    chain: list[str] = []
    for _ in range(max_depth + 1):
        if current in seen:
            raise ReviewError(f"trusted-base symlink cycle at {current}")
        seen.add(current)
        entry = tree.get(current)
        if entry is None:
            raise ReviewError(f"trusted-base symlink target is missing: {current}")
        if entry.get("mode") != "120000":
            return current, entry, chain
        if len(chain) >= max_depth:
            raise ReviewError("trusted-base symlink depth exceeds limit")
        raw_target = git(repo, "cat-file", "blob", entry["oid"])
        try:
            target = raw_target.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ReviewError("trusted-base symlink target is not UTF-8") from error
        if not target or "\x00" in target or "\n" in target or "\r" in target:
            raise ReviewError("trusted-base symlink target contains a control character")
        target_path = PurePosixPath(target)
        if target_path.is_absolute() or target.startswith("/"):
            raise ReviewError("trusted-base symlink target must be relative")
        combined = PurePosixPath(current).parent / target_path
        parts: list[str] = []
        for part in combined.parts:
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    raise ReviewError("trusted-base symlink target escapes the repository")
                parts.pop()
            else:
                parts.append(part)
        if not parts:
            raise ReviewError("trusted-base symlink target is empty")
        chain.append(current)
        current = normalize_repo_path(PurePosixPath(*parts).as_posix())
    raise ReviewError("trusted-base symlink depth exceeds limit")


def materialize_snapshot(
    repo: Path,
    output: Path,
    snapshot: str,
    path: str,
    tree_entry: TreeEntry | None,
    budget: list[int],
    *,
    tree: dict[str, TreeEntry] | None = None,
) -> SnapshotInfo:
    result: SnapshotInfo = {"available": False, "reason": "missing"}
    if tree_entry is None:
        return result
    result.update(tree_entry)
    mode = tree_entry["mode"]
    size = tree_entry["size"]
    if mode == "120000":
        if tree is None:
            result["reason"] = "symlink"
            return result
        try:
            resolved_path, tree_entry, chain = resolve_trusted_symlink(repo, path, tree)
        except ReviewError as error:
            result["reason"] = "unsafe_symlink"
            result["symlink_error"] = str(error)
            return result
        result.update(tree_entry)
        result["symlink_target"] = resolved_path
        result["symlink_chain"] = chain
        mode = tree_entry["mode"]
        size = tree_entry["size"]
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
    governing_paths = sorted(path for path in base_tree if is_governing_base_path(path))
    base_repository: dict[str, dict[str, Any]] = {}
    # Capture governing instructions first so general source material cannot consume their budget.
    for path in governing_paths:
        base_repository[path] = materialize_snapshot(repo, output, "trusted-base", path, base_tree[path], budget, tree=base_tree)

    changed_records: list[ChangedFile] = []
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

    # Capture bounded trusted BASE_SHA source for unchanged definitions, callers, tests,
    # configurations, and repository policy. Unavailable entries remain explicit.
    for path, entry in sorted(base_tree.items()):
        if path not in base_repository:
            base_repository[path] = materialize_snapshot(repo, output, "trusted-base", path, entry, budget, tree=base_tree)
    governing_records = [{"path": path, **base_repository[path]} for path in governing_paths]

    write_json(output / "metadata.json", metadata)
    write_json(output / "changed-files.json", changed_records)
    write_json(output / "base-repository.json", base_repository)
    write_json(output / "governing-base.json", governing_records)
    write_json(output / "diff-hunks.json", hunks)
    write_json(output / "trees.json", {"base": base_tree, "head": head_tree})
    tools_dir = output / "tools"
    tools_dir.mkdir()
    tool_root = Path(__file__).resolve().parent.parent
    implementation_paths = [tool_root / "review_components.py", *sorted((tool_root / "reviewlib").glob("*.py"))]
    schema = tool_root / "review-output-v1.schema.json"
    for implementation in [*implementation_paths, schema]:
        relative = implementation.relative_to(tool_root)
        destination = tools_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(implementation.read_bytes())

    artifact_paths = [
        "metadata.json",
        "changed-files.json",
        "base-repository.json",
        "governing-base.json",
        "diff-hunks.json",
        "trees.json",
        "review.diff",
        *[f"tools/{path.relative_to(tool_root)}" for path in implementation_paths],
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
            "trusted_base_files": sum(1 for item in base_repository.values() if item.get("available")),
            "governing_base_files": len(governing_records),
            "diff_hunks": len(hunks),
        },
        "artifacts": artifacts,
    }
    manifest["context_digest"] = sha256_bytes(canonical_json(manifest))
    write_json(output / "manifest.json", manifest)
    print(json.dumps({"context_digest": manifest["context_digest"], "changed_files": len(changed_records)}))


load_context = validate_manifest
