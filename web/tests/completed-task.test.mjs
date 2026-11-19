import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { libDouble } from "./lib-support.mjs";

const requireFromWeb = createRequire(new URL("../package.json", import.meta.url));
const ts = requireFromWeb("typescript");
const React = requireFromWeb("react");
const { renderToStaticMarkup } = requireFromWeb("react-dom/server");
const source = readFileSync(new URL("../src/features/review/completed-task.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: {
  jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, esModuleInterop: true,
}}).outputText;
const module = { exports: {} };
const localRequire = (name) => name === "next/link"
  ? ({ href, children }) => React.createElement("a", { href }, children)
  : name === "@/features/receivables/shared" ? { stamp: (value) => value ?? "No due date" }
    : requireFromWeb(name);
new Function("require", "module", "exports", compiled)((n) => libDouble(n) ?? localRequire(n), module, module.exports);
const { CompletedTask } = module.exports;

const task = {
  id: "task-1", invoice_id: "invoice-1", source_action_id: null,
  creator_id: "user-1", assignee_id: null, title: "Close review", notes: "Archive receipt",
  status: "done", priority: 2, priority_rank: "1", due_at: null, deleted_at: null,
  version: 3, origin: "human", deadline_locked: false,
};

test("completed task exposes detail and edit/delete controls from persisted task", () => {
  const html = renderToStaticMarkup(React.createElement(CompletedTask, {
    task, workspace: "workspace-1", canEdit: true, busy: false,
    onEdit: () => {}, onDelete: () => {},
  }));
  assert.match(html, /Close review/);
  assert.match(html, /Archive receipt/);
  assert.match(html, /· High</);
  assert.match(html, /Due: No due date/);
  assert.match(html, /receivables\/invoice-1\?workspace=workspace-1/);
  assert.match(html, /Edit Close review/);
  assert.match(html, /Delete Close review/);
});

test("completed task remains readable without write permission", () => {
  const html = renderToStaticMarkup(React.createElement(CompletedTask, {
    task, workspace: "workspace-1", canEdit: false, busy: false,
    onEdit: () => {}, onDelete: () => {},
  }));
  assert.match(html, /Close review/);
  assert.doesNotMatch(html, /Edit Close review|Delete Close review/);
});
