# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SEND_SLACK_ALERT = Path(__file__).with_name("send_slack_alert.sh")


class _SlackHandler(BaseHTTPRequestHandler):
    response_status = 200
    requests: list[bytes] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.requests.append(body)
        self.send_response(self.response_status)
        self.end_headers()
        self.wfile.write(b"ok" if self.response_status == 200 else b"invalid_payload")

    def log_message(self, format: str, *args: object) -> None:
        pass


class SendSlackAlertTest(unittest.TestCase):
    def setUp(self) -> None:
        _SlackHandler.requests.clear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _SlackHandler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.tmp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tmp_dir.cleanup()

    def send(self) -> subprocess.CompletedProcess[str]:
        host, port = self.server.server_address
        tmp_path = Path(self.tmp_dir.name)
        curl = tmp_path / "curl"
        curl.write_text(
            f"#!{sys.executable}\n"
            "import os, sys, urllib.error, urllib.request\n"
            "request = urllib.request.Request(sys.argv[-1], data=os.environ['MESSAGE'].encode(), method='POST')\n"
            "try:\n"
            "    response = urllib.request.urlopen(request)\n"
            "    print(response.read().decode(), end='')\n"
            "except urllib.error.HTTPError as error:\n"
            "    print(error.read().decode(), end='')\n"
            "    raise SystemExit(22 if '--fail-with-body' in sys.argv else 0)\n"
        )
        curl.chmod(0o755)
        env = {
            **os.environ,
            "MESSAGE": '{"text":"test"}',
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "WEBHOOK": f"http://{host}:{port}",
        }
        return subprocess.run(
            [str(SEND_SLACK_ALERT)], capture_output=True, text=True, env=env
        )

    def test_slack_http_rejection_fails_the_action(self) -> None:
        _SlackHandler.response_status = 200
        accepted = self.send()
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        _SlackHandler.response_status = 400
        rejected = self.send()
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("invalid_payload", rejected.stdout)
        self.assertEqual(
            _SlackHandler.requests, [b'{"text":"test"}', b'{"text":"test"}']
        )


if __name__ == "__main__":
    unittest.main()
