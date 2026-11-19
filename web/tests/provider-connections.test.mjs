import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript");
function load() {
  const path = new URL("../src/features/communications/connection-api.ts", import.meta.url);
  const js = ts.transpileModule(readFileSync(path, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
  const mod = { exports: {} };
  new Function("require", "module", "exports", js)(req, mod, mod.exports);
  return mod.exports;
}
test("company connection review preserves captured version and uses current CSRF", async () => {
  const api = load();
  const row = { id: "old/connection", version: 4 };
  const captured = api.captureDisconnect(row);
  row.version = 5;
  const requests = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    return { ok: true, json: async () => ({ status: "DISCONNECTED", remote_revocation: "UNKNOWN" }) };
  };
  try {
    await api.listConnections("company/a");
    const result = await api.disconnectConnection("company/a", captured, "current-csrf");
    assert.equal(requests[0].options.method, "GET");
    assert.equal(requests[1].url, "/api/workspaces/company%2Fa/provider-connections/old%2Fconnection/disconnect");
    assert.deepEqual(JSON.parse(requests[1].options.body), { expected_version: 4 });
    assert.equal(requests[1].options.headers["x-csrf-token"], "current-csrf");
    assert.equal(result.remote_revocation, "UNKNOWN");
  } finally { globalThis.fetch = original; }
});
