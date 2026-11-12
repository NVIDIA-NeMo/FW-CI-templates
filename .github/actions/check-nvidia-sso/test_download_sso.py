# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for release-asset rotation, retries and safe roster publication."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

import download_sso as downloader

ROSTER = {
    "octocat": {"nvidia_email": "octo@nvidia.com", "org_roles": ["NVIDIA:Member"]}
}


def response(url: str, asset_id: int = 2) -> bytes:
    """Return stale tag metadata but a current authoritative asset listing."""
    if "/releases/tags/" in url:
        return json.dumps(
            {"id": 100, "assets": [{"name": "users_sso.json", "id": 1}]}
        ).encode()
    if "per_page" in url:
        return json.dumps([{"name": "users_sso.json", "id": asset_id}]).encode()
    return json.dumps(ROSTER).encode()


class DownloadSsoTests(unittest.TestCase):
    """Verify that failed identity downloads never become a membership result."""

    def setUp(self) -> None:
        """Provide a test credential and replace backoff with a recorded mock."""
        token = patch.dict(os.environ, {"GH_TOKEN": "do-not-log-token"})
        token.start()
        self.addCleanup(token.stop)
        delay = patch.object(downloader.time, "sleep")
        self.delay = delay.start()
        self.addCleanup(delay.stop)

    def test_stale_tag_assets_are_not_downloaded(self) -> None:
        """The deleted embedded asset must never be selected."""
        with patch.object(
            downloader, "_get", side_effect=lambda url, *_a, **_k: response(url)
        ) as get:
            self.assertEqual(downloader.download_roster(), ROSTER)
        self.assertTrue(get.call_args_list[-1].args[0].endswith("/assets/2"))
        self.assertEqual(get.call_count, 3)
        self.delay.assert_not_called()

    def test_retry_rediscovers_replaced_asset(self) -> None:
        """A transient failure starts a fresh listing, not another stale download."""
        for status in (404, 408, 429, 500, 502, 503, 504, None):
            with self.subTest(status=status):
                listings = []
                self.delay.reset_mock()

                def get(url, *_args, **_kwargs):
                    """Replace the selected asset after its first failed download."""
                    if "per_page" in url:
                        listings.append(url)
                    if url.endswith("/assets/1"):
                        raise downloader.SsoFetchError(status=status)
                    return response(url, len(listings))

                with patch.object(downloader, "_get", side_effect=get):
                    self.assertEqual(downloader.download_roster(), ROSTER)
                self.assertEqual(len(listings), 2)
                self.delay.assert_called_once_with(1.0)

    def test_attempt_limit_and_permanent_authorization_failures(self) -> None:
        """Only retryable failures receive the three-attempt budget."""
        for status, attempts in ((401, 1), (403, 1), (404, 3), (503, 3)):
            with self.subTest(status=status):
                with patch.object(
                    downloader,
                    "_get",
                    side_effect=downloader.SsoFetchError(status=status),
                ) as get:
                    with self.assertRaises(downloader.SsoFetchError):
                        downloader.download_roster()
                self.assertEqual(get.call_count, attempts)

    def test_missing_asset_during_rotation_recovers(self) -> None:
        """A delete-before-upload gap retries discovery."""
        listings = []

        def get(url, *_args, **_kwargs):
            """Expose the replacement on the second listing."""
            if "per_page" in url:
                listings.append(url)
                if len(listings) == 1:
                    return b"[]"
            return response(url)

        with patch.object(downloader, "_get", side_effect=get):
            self.assertEqual(downloader.download_roster(), ROSTER)
        self.assertEqual(len(listings), 2)

    def test_pagination_finds_the_current_asset(self) -> None:
        """Full asset pages must not hide the roster on a later page."""

        def get(url, *_args, **_kwargs):
            """Put unrelated assets on the first page."""
            if url.endswith("&page=1"):
                return json.dumps([{"id": 9, "name": "other.json"}] * 100).encode()
            return response(url)

        with patch.object(downloader, "_get", side_effect=get):
            self.assertEqual(downloader.download_roster(), ROSTER)

    def test_malformed_roster_is_not_retried_or_published(self) -> None:
        """Invalid JSON is not authoritative membership evidence."""
        for body in (b"bad JSON", b"[]"):
            with self.subTest(body=body):
                with patch.object(
                    downloader,
                    "_get",
                    side_effect=lambda url, *_a, **_k: (
                        body if "/releases/assets/" in url else response(url)
                    ),
                ):
                    with self.assertRaises(ValueError):
                        downloader.download_roster()
        self.delay.assert_not_called()

    def test_rate_limit_delay_must_fit_budget(self) -> None:
        """Do not immediately hammer a provider asking for a longer delay."""
        with patch.object(
            downloader,
            "_get",
            side_effect=downloader.SsoFetchError(status=429, retry_after=120),
        ) as get:
            with self.assertRaises(downloader.SsoFetchError):
                downloader.download_roster()
        self.assertEqual(get.call_count, 1)
        self.delay.assert_not_called()

    def test_incomplete_response_and_transport_errors_are_sanitized(self) -> None:
        """Interrupted response bodies retry without leaking partial content or URLs."""
        for error in (
            IncompleteRead(b"private roster", 100),
            URLError("signed=private"),
        ):
            with self.subTest(error=type(error).__name__):
                with patch.object(downloader, "build_opener") as opener:
                    opened = opener.return_value.open.return_value
                    opened.__enter__.return_value.read.side_effect = error
                    with self.assertRaises(downloader.SsoFetchError) as caught:
                        downloader._get("https://api.github.com/test", {})
                self.assertTrue(caught.exception.retryable)
                self.assertNotIn("private", str(caught.exception))

    def test_http_errors_classify_rate_limits_without_retrying_plain_denials(
        self,
    ) -> None:
        """A rate-limited 403 differs from an ordinary access denial."""
        for headers, retryable in (({}, False), ({"Retry-After": "4"}, True)):
            with self.subTest(headers=headers):
                with patch.object(downloader, "build_opener") as opener:
                    opener.return_value.open.side_effect = HTTPError(
                        "https://api.github.com/test", 403, "error", headers, None
                    )
                    with self.assertRaises(downloader.SsoFetchError) as caught:
                        downloader._get("https://api.github.com/test", {})
                self.assertEqual(caught.exception.retryable, retryable)

    def test_redirect_drops_authorization(self) -> None:
        """An asset redirect must not forward the audit credential to object storage."""
        request = Request(
            "https://api.github.com/test", headers={"Authorization": "token secret"}
        )
        redirected = downloader._StripAuthOnRedirect().redirect_request(
            request, None, 302, "Found", {}, "https://objects.example.invalid/asset"
        )
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_success_preserves_full_membership_roster_and_atomic_file(self) -> None:
        """Publish the original org_roles rather than a projected email-only map."""
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "users_sso.json"
            destination.write_text("old")
            downloader.save_roster(ROSTER, destination)
            self.assertEqual(json.loads(destination.read_text()), ROSTER)
            self.assertEqual(list(Path(directory).glob(".sso-roster-*")), [])

    def test_cli_failure_does_not_write_roster_or_leak_response(self) -> None:
        """Failed downloads return nonzero and never overwrite an existing file."""
        output = io.StringIO()
        with patch.object(
            downloader,
            "download_roster",
            side_effect=downloader.SsoFetchError(status=404),
        ):
            with (
                patch.object(downloader, "save_roster") as save,
                contextlib.redirect_stderr(output),
            ):
                self.assertEqual(downloader.main(), 1)
        save.assert_not_called()
        self.assertNotIn("do-not-log-token", output.getvalue())


if __name__ == "__main__":
    unittest.main()
