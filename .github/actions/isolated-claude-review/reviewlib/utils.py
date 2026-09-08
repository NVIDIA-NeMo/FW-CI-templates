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
    """Serialize a value into deterministic UTF-8 JSON.

    Args:
        value: Value to serialize or validate.

    Returns:
        Deterministic UTF-8 JSON bytes.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    """Compute the lowercase SHA-256 digest of bytes.

    Args:
        value: Value to serialize or validate.

    Returns:
        Lowercase hexadecimal digest.
    """
    return hashlib.sha256(value).hexdigest()


def read_json(path: Path, *, max_bytes: int = MAX_OUTPUT_BYTES) -> Any:
    """Read and decode a size-bounded JSON file.

    Args:
        path: Repository-relative path or filesystem path to process.
        max_bytes: Maximum number of bytes accepted or returned.

    Returns:
        Decoded JSON value.
    """
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise ReviewError(f"JSON input exceeds {max_bytes} bytes: {path}")
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReviewError(f"Invalid JSON in {path}: {error}") from error


def write_json(path: Path, value: Any) -> None:
    """Write deterministic JSON with a trailing newline.

    Args:
        path: Repository-relative path or filesystem path to process.
        value: Value to serialize or validate.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value) + b"\n")


def require_sha(name: str, value: Any) -> str:
    """Validate a lowercase full Git commit identifier.

    Args:
        name: Field, command, or operation name.
        value: Value to serialize or validate.

    Returns:
        Validated commit identifier.
    """
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise ReviewError(f"{name} must be a lowercase 40-character SHA")
    return value


def require_repository(value: Any) -> str:
    """Validate a canonical owner and repository name.

    Args:
        value: Value to serialize or validate.

    Returns:
        Validated owner and repository name.
    """
    if not isinstance(value, str) or not SAFE_REPOSITORY_RE.fullmatch(value):
        raise ReviewError("repository must be an owner/name value")
    return value


def normalize_repo_path(value: Any) -> str:
    """Validate and normalize a repository-relative path.

    Args:
        value: Value to serialize or validate.

    Returns:
        Normalized POSIX repository path.
    """
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ReviewError("repository path is empty or contains a control character")
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("/") or any(part in ("", ".", "..") for part in path.parts):
        raise ReviewError(f"unsafe repository path: {value!r}")
    return path.as_posix()


def contained_path(root: Path, relative: str) -> Path:
    """Resolve a repository-relative path beneath a trusted root.

    Args:
        root: Trusted context or filesystem root.
        relative: Untrusted repository-relative path.

    Returns:
        Resolved path beneath the trusted root.
    """
    relative = normalize_repo_path(relative)
    root = root.resolve()
    candidate = (root / relative).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ReviewError(f"path escapes context root: {relative!r}") from error
    return candidate


def git(repo: Path, *args: str, input_bytes: bytes | None = None, max_bytes: int | None = None) -> bytes:
    """Run one bounded Git command without repository hooks.

    Args:
        repo: Repository whose Git objects are inspected.
        input_bytes: Optional bytes passed to Git standard input.
        max_bytes: Maximum number of bytes accepted or returned.
        args: Parsed command-line arguments for the operation.

    Returns:
        Standard output bytes from Git.
    """
    command = ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "-c", "core.autocrlf=false", *args]
    result = subprocess.run(command, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace")[-2_000:]
        raise ReviewError(f"git command failed ({args[0]}): {stderr}")
    if max_bytes is not None and len(result.stdout) > max_bytes:
        raise ReviewError(f"git output exceeds {max_bytes} bytes ({args[0]})")
    return result.stdout
