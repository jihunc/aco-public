import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";

const require = createRequire(new URL("../package.json", import.meta.url));
const ts = require("typescript");
const module = { exports: {} };
new Function("module", "exports", ts.transpileModule(
  readFileSync(new URL("../src/lib/sign-out.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
).outputText)(module, module.exports);
const { signOut } = module.exports;

test("successful sign-out posts the CSRF token and leaves for the login page", async () => {
  let request; const visited = [];
  const ok = await signOut("csrf-1", async (url, init) => { request = { url, ...init }; return new Response("{}", { status: 200 }); }, (to) => visited.push(to));
  assert.equal(ok, true);
  assert.equal(request.url, "/api/auth/logout");
  assert.equal(request.method, "POST");
  assert.equal(new Headers(request.headers).get("x-csrf-token"), "csrf-1");
  assert.deepEqual(visited, ["/login"]);
});

test("a refused or failed sign-out never pretends the session ended", async () => {
  const visited = [];
  assert.equal(await signOut("csrf-1", async () => new Response("{}", { status: 403 }), (to) => visited.push(to)), false);
  assert.equal(await signOut("csrf-1", async () => { throw new Error("offline"); }, (to) => visited.push(to)), false);
  assert.deepEqual(visited, []);
});

test("a missing CSRF token is not sent as an empty credential", async () => {
  let called = false;
  assert.equal(await signOut("", async () => { called = true; return new Response("{}"); }, () => {}), false);
  assert.equal(called, false);
});
