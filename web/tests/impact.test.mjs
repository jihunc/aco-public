import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { libDouble } from "./lib-support.mjs";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript"),
  React = req("react");
const { renderToStaticMarkup } = req("react-dom/server");
function load(file) {
  const output = ts.transpileModule(
    readFileSync(
      new URL(`../src/features/impact/${file}`, import.meta.url),
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
      : name === "next/navigation"
        ? {}
        : name === "@/lib/money"
          ? load("../../lib/money.ts")
          : name === "@/features/receivables/shared"
            ? { stamp: (v) => v ?? "Unavailable", ErrorState: () => null }
            : name === "@/features/receivables/overview"
              ? {}
              : name === "@/features/usage/details"
                ? {}
                : ["./cohorts", "./case-outcomes", "./economics"].includes(name)
                  ? {}
                  : name === "./api"
                    ? load("api.ts")
                    : name.endsWith(".css")
                      ? {}
                      : req(name);
  new Function("require", "module", "exports", output)((n) => libDouble(n) ?? local(n), mod, mod.exports);
  return mod.exports;
}
const group = {
  currency: "AUD",
  exponent: 2,
  synthetic: true,
  cash: { minor_units: "10000000000000001", currency: "AUD", exponent: 2 },
  outflows: { minor_units: "100", currency: "AUD", exponent: 2 },
  net_cash: { minor_units: "9999999999999901", currency: "AUD", exponent: 2 },
  allocation_count: 2,
  non_cash_count: 3,
  excluded_count: 0,
  completeness: "partial",
  freshness: "stale",
  last_synced_at: "2026-11-18T00:00:00Z",
  issues: [],
  evidence: [],
};
test("known imported cash stays exact and prominently partial/synthetic/stale", () => {
  const { CashRecord } = load("dashboard.tsx");
  const html = renderToStaticMarkup(
    React.createElement(CashRecord, { group, workspace: "w" }),
  );
  assert.match(html, /AUD 99,999,999,999,999.01/);
  assert.match(html, /Synthetic accounting/);
  assert.match(html, /partial/);
  assert.match(html, /stale/);
  assert.match(html, /3 non-cash/);
  assert.doesNotMatch(
    html,
    /revenue|ROI|resolved successfully|incremental collection/i,
  );
});
test("an invalid linked adjustment never displays an invented net total", () => {
  const { CashRecord } = load("dashboard.tsx");
  const html = renderToStaticMarkup(
    React.createElement(CashRecord, {
      group: {
        ...group,
        net_cash: null,
        excluded_count: 1,
        completeness: "unavailable",
        issues: [
          {
            allocation_id: "bad-allocation",
            invoice_id: "i",
            reason: "PARENT_SCOPE_MISMATCH",
          },
        ],
      },
      workspace: "w",
    }),
  );
  assert.match(html, /Net cash unavailable/);
  assert.match(html, /different invoice|different source|different.*currency/i);
  assert.doesNotMatch(html, /impact-net-AUD/);
});
test("activity records link genuine saved IDs without equating creation with resolution", () => {
  const { ActivityRecords } = load("dashboard.tsx");
  const html = renderToStaticMarkup(
    React.createElement(ActivityRecords, {
      workspace: "w",
      groups: [
        {
          kind: "agent_runs",
          synthetic: true,
          count: 1,
          statuses: { queued: 1 },
        },
      ],
      records: [
        {
          kind: "agent_runs",
          id: "run-real",
          invoice_id: null,
          status: "queued",
          synthetic: true,
          created_at: "2026-11-18T00:00:00Z",
        },
        {
          kind: "cases",
          id: "case-real",
          invoice_id: null,
          status: "OPEN",
          synthetic: true,
          created_at: "2026-11-18T00:00:00Z",
        },
      ],
    }),
  );
  assert.match(html, /agent-runs\/run-real\?workspace=w/);
  assert.match(html, /cases\/case-real\?workspace=w/);
  assert.match(html, /not.*resolution/i);
  assert.doesNotMatch(html, /100%|achieved|case resolved/i);
});
test("impact query retains its explicit aware half-open window and rejects numeric Money responses", async () => {
  const { impactApi } = load("api.ts");
  const old = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    const url = new URL(path, "http://local.invalid");
    assert.equal(url.searchParams.get("since"), "2026-11-01T00:00:00Z");
    assert.equal(url.searchParams.get("until"), "2026-11-18T00:00:00Z");
    assert.equal(init.credentials, "same-origin");
    return {
      ok: true,
      json: async () => ({
        cash: [
          {
            ...group,
            net_cash: {
              ...group.net_cash,
              minor_units: Number("9999999999999901"),
            },
          },
        ],
        operations: { currencies: [] },
      }),
    };
  };
  try {
    await assert.rejects(
      impactApi.read("w", {
        since: "2026-11-01T00:00:00Z",
        until: "2026-11-18T00:00:00Z",
      }),
      /exact|precision/i,
    );
  } finally {
    globalThis.fetch = old;
  }
});
test("UTC window controls preserve offsets without silently relabeling local time", () => {
  const { utcWindowInput } = load("dashboard.tsx");
  assert.equal(
    utcWindowInput("2026-11-18T11:30:00+11:00"),
    "2026-11-18T00:30:00",
  );
  assert.equal(utcWindowInput("2026-11-18T11:30:00"), "");
  assert.equal(utcWindowInput("not-a-dateZ"), "");
});
