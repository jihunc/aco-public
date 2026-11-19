import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { runInNewContext } from "node:vm";
import assert from "node:assert/strict";
import test from "node:test";
const requireWeb = createRequire(resolve("web/package.json"));
const ts = requireWeb("typescript");
const React = requireWeb("react");
const { renderToStaticMarkup } = requireWeb("react-dom/server");

test("usage component preserves exact decimal strings, partial basis, reserved exposure and draft authority", () => {
  const source = readFileSync("web/src/features/usage/details.tsx", "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  });
  const exports = {};
  runInNewContext(compiled.outputText, { exports, require: requireWeb });
  const view = {
    external_execution: "BLOCKED",
    currencies: [
      {
        currency: "USD",
        synthetic: true,
        known_amount: "0.000000000005",
        reported_amount: "0.000000000003",
        estimated_amount: "0.000000000002",
        unknown_count: 1,
        completeness: "partial",
        reserved_amount: "0.060000000000",
        spent_amount: "0.000000000000",
      },
      {
        currency: "AUD",
        synthetic: true,
        known_amount: "99999999999999999999999999.123456789012",
        reported_amount: "99999999999999999999999999.123456789012",
        estimated_amount: "0",
        unknown_count: 0,
        completeness: "complete",
        reserved_amount: "0",
        spent_amount: "0",
      },
    ],
    providers: [],
    bundle_issues: [],
    limits: [
      {
        id: "draft",
        dimension: "tenant",
        currency: "USD",
        amount: "0.100000000000",
        state: "DRAFT",
      },
    ],
    notices: [
      "Real external execution is BLOCKED. Draft limits do not authorize spending.",
      "Budget request blocked: CAP_REACHED:tenant",
    ],
    history_coverage:
      "Earlier operations may lack evidence; never inferred free.",
  };
  const html = renderToStaticMarkup(
    React.createElement(exports.UsageDetails, { view }),
  );
  for (const exact of [
    "Provider-reported fees",
    "Rate-estimated fees",
    "Known fees (reported + estimated)",
    "USD 0.000000000005",
    "USD 0.000000000002",
    "USD 0.000000000003",
    "AUD 99999999999999999999999999.123456789012",
    "USD 0.060000000000",
    "partial · 1 unknown charge",
    "DRAFT · tenant · USD 0.100000000000",
    "Real external execution is BLOCKED. Draft limits do not authorize spending.",
    "Budget request blocked: CAP_REACHED:tenant",
    "Currencies are separate; no FX conversion.",
    "Simulated fees are not actual vendor charges.",
    "Earlier operations may lack evidence; never inferred free.",
  ]) {
    assert.ok(html.includes(exact), exact);
  }
  assert.ok(html.includes("usage-known-USD-synthetic"));
  assert.ok(html.includes("usage-reserved-USD-synthetic"));
});

test("draft PUT reaches the fixed API upstream with exact body and CSRF headers", async () => {
  const source = readFileSync("web/src/app/api/[...path]/route.ts", "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  });
  const exports = {};
  const calls = [];
  runInNewContext(compiled.outputText, {
    exports, URL, Headers, Response, AbortSignal, AbortController, process: { env: {} },
    fetch: async (url, options) => {
      calls.push({ url: String(url), options });
      return Response.json({ limits: [{ amount: "0.100000000000", state: "DRAFT" }] }, { headers: { "set-cookie": "synthetic-response=retained; HttpOnly; SameSite=Lax" } });
    },
  });
  assert.equal(typeof exports.PUT, "function");
  const body = JSON.stringify({ currency: "USD", amount: "0.10", requests: 0 });
  const response = await exports.PUT(new Request("http://127.0.0.1:53000/api/workspaces/synthetic/usage/draft-limit", {
    method: "PUT",
    headers: { "content-type": "application/json", origin: "http://127.0.0.1:53000", "x-csrf-token": "synthetic-csrf", cookie: "synthetic-session" },
    body,
  }));
  assert.equal(response.status, 200);
  assert.deepEqual(response.headers.getSetCookie(), ["synthetic-response=retained; HttpOnly; SameSite=Lax"]);
  assert.equal((await response.json()).limits[0].amount, "0.100000000000");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://127.0.0.1:58000/api/workspaces/synthetic/usage/draft-limit");
  assert.equal(calls[0].options.method, "PUT");
  assert.equal(new TextDecoder().decode(calls[0].options.body), body);
  assert.equal(calls[0].options.headers.get("x-csrf-token"), "synthetic-csrf");
  assert.equal(calls[0].options.headers.get("origin"), "http://127.0.0.1:53000");
  assert.equal(calls[0].options.headers.get("cookie"), "synthetic-session");
});

test("unresolved bundle preserves known child evidence and labels subtotal without a complete total", () => {
  const compiled = ts.transpileModule(readFileSync("web/src/features/usage/details.tsx", "utf8"), {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  });
  const exports = {};
  runInNewContext(compiled.outputText, { exports, require: requireWeb });
  const view = {
    external_execution: "BLOCKED", notices: [], limits: [], providers: [], history_coverage: "Preserved history",
    currencies: [{ currency: "EUR", synthetic: true, known_amount: "0.020000000000", reported_amount: "0.020000000000", estimated_amount: "0", unknown_count: 0, bundle_issue_count: 1, completeness: "partial", reserved_amount: "0", spent_amount: "0" }],
    bundle_issues: [{ event_id: "child", parent_event_id: "parent", provider_connection_id: "connection", component: "carrier", parent_component: "provider_total", currency: "EUR", parent_currency: "EUR", component_amount: "0.080000000000", parent_amount: "0.020000000000", reason: "COMPONENT_EXCEEDS_PARENT" }],
  };
  const html = renderToStaticMarkup(React.createElement(exports.UsageDetails, { view }));
  for (const text of ["Total unavailable: unresolved bundle overlap", "Top-level known subtotal", "carrier: EUR 0.080000000000", "provider_total: EUR 0.020000000000", "COMPONENT_EXCEEDS_PARENT", "child", "parent"]) assert.ok(html.includes(text), text);
});

test("distinct sibling evidence remains separate and out-of-window context is explicit", () => {
  const compiled = ts.transpileModule(readFileSync("web/src/features/usage/details.tsx", "utf8"), {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  });
  const exports = {};
  runInNewContext(compiled.outputText, { exports, require: requireWeb });
  const view = {
    external_execution: "BLOCKED", notices: [], limits: [], providers: [], history_coverage: "Preserved history",
    currencies: [{ currency: "EUR", synthetic: true, known_amount: "0.100000000000", reported_amount: "0.100000000000", estimated_amount: "0", unknown_count: 0, bundle_issue_count: 2, completeness: "partial", reserved_amount: "0", spent_amount: "0" }],
    bundle_issues: ["carrier", "tts"].map((component) => ({ event_id: component, parent_event_id: "parent", provider_connection_id: "connection", component, parent_component: "provider_total", currency: "EUR", parent_currency: "EUR", component_amount: "0.080000000000", parent_amount: "0.100000000000", reason: "BUNDLE_COMPONENTS_EXCEED_PARENT", outside_filter: component === "tts" })),
  };
  const html = renderToStaticMarkup(React.createElement(exports.UsageDetails, { view }));
  for (const text of ["Total unavailable: unresolved bundle overlap", "Top-level known subtotal", "carrier: EUR 0.080000000000", "tts: EUR 0.080000000000", "provider_total: EUR 0.100000000000", "BUNDLE_COMPONENTS_EXCEED_PARENT", "Context outside selected time range"]) assert.ok(html.includes(text), text);
  assert.ok(!html.includes("EUR 0.160000000000"));
});
