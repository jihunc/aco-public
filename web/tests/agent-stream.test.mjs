import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
const requireFromWeb = createRequire(
  new URL("../package.json", import.meta.url),
);
const ts = requireFromWeb("typescript");
const compiled = ts.transpileModule(
  readFileSync(
    new URL("../src/features/agent/stream.ts", import.meta.url),
    "utf8",
  ),
  {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  },
).outputText;
const module = { exports: {} };
new Function("module", "exports", compiled)(module, module.exports);
const { observeAgentEvents } = module.exports;
class FakeEventSource {
  static instance;
  listeners = new Map();
  closed = false;
  constructor(url, options) {
    this.url = url;
    this.options = options;
    FakeEventSource.instance = this;
  }
  addEventListener(kind, fn) {
    this.listeners.set(kind, fn);
  }
  close() {
    this.closed = true;
  }
  emit(sequence, kind = "step.completed", patch = {}) {
    this.listeners.get(kind)?.({
      data: JSON.stringify({
        id: `run-1:${sequence}`,
        run_id: "run-1",
        event_sequence: sequence,
        kind,
        step_id: "same-step",
        ...patch,
      }),
      lastEventId: `run-1:${sequence}`,
    });
  }
}
globalThis.EventSource = FakeEventSource;
test("SSE observer deduplicates replay from the saved watermark and preserves separate transitions on one step", () => {
  const seen = [];
  const close = observeAgentEvents(
    "/api/workspaces/workspace-1/agent-runs/run-1/events",
    "run-1",
    4,
    (event) => seen.push(event.event_sequence),
    () => {},
  );
  const source = FakeEventSource.instance;
  assert.equal(source.options.withCredentials, true);
  source.emit(3);
  source.emit(4);
  source.emit(5, "step.started");
  source.emit(5, "step.started");
  source.emit(6);
  source.emit(7, "step.completed", { run_id: "foreign-run" });
  source.emit(7, "step.completed", { id: "wrong:7" });
  assert.deepEqual(seen, [5, 6]);
  close();
  assert.equal(source.closed, true);
  source.emit(8);
  assert.deepEqual(seen, [5, 6]);
});
test("native reconnect state falls back to persisted polling without any cancellation or start request", () => {
  const states = [];
  const close = observeAgentEvents(
    "/events",
    "run-1",
    0,
    () => {},
    (state) => states.push(state),
  );
  const source = FakeEventSource.instance;
  source.onopen();
  source.onerror();
  source.onopen();
  assert.deepEqual(states, ["connected", "polling", "connected"]);
  assert.equal(source.closed, false);
  close();
});
