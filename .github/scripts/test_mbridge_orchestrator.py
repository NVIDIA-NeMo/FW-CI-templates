# Copyright (c) 2026, NVIDIA CORPORATION. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import json
import subprocess
import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


def load_embedded_orchestrator() -> types.ModuleType:
    workflow = Path(__file__).parents[1] / "workflows/_mbridge_orchestrator.yml"
    lines = workflow.read_text(encoding="utf-8").splitlines()
    start = lines.index("          python3 <<'MBRIDGE_ORCHESTRATOR'") + 1
    end = lines.index("          MBRIDGE_ORCHESTRATOR", start)
    embedded_lines = lines[start:end]
    if not embedded_lines or any(
        line and not line.startswith("          ") for line in embedded_lines
    ):
        raise RuntimeError("Embedded orchestrator indentation is invalid")
    source = "\n".join(line[10:] if line else "" for line in embedded_lines) + "\n"
    module = types.ModuleType("mbridge_orchestrator")
    module.__file__ = f"{workflow}:embedded-python"
    sys.modules[module.__name__] = module
    exec(  # noqa: S102 - execute the reviewed workflow payload under test
        compile(source, module.__file__, "exec"), module.__dict__
    )
    return module


orchestrator = load_embedded_orchestrator()
Config = orchestrator.Config
Credential = orchestrator.Credential


class FakeClock:
    def __init__(self) -> None:
        self.now_ms = 0

    def now(self) -> int:
        return self.now_ms

    def sleep(self, milliseconds: int) -> None:
        self.now_ms += milliseconds


def config(**overrides: Any) -> Config:
    values: dict[str, Any] = {
        "app_id": "123",
        "private_key": (
            "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----"  # pragma: allowlist secret
        ),
        "mcore_ref": "a" * 40,
        "test_suite": "L1",
        "poll_timeout_seconds": 12600,
        "caller_repository": "NVIDIA/Megatron-LM",
        "run_id": "456",
        "run_attempt": "2",
        "server_url": "https://github.com",
    }
    values.update(overrides)
    return Config(**values)


