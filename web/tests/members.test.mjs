import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript");
function load(file, deps = {}) {
  const source = readFileSync(
    new URL(`../src/features/onboarding/${file}`, import.meta.url),
    "utf8",
  );
  const code = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
      esModuleInterop: true,
    },
  }).outputText;
  const mod = { exports: {} };
  new Function("require", "module", "exports", code)(
    (name) => (name in deps ? deps[name] : req(name)),
    mod,
    mod.exports,
  );
  return mod.exports;
}
const member = {
  user_id: "actual-id",
  email: "<script>@example.test",
  role: "viewer",
  active: true,
  version: 3,
};
test("reviewed member snapshot rejects another user, version, status or role after refresh", () => {
  const api = load("members-api.ts", { "@/lib/api": {} });
  assert.equal(api.sameMemberReview(member, { ...member }), true);
  for (const change of [
    { version: 4 },
    { user_id: "foreign" },
    { active: false },
    { role: "owner" },
  ])
    assert.equal(api.sameMemberReview(member, { ...member, ...change }), false);
});
test("member save sends captured version and CSRF once, and does not retry conflict", async () => {
  const api = load("members-api.ts", { "@/lib/api": {} }),
    calls = [];
  const previous = globalThis.fetch;
  try {
    globalThis.fetch = async (url, options) => {
      calls.push({ url, options });
      return new Response(
        JSON.stringify({ detail: "MEMBERSHIP_VERSION_CHANGED" }),
        { status: 409 },
      );
    };
    await assert.rejects(
      api.membersApi.change(
        "workspace",
        member,
        { role: "operator", active: false },
        "csrf",
      ),
      /changed.*review/i,
    );
    assert.equal(calls.length, 1);
    assert.deepEqual(JSON.parse(calls[0].options.body), {
      role: "operator",
      active: false,
      expected_version: 3,
    });
    assert.equal(calls[0].options.headers["x-csrf-token"], "csrf");
    assert.equal(calls[0].options.credentials, "same-origin");
  } finally {
    globalThis.fetch = previous;
  }
});
test("review shows exact old and proposed role/status with inert identity and no email claim", () => {
  const React = req("react"),
    { renderToStaticMarkup } = req("react-dom/server");
  const api = load("members-api.ts", { "@/lib/api": {} });
  const ui = load("members.tsx", {
    "@tanstack/react-query": {},
    "./members-api": api,
  });
  const html = renderToStaticMarkup(
    React.createElement(ui.MemberReview, {
      review: { member, change: { role: "operator", active: false } },
      blocked: false,
      pending: false,
      onSave() {},
      onCancel() {},
    }),
  );
  assert.match(html, /viewer → operator/);
  assert.match(html, /Active → Inactive/);
  assert.match(html, /version 3/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /Save reviewed membership/);
});
