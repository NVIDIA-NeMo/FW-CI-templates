// Copyright (c) 2026, NVIDIA CORPORATION. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const test = require("node:test");

const orchestrator = require("./mbridge_orchestrator.js");

function config(overrides = {}) {
  return {
    appId: "123",
    privateKey: "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----",
    mcoreRef: "a".repeat(40),
    testSuite: "L1",
    pollTimeoutSeconds: 12600,
    callerRepository: "NVIDIA/Megatron-LM",
    runId: "456",
    runAttempt: "2",
    serverUrl: "https://github.com",
    ...overrides,
  };
}

test("validateConfig accepts the bounded caller contract", () => {
  assert.doesNotThrow(() => orchestrator.validateConfig(config()));
  assert.equal(orchestrator.testingBranch(config()), "mcore-testing-456-2");
  assert.equal(
    orchestrator.triggeredBy(config()),
    "https://github.com/NVIDIA/Megatron-LM/actions/runs/456",
  );
});

test("validateConfig rejects untrusted caller inputs", () => {
  assert.throws(
    () =>
      orchestrator.validateConfig(config({ callerRepository: "other/repo" })),
    /only accepts calls/,
  );
  assert.throws(
    () => orchestrator.validateConfig(config({ testSuite: "arbitrary" })),
    /Unsupported/,
  );
  assert.throws(
    () => orchestrator.validateConfig(config({ mcoreRef: "main" })),
    /full lowercase commit SHA/,
  );
});

test("createAppJwt creates a verifiable short-lived JWT", () => {
  const { privateKey, publicKey } = crypto.generateKeyPairSync("rsa", {
    modulusLength: 2048,
  });
  const jwt = orchestrator.createAppJwt("123", privateKey, 1_800_000);
  const [header, payload, signature] = jwt.split(".");
  assert.equal(JSON.parse(Buffer.from(header, "base64url")).alg, "RS256");
  assert.deepEqual(JSON.parse(Buffer.from(payload, "base64url")), {
    iat: 1740,
    exp: 2280,
    iss: "123",
  });
  assert.equal(
    crypto.verify(
      "RSA-SHA256",
      Buffer.from(`${header}.${payload}`),
      publicKey,
      Buffer.from(signature, "base64url"),
    ),
    true,
  );
});

test("pollRun refreshes actions-read tokens before expiry", async () => {
  let now = 0;
  const permissions = [];
  const tokens = [
    { token: "first", expiresAtMs: 650_000 },
    { token: "second", expiresAtMs: 4_000_000 },
  ];
  const statuses = [
    { status: "in_progress", conclusion: null },
    { status: "completed", conclusion: "success" },
  ];
  await orchestrator.pollRun(
    99,
    1000,
    async (requestedPermissions) => {
      permissions.push(requestedPermissions);
      return tokens.shift();
    },
    {
      now: () => now,
      sleep: async (milliseconds) => {
        now += milliseconds;
      },
      apiRequest: async (token) => {
        assert.equal(token, permissions.length === 1 ? "first" : "second");
        return statuses.shift();
      },
    },
  );
  assert.deepEqual(permissions, [{ actions: "read" }, { actions: "read" }]);
});

test("pollRun fails closed when the downstream workflow times out", async () => {
  let now = 0;
  await assert.rejects(
    orchestrator.pollRun(
      99,
      60,
      async () => ({ token: "read", expiresAtMs: 4_000_000 }),
      {
        now: () => now,
        sleep: async (milliseconds) => {
          now += milliseconds;
        },
        apiRequest: async () => ({ status: "in_progress", conclusion: null }),
      },
    ),
    /Timed out/,
  );
});

test("orchestrate scopes tokens and cleans up after success", async () => {
  const calls = [];
  const permissions = [];
  const responses = [
    { object: { sha: "b".repeat(40) } },
    null,
    { workflow_runs: [] },
    null,
    {
      workflow_runs: [
        {
          id: 77,
          event: "workflow_dispatch",
          head_branch: "mcore-testing-456-2",
          created_at: "1970-01-01T00:00:00.000Z",
        },
      ],
    },
    { status: "completed", conclusion: "success" },
    null,
  ];
  await orchestrator.orchestrate(config(), {
    now: () => 0,
    sleep: async () => {},
    mintToken: async (requestedPermissions) => {
      permissions.push(requestedPermissions);
      return { token: `token-${permissions.length}`, expiresAtMs: 4_000_000 };
    },
    apiRequest: async (token, method, path, body) => {
      calls.push({ token, method, path, body });
      return responses.shift();
    },
  });
  assert.deepEqual(permissions, [
    { contents: "write" },
    { actions: "write" },
    { actions: "read" },
    { contents: "write" },
  ]);
  assert.equal(calls.at(-1).method, "DELETE");
  assert.equal(calls.at(-1).token, "token-4");
  assert.deepEqual(calls[3].body.inputs, {
    mcore_ref: "a".repeat(40),
    test_suite: "L1",
    triggered_by: "https://github.com/NVIDIA/Megatron-LM/actions/runs/456",
  });
});

test("orchestrate preserves the test failure when cleanup also fails", async () => {
  let call = 0;
  const expected = new Error("downstream failed");
  await assert.rejects(
    orchestrator.orchestrate(config(), {
      now: () => 0,
      sleep: async () => {},
      mintToken: async () => ({ token: "token", expiresAtMs: 4_000_000 }),
      apiRequest: async (_token, method) => {
        call += 1;
        if (call === 1) return { object: { sha: "b".repeat(40) } };
        if (call === 2) return null;
        if (call === 3) throw expected;
        if (method === "DELETE") throw new Error("cleanup failed");
        return null;
      },
    }),
    (error) => error === expected,
  );
});
