import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
const requireFromWeb = createRequire(
  new URL("../package.json", import.meta.url),
);
const ts = requireFromWeb("typescript");
const source = readFileSync(
  new URL("../src/features/agent/api.ts", import.meta.url),
  "utf8",
);
const module = { exports: {} };
new Function(
  "module",
  "exports",
  ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText,
)(module, module.exports);
const { agentApi, isActiveRun } = module.exports;
test("start preserves reviewed scope, objective and idempotency with same-origin CSRF", async () => {
  let request;
  globalThis.fetch = async (path, options) => {
    request = { path, ...options };
    return Response.json({ id: "run-1" }, { status: 202 });
  };
  const command = {
    invoice_ids: ["invoice-1"],
    objective: "Verify evidence",
    request_id: "request-1",
  };
  assert.deepEqual(await agentApi.start("workspace-1", command, "csrf-1"), {
    id: "run-1",
  });
  assert.equal(request.path, "/api/workspaces/workspace-1/agent-runs");
  assert.equal(request.credentials, "same-origin");
  assert.equal(request.headers["x-csrf-token"], "csrf-1");
  assert.deepEqual(JSON.parse(request.body), command);
});
test("cancel sends the explicitly reviewed version and never retries a conflict", async () => {
  const requests = [];
  globalThis.fetch = async (path, options) => {
    requests.push({ path, ...options });
    return Response.json({ detail: "RUN_VERSION_CONFLICT" }, { status: 409 });
  };
  await assert.rejects(
    agentApi.cancel("workspace-1", "run-1", 4, "csrf-1"),
    /Refresh its current state/,
  );
  assert.equal(requests.length, 1);
  assert.deepEqual(JSON.parse(requests[0].body), { expected_version: 4 });
});
test("event replay reads only after the supplied committed watermark", async () => {
  let path;
  globalThis.fetch = async (url) => {
    path = url;
    return Response.json({ items: [], last_event_sequence: 9 });
  };
  await agentApi.events("workspace-1", "run-1", 7);
  assert.equal(
    path,
    "/api/workspaces/workspace-1/agent-runs/run-1/event-log?after=7",
  );
  assert.equal(isActiveRun("running"), true);
  assert.equal(isActiveRun("failed"), false);
});
