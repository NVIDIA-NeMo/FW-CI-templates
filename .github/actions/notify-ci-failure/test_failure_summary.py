# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from failure_summary import build_payload


ACTION_DIR = Path(__file__).parent
CONTEXT = {
    "GITHUB_REPOSITORY": "NVIDIA-NeMo/example",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_RUN_ID": "123",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_WORKFLOW": "Nightly tests",
    "GITHUB_REF_NAME": "main",
    "GITHUB_SHA": "a" * 40,
    "GITHUB_EVENT_NAME": "schedule",
}


class FailureSummaryTests(unittest.TestCase):
    def test_no_failure_suppresses_delivery(self):
        for result in ("success", "skipped", "cancelled"):
            with self.subTest(result=result):
                self.assertIsNone(
                    build_payload(json.dumps({"test": {"result": result}}), CONTEXT)
                )
        self.assertIsNone(build_payload("{}", CONTEXT))

    def test_setup_failure_with_skipped_consumers_is_reported(self):
        needs = {
            "setup": {"result": "failure", "outputs": {"secret": "NEVER_SEND_THIS"}},
            "tests": {"result": "skipped"},
            "lint": {"result": "success"},
            "cancelled_job": {"result": "cancelled"},
        }
        payload = build_payload(json.dumps(needs), CONTEXT)
        text = payload["blocks"][2]["text"]["text"]
        self.assertTrue(text.startswith("setup: failure"))
        for job, value in needs.items():
            self.assertIn(f"{job}: {value['result']}", text)
        self.assertNotIn("NEVER_SEND_THIS", json.dumps(payload))
        self.assertNotIn("outputs", json.dumps(payload))

    def test_run_link_identifies_the_attempt(self):
        payload = build_payload('{"tests":{"result":"failure"}}', CONTEXT)
        self.assertIn(
            "/actions/runs/123/attempts/2", payload["blocks"][-1]["text"]["text"]
        )
        self.assertIn("Run attempt: 2", payload["blocks"][1]["text"]["text"])

    def test_metadata_is_plain_text_and_json_escaped(self):
        malicious = (
            '<!channel> "quotes"\n$(touch /tmp/not-executed) | <https://example.com>'
        )
        payload = build_payload(
            json.dumps({malicious: {"result": "failure"}}),
            {**CONTEXT, "GITHUB_WORKFLOW": malicious, "GITHUB_REF_NAME": malicious},
        )
        self.assertEqual(json.loads(json.dumps(payload)), payload)
        for block in payload["blocks"][:-1]:
            self.assertEqual(block["text"]["type"], "plain_text")
        self.assertIn(malicious, payload["blocks"][2]["text"]["text"])
        self.assertNotIn("<!channel>", payload["text"])
        self.assertIn("&lt;!channel&gt;", payload["text"])

    def test_large_matrix_payload_respects_slack_limits(self):
        needs = {f"job_{i}": {"result": "success"} for i in range(1000)}
        needs["zz_failed"] = {"result": "failure"}
        payload = build_payload(
            json.dumps(needs), {**CONTEXT, "GITHUB_WORKFLOW": "x" * 5000}
        )
        self.assertLessEqual(len(payload["blocks"][0]["text"]["text"]), 150)
        for block in payload["blocks"][1:]:
            self.assertLessEqual(len(block["text"]["text"]), 3000)
        summary = payload["blocks"][2]["text"]["text"]
        self.assertTrue(summary.startswith("zz_failed: failure"))
        self.assertIn("more jobs", summary)

    def test_invalid_needs_is_rejected_without_echoing_data(self):
        for needs in (
            "SECRET_DATA",
            "null",
            "[]",
            '{"x":null}',
            '{"x":{}}',
            '{"x":{"result":false}}',
            '{"x":{"result":"SECRET_DATA"}}',
        ):
            with self.subTest(needs=needs):
                with self.assertRaises(ValueError) as error:
                    build_payload(needs, CONTEXT)
                self.assertNotIn("SECRET_DATA", str(error.exception))

    def test_invalid_run_coordinates_are_rejected(self):
        for key, value in (
            ("GITHUB_SERVER_URL", "https://user@example.com"),
            ("GITHUB_SERVER_URL", "https://example.com?query=1"),
            ("GITHUB_SERVER_URL", "https://example.com|<!channel>"),
            ("GITHUB_SERVER_URL", "file:///tmp/x"),
            ("GITHUB_REPOSITORY", "owner/repo|<!channel>"),
            ("GITHUB_RUN_ID", "0"),
            ("GITHUB_RUN_ATTEMPT", "bad"),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    build_payload(
                        '{"tests":{"result":"failure"}}', {**CONTEXT, key: value}
                    )

    def test_cli_emits_payload_or_nothing_and_rejects_invalid_json(self):
        for needs, expected in (
            ("{}", 0),
            ('{"setup":{"result":"failure"}}', 0),
            ("bad", 1),
        ):
            with self.subTest(needs=needs):
                result = subprocess.run(
                    [sys.executable, str(ACTION_DIR / "failure_summary.py")],
                    env={**os.environ, **CONTEXT, "NEEDS_JSON": needs},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                if needs == "{}":
                    self.assertEqual(result.stdout, "")
                elif expected == 0:
                    self.assertEqual(
                        json.loads(result.stdout)["blocks"][0]["type"], "header"
                    )
                else:
                    self.assertEqual(result.stdout, "")

    def run_action(self, needs, curl_status=0):
        # Execute the actual composite shell payload, not a separately copied implementation.
        action = (ACTION_DIR / "action.yml").read_text()
        shell = "\n".join(
            line[8:] for line in action.split("      run: |\n", 1)[1].splitlines()
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            capture = tmp / "payload.json"
            curl = tmp / "curl"
            curl.write_text(
                "#!/bin/bash\n"
                'printf "%s" "$MESSAGE" > "$CAPTURE"\n'
                f"exit {curl_status}\n"
            )
            curl.chmod(0o755)
            python = tmp / "python3"
            python.symlink_to(sys.executable)
            output = tmp / "output"
            result = subprocess.run(
                ["bash", "-e", "-u", "-o", "pipefail", "-c", shell],
                env={
                    **os.environ,
                    **CONTEXT,
                    "NEEDS_JSON": needs,
                    "WEBHOOK": "https://example.invalid/slack",
                    "GITHUB_ACTION_PATH": str(ACTION_DIR.resolve()),
                    "GITHUB_OUTPUT": str(output),
                    "CAPTURE": str(capture),
                    "PATH": f"{tmp}:{os.environ['PATH']}",
                },
                capture_output=True,
                text=True,
            )
            return (
                result,
                output.read_text() if output.exists() else "",
                capture.read_text() if capture.exists() else "",
            )

    def test_action_skips_sender_without_failures(self):
        result, output, captured = self.run_action('{"test":{"result":"success"}}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "notified=false\n")
        self.assertEqual(captured, "")

    def test_action_reuses_shared_sender_and_marks_success(self):
        result, output, captured = self.run_action('{"test":{"result":"failure"}}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "notified=true\n")
        self.assertIn("test: failure", captured)
        self.assertEqual(json.loads(captured)["blocks"][-1]["text"]["type"], "mrkdwn")

    def test_action_propagates_delivery_failure(self):
        result, output, captured = self.run_action(
            '{"test":{"result":"failure"}}', curl_status=22
        )
        self.assertEqual(result.returncode, 22, result.stderr)
        self.assertEqual(output, "")
        self.assertTrue(captured)

    def test_action_invalid_input_does_not_send(self):
        result, output, captured = self.run_action("INVALID")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output, "")
        self.assertEqual(captured, "")


if __name__ == "__main__":
    unittest.main()
