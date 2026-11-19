import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { libDouble } from "./lib-support.mjs";
const require = createRequire(new URL("../package.json", import.meta.url));
const ts = require("typescript"), React = require("react");
const { renderToStaticMarkup: render } = require("react-dom/server");
let state;
const module = { exports: {} };
const source = readFileSync(new URL("../src/features/receivables/activity.tsx", import.meta.url), "utf8");
new Function("require", "module", "exports", ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
}).outputText)((name) => {
  if (libDouble(name)) return libDouble(name);
  if (name === "next/link") return ({ children, href }) => React.createElement("a", { href }, children);
  if (name === "@tanstack/react-query") return { useQuery: () => state };
  if (name === "./shared") return { stamp: value => value, ErrorState: ({label}) => React.createElement("p", {role:"alert"}, label) };
  if (name === "@/lib/api") return { readApi: () => {} };
  if (name === "@/features/review/api") return { reviewApi: {} };
  return require(name);
}, module, module.exports);
const { InvoiceTasks, InvoiceAudit } = module.exports;
const props = { workspace: "tenant-a", invoiceId: "invoice-a" };
const ready = data => ({ data, isPending: false, isError: false, isFetching: false, refetch(){} });
test("invoice tasks show persisted status and genuine detail links without claiming resolution", () => {
  state = ready({items:[{id:"task-a",title:"Check source <unsafe>",status:"done",origin:"generated",due_at:null,deadline_locked:false}]});
  const html = render(React.createElement(InvoiceTasks, props));
  assert.match(html, /\/tasks\/task-a\?workspace=tenant-a/);
  assert.match(html, /Check source &lt;unsafe&gt;/);
  assert.match(html, /done/);
  assert.match(html, /Task completion does not resolve a case or change accounting/);
});
test("audit preserves safe event provenance and never links deleted targets", () => {
  state = ready({invoice_id:"invoice-a",next_before_id:"event-a",items:[
    {id:"event-a",action:"task.deleted",actor_kind:"human",actor_id:"actor-a",target_kind:"task",target_id:"deleted-task",target_available:false,created_at:"2026-11-19",correlation_id:"correlation-a"},
    {id:"event-b",action:"case.created",actor_kind:"service",actor_id:"worker-a",target_kind:"case",target_id:"case-a",target_available:true,created_at:"2026-11-19",correlation_id:"correlation-b"},
  ]});
  const html = render(React.createElement(InvoiceAudit, props));
  assert.match(html, /event-a/); assert.match(html, /actor-a/); assert.match(html, /correlation-a/);
  assert.match(html, /deleted-task/); assert.doesNotMatch(html, /href="[^"]*deleted-task/);
  assert.match(html, /\/cases\/case-a\?workspace=tenant-a/);
  assert.match(html, /Older activity/); assert.match(html, /finite invoice-linked business audit/);
  assert.match(html, /not the complete workspace audit log/);
});
test("audit timestamps keep seconds so close events stay distinguishable", () => {
  const { auditStamp } = module.exports;
  assert.equal(typeof auditStamp, "function");
  assert.match(auditStamp("2026-11-19T11:50:04Z"), /11:50:04/);
  assert.match(auditStamp("2026-11-19T11:50:06Z"), /UTC$/);
  assert.equal(auditStamp(null), "Unavailable");
});
test("empty retained history is distinct from unavailable and loading", () => {
  state = ready({items:[],next_before_id:null});
  assert.match(render(React.createElement(InvoiceAudit, props)), /No retained events in this audit scope/);
  assert.match(render(React.createElement(InvoiceTasks, props)), /No current tasks are linked/);
  state = {isPending:true};
  assert.match(render(React.createElement(InvoiceAudit, props)), /Loading invoice audit/);
  state = {isError:true,error:new Error("offline"),refetch(){}};
  const html = render(React.createElement(InvoiceAudit, props));
  assert.match(html, /Invoice audit unavailable/); assert.doesNotMatch(html, /No retained events/);
});
