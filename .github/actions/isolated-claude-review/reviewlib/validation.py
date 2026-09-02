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

"""Structured model-output and retrieval-coverage validation."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from .context import validate_manifest
from .contracts import (
    MAX_COMMENT_BODY_BYTES,
    MAX_GENERAL_FINDINGS,
    MAX_INLINE_FINDINGS,
    MAX_OUTPUT_BYTES,
    MAX_SUMMARY_BYTES,
    SCHEMA_VERSION,
    ChangedFile,
    RetrievalCoverage,
    ReviewError,
    ReviewOutput,
)
from .retrieval import retrieval_coverage
from .utils import normalize_repo_path, read_json, write_json

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


def validate_output_document(
    output: Any,
    manifest: dict[str, Any],
    changed: list[ChangedFile],
    audited_coverage: RetrievalCoverage | None = None,
) -> ReviewOutput:
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
    if audited_coverage is not None:
        for field in ("changed_files_reviewed", "changed_files_total", "diff_complete"):
            if coverage[field] != audited_coverage[field]:
                raise ReviewError(f"coverage.{field} does not match the retrieval audit")
        if output["status"] == "complete" and not audited_coverage["complete"]:
            raise ReviewError("complete output is not supported by audited retrieval")
    if output["status"] == "complete" and (
        coverage["changed_files_reviewed"] != coverage["changed_files_total"] or not coverage["diff_complete"]
    ):
        raise ReviewError("complete output must report complete coverage")
    if output["status"] == "incomplete" and (output["inline_findings"] or output["general_findings"] or output["clean_review"]):
        raise ReviewError("incomplete output cannot publish findings or claim a clean review")

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


def validate_output_data(output: Any, manifest: dict[str, Any]) -> ReviewOutput:
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
    audited = retrieval_coverage(root, Path(args.audit).resolve())
    validated = validate_output_document(output, manifest, changed, audited)
    write_json(Path(args.validated_output), validated)
    print(json.dumps({"status": validated["status"], "inline_findings": len(validated["inline_findings"])}))
