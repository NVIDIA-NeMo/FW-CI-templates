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

"""Fail-closed JSON, path, hash, and Git helpers."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from .contracts import MAX_OUTPUT_BYTES, SAFE_REPOSITORY_RE, SHA_RE, ReviewError


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