class OrchestratorTests(unittest.TestCase):
    def test_validate_config_accepts_bounded_caller_contract(self) -> None:
        orchestrator.validate_config(config())
        self.assertEqual(orchestrator.testing_branch(config()), "mcore-testing-456-2")
        self.assertEqual(
            orchestrator.triggered_by(config()),
            "https://github.com/NVIDIA/Megatron-LM/actions/runs/456",
        )

    def test_validate_config_rejects_untrusted_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "only accepts calls"):
            orchestrator.validate_config(config(caller_repository="other/repo"))
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            orchestrator.validate_config(config(test_suite="arbitrary"))
        with self.assertRaisesRegex(ValueError, "full lowercase commit SHA"):
            orchestrator.validate_config(config(mcore_ref="main"))

    def test_create_app_jwt_is_short_lived_and_invokes_openssl(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["openssl"], returncode=0, stdout=b"signed", stderr=b""
        )
        with mock.patch.object(
            orchestrator.subprocess, "run", return_value=completed
        ) as run:
            jwt = orchestrator.create_app_jwt(
                "123",
                "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----",  # pragma: allowlist secret
                1_800_000,
            )
        header, payload, signature = jwt.split(".")
        for encoded in (header, payload):
            padding = "=" * (-len(encoded) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(encoded + padding))
            if encoded == header:
                self.assertEqual(decoded, {"alg": "RS256", "typ": "JWT"})
            else:
                self.assertEqual(decoded, {"iat": 1740, "exp": 2280, "iss": "123"})
        self.assertEqual(
            base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)), b"signed"
        )
        args, kwargs = run.call_args
        self.assertEqual(args[0][:4], ["openssl", "dgst", "-sha256", "-sign"])
        self.assertEqual(kwargs["input"], f"{header}.{payload}".encode())
        self.assertTrue(kwargs["check"])

    def test_poll_run_refreshes_actions_read_tokens(self) -> None:
        clock = FakeClock()
        permissions: list[dict[str, str]] = []
        tokens = iter([Credential("first", 650_000), Credential("second", 4_000_000)])
        statuses = iter(
            [
                {"status": "in_progress", "conclusion": None},
                {"status": "completed", "conclusion": "success"},
            ]
        )

        def mint(requested: dict[str, str]) -> Credential:
            permissions.append(requested)
            return next(tokens)

        def request(
            token: str, method: str, path: str, body: dict[str, Any] | None
        ) -> dict[str, Any]:
            self.assertEqual(token, "first" if len(permissions) == 1 else "second")
            return next(statuses)

        orchestrator.poll_run(
            99, 1000, mint, request=request, now=clock.now, sleep=clock.sleep
        )
        self.assertEqual(permissions, [{"actions": "read"}, {"actions": "read"}])

    def test_poll_run_fails_closed_on_timeout(self) -> None:
        clock = FakeClock()
        with self.assertRaisesRegex(TimeoutError, "Timed out"):
            orchestrator.poll_run(
                99,
                60,
                lambda _: Credential("read", 4_000_000),
                request=lambda *_: {"status": "in_progress", "conclusion": None},
                now=clock.now,
                sleep=clock.sleep,
            )

    def test_orchestrate_scopes_tokens_and_cleans_up(self) -> None:
        calls: list[tuple[str, str, str, dict[str, Any] | None]] = []
        permissions: list[dict[str, str]] = []
        responses = iter(
            [
                {"object": {"sha": "b" * 40}},
                None,
                {"workflow_runs": []},
                None,
                {
                    "workflow_runs": [
                        {
                            "id": 77,
                            "event": "workflow_dispatch",
                            "head_branch": "mcore-testing-456-2",
                            "created_at": "1970-01-01T00:00:00.000Z",
                        }
                    ]
                },
                {"status": "completed", "conclusion": "success"},
                None,
            ]
        )

        def mint(requested: dict[str, str]) -> Credential:
            permissions.append(requested)
            return Credential(f"token-{len(permissions)}", 4_000_000)

        def request(
            token: str, method: str, path: str, body: dict[str, Any] | None
        ) -> dict[str, Any] | None:
            calls.append((token, method, path, body))
            return next(responses)

        orchestrator.orchestrate(
            config(),
            request=request,
            now=lambda: 0,
            sleep=lambda _: None,
            mint_token=mint,
        )
        self.assertEqual(
            permissions,
            [
                {"contents": "write"},
                {"actions": "write"},
                {"actions": "read"},
                {"contents": "write"},
            ],
        )
        self.assertEqual(calls[-1][1], "DELETE")
        self.assertEqual(calls[-1][0], "token-4")
        self.assertEqual(
            calls[3][3]["inputs"],
            {
                "mcore_ref": "a" * 40,
                "test_suite": "L1",
                "triggered_by": "https://github.com/NVIDIA/Megatron-LM/actions/runs/456",
            },
        )

    def test_orchestrate_preserves_test_failure_when_cleanup_fails(self) -> None:
        expected = RuntimeError("downstream failed")
        call = 0

        def request(
            token: str, method: str, path: str, body: dict[str, Any] | None
        ) -> dict[str, Any] | None:
            nonlocal call
            call += 1
            if call == 1:
                return {"object": {"sha": "b" * 40}}
            if call == 2:
                return None
            if call == 3:
                raise expected
            if method == "DELETE":
                raise RuntimeError("cleanup failed")
            return None

        with self.assertRaisesRegex(RuntimeError, "downstream failed"):
            orchestrator.orchestrate(
                config(),
                request=request,
                now=lambda: 0,
                sleep=lambda _: None,
                mint_token=lambda _: Credential("token", 4_000_000),
            )


if __name__ == "__main__":
    unittest.main()
