import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript"),
  React = req("react");
const { renderToStaticMarkup } = req("react-dom/server");
const { QueryClient, QueryClientProvider } = req("@tanstack/react-query");
function load(relative) {
  const js = ts.transpileModule(
    readFileSync(new URL(`../src/${relative}`, import.meta.url), "utf8"),
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.ReactJSX,
        esModuleInterop: true,
      },
    },
  ).outputText;
  const mod = { exports: {} };
  const resolve = (name) =>
    name === "next/link"
      ? ({ href, children, ...props }) =>
          React.createElement("a", { href, ...props }, children)
      : name === "@/components/ui/button"
        ? {
            Button: ({ children, ...props }) =>
              React.createElement("button", props, children),
          }
        : name === "./shared"
          ? {
              stamp: (value) => value,
              ErrorState: ({ label }) => React.createElement("p", null, label),
            }
          : name.startsWith("@/")
            ? load(
                name.slice(2) +
                  (name.endsWith("api") || name.endsWith("money")
                    ? ".ts"
                    : ".tsx"),
              )
            : name === "./workload"
              ? load("features/receivables/workload.tsx")
              : req(name);
  new Function("require", "module", "exports", js)(resolve, mod, mod.exports);
  return mod.exports;
}
const workload = {
  as_of_at: "2026-11-19T00:00:00Z",
  open_tasks: 3,
  pending_approvals: 2,
  unresolved_cases: 4,
  active_promises: 1,
  unconfirmed_promises: 2,
  missed_promises: 1,
  active_investigations: 1,
  completed_investigations: 5,
  completed_live_investigations: 1,
  failed_investigations: 2,
  stopped_actions: 3,
  outbound_paused: true,
  automation_enabled: false,
  accounting_sources: [
    {
      connection_id: "c1",
      provider: "demo",
      status: "AUTH_EXPIRED",
      imported_invoice_count: 24,
      oldest_import_observation_at: null,
    },
  ],
};
function render(data) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  client.setQueryData(["operations", "company/a"], {
    availability: "unavailable",
    basis: "No imported balances",
    as_of_at: workload.as_of_at,
    currencies: [],
  });
  client.setQueryData(["overview-workload", "company/a"], data);
  const { Overview } = load("features/receivables/overview.tsx");
  return renderToStaticMarkup(
    React.createElement(
      QueryClientProvider,
      { client },
      React.createElement(Overview, {
        workspace: "company/a",
        href: (path) => `${path}?workspace=company%2Fa`,
      }),
    ),
  );
}
test("Overview exposes persisted company workload and source health without claiming financial outcomes", () => {
  const html = render(workload);
  assert.match(html, /<dt>Open tasks<\/dt><dd>3<\/dd>/);
  assert.match(html, /<dt>Unresolved cases<\/dt><dd>4<\/dd>/);
  assert.match(html, /<dt>Pending approvals<\/dt><dd>2<\/dd>/);
  assert.match(html, /1 completed with a real model/);
  assert.match(html, /AUTH_EXPIRED/);
  assert.match(html, /Observation time unavailable/);
  assert.match(html, /Outbound is paused/);
  assert.match(html, /href="\/settings\/email\?workspace=company%2Fa"/);
  assert.doesNotMatch(
    html,
    /Insufficient data for cohort comparisons and savings/,
  );
});
test("empty workflow counts remain distinct from source availability and escaped provider labels", () => {
  const html = render({
    ...workload,
    open_tasks: 0,
    pending_approvals: 0,
    unresolved_cases: 0,
    accounting_sources: [
      { ...workload.accounting_sources[0], provider: "<script>bad()</script>" },
    ],
  });
  assert.match(html, /<dt>Open tasks<\/dt><dd>0<\/dd>/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;bad\(\)&lt;\/script&gt;/);
  assert.match(html, /No imported accounting data/);
});
