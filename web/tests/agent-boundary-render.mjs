// Actual persisted SSE payloads/steps from the Python API consumer, no fixture expectations.
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { libDouble } from "./lib-support.mjs";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript"),
  React = req("react");
const { renderToStaticMarkup } = req("react-dom/server");
function load(file) {
  const compiled = ts.transpileModule(
    readFileSync(
      new URL(`../src/features/agent/${file}`, import.meta.url),
      "utf8",
    ),
    {
      compilerOptions: {
        jsx: ts.JsxEmit.ReactJSX,
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText;
  const mod = { exports: {} };
  const local = (name) =>
    name === "next/link"
      ? ({ href, children }) => React.createElement("a", { href }, children)
      : name === "@/features/receivables/shared"
        ? { stamp: (value) => value }
        : req(name);
  new Function("require", "module", "exports", compiled)(
    (n) => libDouble(n) ?? local(n),
    mod,
    mod.exports,
  );
  return mod.exports;
}
const data = JSON.parse(readFileSync(0, "utf8"));
class Source {
  static current;
  listeners = new Map();
  constructor() {
    Source.current = this;
  }
  addEventListener(name, callback) {
    this.listeners.set(name, callback);
  }
  close() {}
}
globalThis.EventSource = Source;
const sequences = [];
const close = load("stream.ts").observeAgentEvents(
  "/actual-events",
  data.run.id,
  data.cursor,
  (event) => sequences.push(event.event_sequence),
  () => {},
);
for (const event of [...data.events, ...data.events])
  Source.current.listeners.get(event.kind)?.({
    data: JSON.stringify(event),
    lastEventId: event.id,
  });
close();
const html = renderToStaticMarkup(
  React.createElement(load("activity.tsx").AgentActivity, {
    run: data.run,
    steps: data.steps,
    workspace: data.run.tenant_id,
  }),
);
const step = data.steps[0];
const status = html.match(
  new RegExp(
    `<li id="agent-step-${step.id}"[^]*?<span class="badge agent-status-[^"]+">([^<]+)</span>`,
  ),
)?.[1];
if (!status) throw new Error("Actual persisted step was not rendered");
process.stdout.write(
  JSON.stringify({
    replayed_sequences: sequences,
    rendered_final_status: status.toLowerCase(),
  }),
);
