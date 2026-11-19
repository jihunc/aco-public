import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript"),
  React = req("react");
const { renderToStaticMarkup } = req("react-dom/server");
const workspace = {
  id: "workspace-1",
  name: "Synthetic workspace",
  timezone: "UTC",
  role: "owner",
  permissions: ["settings"],
  automation_enabled: false,
  outbound_paused: true,
};
function load(file) {
  const requested = file instanceof URL ? file : new URL(`../src/features/${file}`, import.meta.url);
  const source = [requested, new URL(`${requested.href}.tsx`), new URL(`${requested.href}.ts`)]
    .find(path => existsSync(path));
  assert.ok(source, `Actual component source missing: ${requested.pathname}`);
  const output = ts.transpileModule(
    readFileSync(source, "utf8"),
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
    name === "@tanstack/react-query"
      ? {
          useQuery: ({ queryKey }) =>
            queryKey[0] === "me"
              ? {
                  data: {
                    email: "synthetic@example.test",
                    csrf_token: "csrf",
                    workspaces: [workspace],
                  },
                }
              : queryKey[0] === "receivables"
                ? {
                    data: {
                      total: 0,
                      page: 1,
                      page_size: 20,
                      items: [],
                      as_of_at: "2026-11-18T00:00:00Z",
                    },
                  }
                : ["members", "onboarding-accounting", "demo-reset"].includes(queryKey[0])
                  ? { isPending: true }
                  : { data: { total: 0, items: [] } },
          useQueryClient: () => ({}),
        }
      : name === "next/link"
        ? ({ href, children }) => React.createElement("a", { href }, children)
        : name === "@/components/ui/button"
          ? {
              Button: ({ children, ...props }) =>
                React.createElement("button", props, children),
            }
          : name === "@/lib/api"
            ? {}
            : name === "@/lib/money"
              ? {}
              : name === "./shared"
                ? {
                    stamp: (v) => v,
                    ErrorState: () => null,
                    SourceBadges: () => null,
                  }
                : name.startsWith(".")
                  ? load(new URL(name, source))
                  : name.startsWith("@/features/")
                    ? load(name.slice("@/features/".length))
                    : req(name);
  new Function("require", "module", "exports", output)(local, mod, mod.exports);
  return mod.exports;
}
test("invitation role has an exact associated text label without option text", () => {
  const { Setup } = load("onboarding/setup.tsx");
  const html = renderToStaticMarkup(React.createElement(Setup));
  const label = html.match(/<label[^>]*for="([^"]+)"[^>]*>Role<\/label>/);
  assert.ok(label, "Role must have an exact standalone associated label");
  const select = html.match(new RegExp(`<select[^>]*id="${label[1]}"[^>]*>`));
  assert.ok(select, "Role label must identify a real select");
  assert.match(select[0], /name="role"/);
  assert.match(
    html,
    /<option[^>]*value="finance_manager"|<option[^>]*>finance_manager/,
  );
});
test("receivables result count stays independently readable beside investigation navigation", () => {
  const { Receivables } = load("receivables/list.tsx");
  const html = renderToStaticMarkup(
    React.createElement(Receivables, {
      workspace: "workspace-1",
      href: (path) => `${path}?workspace=workspace-1`,
    }),
  );
  assert.match(html, /<(?:p|span)[^>]*>0 invoices<\/(?:p|span)>/);
  assert.match(
    html,
    /<a href="\/agents\?workspace=workspace-1">Investigate invoices<\/a>/,
  );
});
