import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript"),
  React = req("react");
const { renderToStaticMarkup } = req("react-dom/server");
function load(file) {
  const js = ts.transpileModule(
    readFileSync(
      new URL(`../src/features/onboarding/${file}`, import.meta.url),
      "utf8",
    ),
    {
      compilerOptions: {
        jsx: ts.JsxEmit.ReactJSX,
        module: ts.ModuleKind.CommonJS,
        esModuleInterop: true,
      },
    },
  ).outputText;
  const mod = { exports: {} };
  const local = (name) =>
    name === "next/link"
      ? ({ href, children }) => React.createElement("a", { href }, children)
      : name === "@/features/receivables/shared"
        ? { stamp: (v) => v, ErrorState: () => null }
        : name === "./demo-reset"
          ? load("demo-reset.tsx")
          : name === "./reset-api"
            ? load("reset-api.ts")
            : name === "./demo-api"
              ? load("demo-api.ts")
              : req(name);
  new Function("require", "module", "exports", js)(local, mod, mod.exports);
  return mod.exports;
}
const owner = {
  id: "workspace",
  role: "owner",
  is_demo: true,
  permissions: ["settings", "invoice_read"],
};
const data = {
  invoices: {
    items: [],
    total: 24,
    page: 1,
    page_size: 1,
    as_of_at: "2026-11-19T00:00:00Z",
  },
  customers: {
    items: [],
    total: 6,
    page: 1,
    page_size: 1,
    as_of_at: "2026-11-19T00:00:01Z",
  },
};
test("synthetic source import requires current Owner demo settings capability and CSRF before network", async () => {
  const { canImportDemo, demoApi } = load("demo-api.ts");
  const old = globalThis.fetch;
  let calls = 0;
  try {
    globalThis.fetch = async (path, options) => {
      calls++;
      assert.equal(path, "/api/workspaces/workspace/demo/accounting/seed");
      assert.equal(options.method, "POST");
      assert.equal(options.credentials, "same-origin");
      assert.equal(options.headers["x-csrf-token"], "csrf");
      assert.deepEqual(JSON.parse(options.body), {});
      return {
        ok: true,
        json: async () => ({
          provider_connection_id: "source",
          synthetic: true,
        }),
      };
    };
    assert.equal(canImportDemo(owner), true);
    for (const workspace of [
      { ...owner, role: "viewer" },
      { ...owner, is_demo: false },
      { ...owner, permissions: [] },
    ]) {
      assert.equal(canImportDemo(workspace), false);
      await assert.rejects(
        demoApi.import(workspace, "csrf"),
        /Owner|permission|demo/,
      );
    }
    await assert.rejects(demoApi.import(owner, ""), /sign in|csrf/i);
    assert.equal(calls, 0);
    assert.deepEqual(await demoApi.import(owner, "csrf"), {
      provider_connection_id: "source",
      synthetic: true,
    });
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = old;
  }
});
test("onboarding reads actual imported totals and timestamps from current workspace APIs", async () => {
  const { demoApi } = load("demo-api.ts");
  const old = globalThis.fetch;
  const seen = [];
  try {
    globalThis.fetch = async (path, options) => {
      seen.push(path);
      assert.equal(options.credentials, "same-origin");
      return {
        ok: true,
        json: async () =>
          path.includes("/receivables?") ? data.invoices : data.customers,
      };
    };
    assert.deepEqual(await demoApi.read("workspace"), data);
    assert.deepEqual(seen.sort(), [
      "/api/workspaces/workspace/customers?page_size=1",
      "/api/workspaces/workspace/receivables?page_size=1",
    ]);
  } finally {
    globalThis.fetch = old;
  }
});
test("import result displays server counts and existing-data preservation without fabricated source success", () => {
  const { DemoAccountingResult } = load("demo-accounting.tsx");
  const html = renderToStaticMarkup(
    React.createElement(DemoAccountingResult, { data, workspace: "workspace" }),
  );
  assert.match(html, /24 imported invoices/);
  assert.match(html, /6 imported customers/);
  assert.match(html, /2026-11-19T00:00:00Z/);
  assert.match(html, /receivables\?workspace=workspace/);
  assert.match(html, /customers\?workspace=workspace/);
  assert.doesNotMatch(html, /outreach enabled|paid service|reset complete/i);
  const existing = renderToStaticMarkup(
    React.createElement(DemoAccountingResult, {
      data: { ...data, invoices: { ...data.invoices, total: 32 } },
      workspace: "workspace",
    }),
  );
  assert.match(existing, /32 imported invoices/);
});
test("disabled or uncertain source commands remain explicit errors rather than invented ready data", async () => {
  const { demoApi } = load("demo-api.ts");
  const old = globalThis.fetch;
  try {
    globalThis.fetch = async () => ({ ok: false, status: 503 });
    await assert.rejects(
      demoApi.import(owner, "csrf"),
      /not configured|unavailable/i,
    );
    globalThis.fetch = async () => ({ ok: false, status: 403 });
    await assert.rejects(demoApi.import(owner, "csrf"), /current|Owner/i);
    globalThis.fetch = async () => ({
      ok: true,
      json: async () => ({
        provider_connection_id: "source",
        synthetic: false,
      }),
    });
    await assert.rejects(demoApi.import(owner, "csrf"), /synthetic/i);
  } finally {
    globalThis.fetch = old;
  }
});
