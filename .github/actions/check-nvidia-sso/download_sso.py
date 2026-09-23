# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Download the current SSO roster without stale embedded asset metadata."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

GITHUB_API_URL = "https://api.github.com"
GITHUB_AUDITS_REPO = os.environ.get(
    "SSO_REPOSITORY", "NVIDIA-GitHub-Management/github-audits"
)
GITHUB_AUDITS_VERSION = os.environ.get("SSO_RELEASE_TAG", "v0.1.0")
SSO_USERS_FILENAME = os.environ.get("SSO_FILENAME", "users_sso.json")
GITHUB_AUDITS_TIMEOUT = 15.0
_SSO_FETCH_ATTEMPTS = 3
_SSO_FETCH_BUDGET_SECONDS = 60.0
_SSO_ASSET_MAX_PAGES = 5


class _StripAuthOnRedirect(HTTPRedirectHandler):
    """Strip credentials when the asset redirects to another host.

    Object storage uses the redirect's signed query, not the GitHub audit token.
    """

    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Request | None:
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib.parse.urlsplit(req.full_url).netloc
            new_host = urllib.parse.urlsplit(newurl).netloc
            if old_host != new_host:
                for key in [k for k in new.headers if k.lower() == "authorization"]:
                    del new.headers[key]
        return new


class SsoFetchError(RuntimeError):
    """A content-free fetch failure with explicit retry classification."""

    def __init__(self, *, status: int | None = None, retry_after: float = 0.0) -> None:
        """Retain status and delay without exposing response bodies or signed URLs."""
        super().__init__(
            f"github-audits request rejected: HTTP {status}"
            if status
            else "github-audits request unavailable"
        )
        self.status = status
        self.retry_after = retry_after
        self.retryable = (
            status is None or status in {404, 408, 429} or 500 <= status < 600
        )


def _retry_after_seconds(headers: Any) -> float:
    """Parse Retry-After seconds or HTTP dates, ignoring malformed server hints."""
    value = headers.get("Retry-After", "") if headers else ""
    try:
        seconds = float(value)
        return max(0.0, seconds) if seconds < float("inf") else 0.0
    except ValueError:
        try:
            return max(
                0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            )
        except (TypeError, ValueError, OverflowError):
            return 0.0


def _get(url: str, headers: dict[str, str], *, timeout: float | None = None) -> bytes:
    """Fetch bytes with sanitized errors and cross-host credential stripping."""
    opener = build_opener(_StripAuthOnRedirect())
    request = Request(url, headers=headers, method="GET")
    try:
        with opener.open(
            request, timeout=GITHUB_AUDITS_TIMEOUT if timeout is None else timeout
        ) as response:
            return response.read()
    except HTTPError as exc:
        retry_after = _retry_after_seconds(exc.headers)
        rate_limited = exc.code == 403 and (
            retry_after > 0 or (exc.headers or {}).get("X-RateLimit-Remaining") == "0"
        )
        exc.close()
        raise SsoFetchError(
            status=429 if rate_limited else exc.code, retry_after=retry_after
        ) from exc
    except (URLError, OSError, IncompleteRead) as exc:
        raise SsoFetchError() from exc


def _sso_fetch(url: str, headers: dict[str, str], deadline: float) -> bytes:
    """Keep each request inside the remaining roster-lookup time budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SsoFetchError()
    return _get(url, headers, timeout=min(GITHUB_AUDITS_TIMEOUT, remaining))


def _current_sso_asset(api_headers: dict[str, str], deadline: float) -> int:
    """Resolve the current paginated asset list, not embedded release metadata."""
    repository_url = f"{GITHUB_API_URL}/repos/{GITHUB_AUDITS_REPO}"
    tag = urllib.parse.quote(GITHUB_AUDITS_VERSION, safe="")
    release_url = f"{repository_url}/releases/tags/{tag}"
    release = json.loads(_sso_fetch(release_url, api_headers, deadline))
    release_id = release.get("id") if isinstance(release, dict) else None
    if type(release_id) is not int or release_id <= 0:
        raise ValueError("invalid GitHub audit release identity")
    for page in range(1, _SSO_ASSET_MAX_PAGES + 1):
        url = f"{repository_url}/releases/{release_id}/assets?per_page=100&page={page}"
        assets = json.loads(_sso_fetch(url, api_headers, deadline))
        if not isinstance(assets, list):
            raise ValueError("invalid GitHub audit asset listing")
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("name") != SSO_USERS_FILENAME:
                continue
            asset_id = asset.get("id")
            if type(asset_id) is not int or asset_id <= 0:
                raise ValueError("invalid GitHub audit asset identity")
            return asset_id
        if len(assets) < 100:
            raise SsoFetchError(status=404)
    raise ValueError("GitHub audit asset listing exceeded the page limit")


def _download_sso_map(deadline: float) -> dict[str, Any]:
    """Refresh discovery and credentials, returning only validated JSON."""
    token = os.environ["GH_TOKEN"]
    api_headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Cache-Control": "no-cache",
    }
    asset_id = _current_sso_asset(api_headers, deadline)
    url = f"{GITHUB_API_URL}/repos/{GITHUB_AUDITS_REPO}/releases/assets/{asset_id}"
    raw = _sso_fetch(
        url, {**api_headers, "Accept": "application/octet-stream"}, deadline
    )
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("invalid GitHub SSO roster")
    return data


def download_roster() -> dict[str, Any]:
    """Refresh asset discovery on each of three bounded download attempts."""
    deadline = time.monotonic() + _SSO_FETCH_BUDGET_SECONDS
    for attempt in range(1, _SSO_FETCH_ATTEMPTS + 1):
        try:
            return _download_sso_map(deadline)
        except SsoFetchError as exc:
            delay = max(float(2 ** (attempt - 1)), exc.retry_after)
            if (
                not exc.retryable
                or attempt == _SSO_FETCH_ATTEMPTS
                or time.monotonic() + delay >= deadline
            ):
                raise
            print(
                f"SSO attempt {attempt} failed (HTTP {exc.status or 'transport'}); "
                "refreshing asset discovery",
                flush=True,
            )
            time.sleep(delay)
    raise RuntimeError("SSO roster download attempts exhausted")


def save_roster(data: dict[str, Any], destination: Path) -> None:
    """Atomically replace the destination only after a complete valid JSON download."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=".sso-roster-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    """Download a valid roster and emit content-free diagnostics."""
    try:
        if not os.environ.get("GH_TOKEN"):
            raise ValueError("GitHub audit credential is missing")
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", GITHUB_AUDITS_REPO) is None:
            raise ValueError("invalid audit repository")
        destination = Path(SSO_USERS_FILENAME)
        if (
            not SSO_USERS_FILENAME
            or destination.name != SSO_USERS_FILENAME
            or SSO_USERS_FILENAME in {".", ".."}
        ):
            raise ValueError("SSO filename must be a plain filename")
        data = download_roster()
        save_roster(data, destination)
    except (SsoFetchError, ValueError, KeyError, OSError) as exc:
        detail = str(exc) if isinstance(exc, SsoFetchError) else type(exc).__name__
        print(
            f"ERROR: SSO roster unavailable ({detail}); membership was not checked",
            file=sys.stderr,
        )
        return 1
    print(f"Successfully downloaded SSO roster with {len(data)} users")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
