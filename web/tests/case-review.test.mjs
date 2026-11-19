import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const React = req("react"),
  ts = req("typescript");
const { renderToStaticMarkup } = req("react-dom/server");
function load() {
  const path = new URL(
    "../src/features/agent/case-review.tsx",
    import.meta.url,
  );
  if (!existsSync(path)) return {};
  const source = ts.transpileModule(readFileSync(path, "utf8"), {
    compilerOptions: {
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.CommonJS,
    },
  }).outputText;
  const mod = { exports: {} };
  const local = (name) =>
    name === "next/navigation"
      ? { useRouter: () => ({ push() {} }) }
      : name === "./api"
        ? {}
        : req(name);
  new Function("require", "module", "exports", source)(local, mod, mod.exports);
  return mod.exports;
}
const record = {
  id: "case-id",
  version: 7,
  invoice_links: [{ invoice_id: "i1" }, { invoice_id: "i2" }],
};
test("case review command preserves all linked invoices and exact displayed version", () => {
  const { caseReviewCommand } = load();
  assert.equal(typeof caseReviewCommand, "function");
  const command = caseReviewCommand(record, "request-id");
  assert.deepEqual(command.invoice_ids, ["i1", "i2"]);
  assert.equal(command.request_id, "request-id");
  assert.match(command.objective, /case case-id version 7/);
  assert.match(command.objective, /approval/);
  assert.throws(() =>
    caseReviewCommand({ ...record, invoice_links: [] }, "request-id"),
  );
  assert.throws(() =>
    caseReviewCommand(
      {
        ...record,
        invoice_links: [1, 2, 3, 4].map((i) => ({ invoice_id: String(i) })),
      },
      "request-id",
    ),
  );
});
test("case review entry is current-permission gated and does not claim resolution", () => {
  const { CaseTaskReview } = load();
  assert.equal(typeof CaseTaskReview, "function");
  const html = renderToStaticMarkup(
    React.createElement(CaseTaskReview, {
      record,
      workspace: { id: "w", permissions: ["investigate"] },
      csrfToken: "csrf",
    }),
  );
  assert.match(html, /Request independent task review/);
  assert.match(html, /case version 7/i);
  const viewer = renderToStaticMarkup(
    React.createElement(CaseTaskReview, {
      record,
      workspace: { id: "w", permissions: [] },
      csrfToken: "csrf",
    }),
  );
  assert.doesNotMatch(viewer, /Request independent task review/);
});
