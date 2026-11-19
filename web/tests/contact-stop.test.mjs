import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const React = req("react");
const ts = req("typescript");
function load(file) {
  const source = readFileSync(
    new URL(`../src/features/receivables/${file}`, import.meta.url),
    "utf8",
  );
  const module = { exports: {} };
  new Function(
    "require",
    "module",
    "exports",
    ts.transpileModule(source, {
      compilerOptions: {
        jsx: ts.JsxEmit.ReactJSX,
        module: ts.ModuleKind.CommonJS,
        esModuleInterop: true,
      },
    }).outputText,
  )(
    (name) =>
      name === "@/features/conversations/voice-business-source"
        ? load("../conversations/voice-business-source.tsx")
        : name === "@/lib/api"
          ? { readApi: () => {} }
          : name === "./contact-stop-api"
            ? load("contact-stop-api.ts")
            : name === "next/link"
              ? ({ href, children, ...props }) =>
                  React.createElement("a", { href, ...props }, children)
              : req(name),
    module,
    module.exports,
  );
  return module.exports;
}
const view = {
  action_id: "action-1",
  action_version: 3,
  customer_id: "customer-1",
  accounting_connection_id: "account-1",
  source_communication_id: "source-1",
  source_excerpt: "Please stop contacting me.",
  pending: true,
  evidence_complete: true,
  can_release: true,
  original_holds: [
    {
      id: "hold-1",
      invoice_id: "invoice-1",
      version: 2,
      released_at: null,
      release_evidence: null,
    },
    {
      id: "hold-2",
      invoice_id: "invoice-2",
      version: 4,
      released_at: "2026-11-19T00:00:00Z",
      release_evidence: "Earlier explicit review",
    },
  ],
};
test("capture freezes exact source, action and every original hold version independently from refreshed data", () => {
  const { captureContactStop, restoreContactStopCapture } = load(
    "contact-stop-api.ts",
  );
  const current = structuredClone(view);
  const captured = captureContactStop(
    current,
    " Independently reviewed customer request ",
    "request-1",
  );
  current.original_holds[0].version = 99;
  current.source_excerpt = "Refreshed source";
  assert.equal(captured.review.source_excerpt, view.source_excerpt);
  assert.deepEqual(captured.command, {
    request_id: "request-1",
    expected_action_version: 3,
    reviewed_holds: [
      { hold_id: "hold-1", expected_version: 2 },
      { hold_id: "hold-2", expected_version: 4 },
    ],
    reason: "Independently reviewed customer request",
  });
  assert.deepEqual(
    restoreContactStopCapture(JSON.stringify(captured)),
    captured,
  );
  assert.equal(restoreContactStopCapture('{"review":{}}'), null);
  assert.throws(
    () =>
      captureContactStop(
        { ...view, can_release: false },
        "Reviewed source request",
      ),
    /permission|review/i,
  );
  assert.throws(() => captureContactStop(view, "          "), /reason/i);
});
test("actual release sends unchanged reviewed snapshot and CSRF for lost confirmation retry; conflict preserves capture", async () => {
  const { captureContactStop, contactStopApi } = load("contact-stop-api.ts");
  const captured = captureContactStop(
    view,
    "Independently reviewed source request",
    "request-2",
  );
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return new Response(
      JSON.stringify(calls.length < 2 ? {} : { ...view, pending: false }),
      { status: calls.length === 1 ? 503 : 200 },
    );
  };
  try {
    await assert.rejects(
      contactStopApi.release("workspace-1", captured, "csrf-1"),
      /confirmed|retry/i,
    );
    assert.equal(
      (await contactStopApi.release("workspace-1", captured, "csrf-1")).pending,
      false,
    );
    assert.equal(calls[0].options.body, calls[1].options.body);
    assert.equal(calls[0].options.headers["x-csrf-token"], "csrf-1");
    assert.equal(
      calls[0].url,
      "/api/workspaces/workspace-1/contact-stops/action-1/release",
    );
    globalThis.fetch = async () => new Response("{}", { status: 409 });
    await assert.rejects(
      contactStopApi.release("workspace-1", captured, "csrf-1"),
      (error) => error.refreshRequired === true,
    );
    assert.equal(captured.command.request_id, "request-2");
  } finally {
    globalThis.fetch = original;
  }
});
test("human review renders actual source and invoice links, original remaining holds and no recipient/resume claim", () => {
  const { ContactStopSummary } = load("contact-stop.tsx");
  const html = req("react-dom/server").renderToStaticMarkup(
    React.createElement(ContactStopSummary, {
      review: view,
      workspace: "workspace-1",
    }),
  );
  assert.match(html, /1 of 2 original holds remain pending/);
  assert.match(html, /source-1\?workspace=workspace-1/);
  assert.match(html, /receivables\/invoice-2\?workspace=workspace-1/);
  assert.match(html, /Sender authority remains unverified/);
  assert.match(html, /does not verify a recipient or resume automation/);
  assert.doesNotMatch(
    html,
    /tasks\/hold-|held invoice is paid|recipient is verified/i,
  );
});

test("a captured operational source survives reload without a fabricated communication reference", () => {
  const { captureContactStop, restoreContactStopCapture } = load(
    "contact-stop-api.ts",
  );
  const current = {
    ...view,
    source_communication_id: null,
    voice_source: { id: "voice-source", facts_hash: "b".repeat(64) },
  };
  const captured = captureContactStop(
    current,
    "Reviewed the exact signed normalized restriction",
    "request-voice",
  );
  current.voice_source.facts_hash = "c".repeat(64);
  assert.equal(captured.review.voice_source.facts_hash, "b".repeat(64));
  assert.deepEqual(
    restoreContactStopCapture(JSON.stringify(captured)),
    captured,
  );
});

test("transport loss preserves the exact reviewed release and gives explicit same-command retry guidance", async () => {
  const { captureContactStop, contactStopApi, ContactStopError } = load("contact-stop-api.ts");
  const captured = captureContactStop(view, "Independently reviewed original request", "request-lost");
  const snapshot = structuredClone(captured);
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    if (calls.length === 1) throw new TypeError("Failed to fetch");
    return new Response(JSON.stringify({ ...view, pending: false }), { status: 200 });
  };
  try {
    await assert.rejects(contactStopApi.release("workspace-1", captured, "csrf-1"), (error) =>
      error instanceof ContactStopError && error.refreshRequired === false &&
      /could not be confirmed.*same captured review/i.test(error.message));
    assert.equal(calls.length, 1, "No automatic retry after an uncertain mutation");
    assert.deepEqual(captured, snapshot);
    assert.equal((await contactStopApi.release("workspace-1", captured, "csrf-1")).pending, false);
    assert.equal(calls.length, 2);
    assert.equal(calls[0].options.body, calls[1].options.body);
    assert.equal(calls[1].options.body, JSON.stringify(snapshot.command));
    assert.deepEqual(captured, snapshot);
  } finally {
    globalThis.fetch = original;
  }
});
