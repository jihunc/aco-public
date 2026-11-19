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
      new URL(`../src/features/communications/${file}`, import.meta.url),
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
        ? { useRouter: () => ({ replace: () => {} }) }
        : name === "@/features/receivables/shared"
          ? { stamp: (v) => v ?? "Unavailable", ErrorState: () => null }
          : name === "@/lib/api"
            ? {}
            : name === "./ingress-api"
              ? load("ingress-api.ts")
              : name === "@/lib/money"
                ? {}
                : name === "@/features/agent/api"
                  ? {}
                  : req(name);
  new Function("require", "module", "exports", output)((n) => libDouble(n) ?? local(n), mod, mod.exports);
  return mod.exports;
}
const activation = {
  id: "activation-1",
  version: 4,
  email_connection_id: "email-1",
  storage_connection_id: "storage-1",
  expires_at: "2026-11-18T12:00:00Z",
  revoked_at: null,
  synthetic: true,
};
const receipt = {
  synthetic: true,
  id: "receipt-1",
  state: "queued",
  version: 1,
  communication_id: null,
  agent_run_id: null,
  job_id: null,
  error_code: null,
  review_required: true,
  sender_authority: "UNVERIFIED",
  sensitive_action_allowed: false,
};
test("simulation retains exact reviewed activation and idempotency on same-payload retry", () => {
  const { prepareSimulation } = load("ingress-api.ts");
  let ids = 0;
  const first = prepareSimulation(
    null,
    activation,
    ["invoice-1"],
    "invoice_reply",
    () => `request-${++ids}`,
  );
  activation.version = 5;
  const retained = prepareSimulation(
    first,
    { ...activation, version: 4 },
    ["invoice-1"],
    "invoice_reply",
    () => `request-${++ids}`,
  );
  assert.equal(retained.command.request_id, "request-1");
  assert.equal(retained.command.expected_activation_version, 4);
  assert.equal(ids, 1);
  const changed = prepareSimulation(
    first,
    activation,
    ["invoice-1"],
    "invoice_reply",
    () => `request-${++ids}`,
  );
  assert.equal(changed.command.expected_activation_version, 5);
  assert.equal(changed.command.request_id, "request-2");
  activation.version = 4;
});
test("simulation sends fixed scenario and captured version with same-origin CSRF and no raw content", async () => {
  const { ingressApi } = load("ingress-api.ts");
  const prior = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    assert.equal(
      path,
      "/api/workspaces/workspace-1/email-ingress/activations/activation-1/simulate",
    );
    assert.equal(init.credentials, "same-origin");
    assert.equal(init.headers["x-csrf-token"], "csrf");
    assert.deepEqual(JSON.parse(init.body), {
      request_id: "request-1",
      expected_activation_version: 4,
      invoice_ids: ["invoice-1"],
      scenario: "invoice_reply",
    });
    return { ok: true, json: async () => receipt };
  };
  try {
    assert.equal(
      (
        await ingressApi.simulate(
          "workspace-1",
          "activation-1",
          {
            request_id: "request-1",
            expected_activation_version: 4,
            invoice_ids: ["invoice-1"],
            scenario: "invoice_reply",
          },
          "csrf",
        )
      ).id,
      "receipt-1",
    );
  } finally {
    globalThis.fetch = prior;
  }
});
test("simulation conflict does not retry or replace captured activation preconditions", async () => {
  const { ingressApi } = load("ingress-api.ts");
  const prior = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return {
      ok: false,
      status: 409,
      json: async () => ({ detail: "INGRESS_ACTIVATION_CONFLICT" }),
    };
  };
  try {
    await assert.rejects(
      ingressApi.simulate(
        "workspace-1",
        "activation-1",
        {
          request_id: "request-1",
          expected_activation_version: 4,
          invoice_ids: ["invoice-1"],
          scenario: "invoice_reply",
        },
        "csrf",
      ),
      /changed|review/i,
    );
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = prior;
  }
});
test("queued receipt never fabricates archived content or an investigation", () => {
  const { IngressReceiptRecord } = load("ingress.tsx");
  const html = renderToStaticMarkup(
    React.createElement(IngressReceiptRecord, {
      receipt,
      workspace: "workspace-1",
    }),
  );
  assert.match(html, /Queued/);
  assert.match(html, /Sender identity remains unverified/);
  assert.match(html, /No sensitive action authority/);
  assert.doesNotMatch(
    html,
    /href=.*communications|href=.*agent-runs|archived successfully|task created/i,
  );
});
test("persisted receipt links only server-provided conversation and investigation IDs", () => {
  const { IngressReceiptRecord } = load("ingress.tsx");
  const html = renderToStaticMarkup(
    React.createElement(IngressReceiptRecord, {
      receipt: {
        ...receipt,
        state: "processed",
        communication_id: "message-1",
        agent_run_id: "run-1",
      },
      workspace: "workspace-1",
    }),
  );
  assert.match(html, /communications\/message-1\?workspace=workspace-1/);
  assert.match(html, /agent-runs\/run-1\?workspace=workspace-1/);
  assert.doesNotMatch(
    html,
    /Sender verified|Payment verified|email sent|case created/i,
  );
});
test("simulated investigation outcome links actual committed task records and keeps proposals unexecuted", () => {
  const { InboundOutcomeRecord } = load("ingress.tsx");
  const run = {
    id: "run-1",
    status: "completed",
    authenticity: "FAKE",
    model_id: null,
    execution: "EXECUTED",
    error_code: null,
    result: {
      summary: "Synthetic source-backed review",
      proposals: [
        {
          kind: "create_task",
          executed: true,
          title: "Inspect source",
          changed_ids: ["task-1"],
        },
        {
          kind: "follow_up",
          executed: false,
          reason: "Needs human review",
          changed_ids: [],
        },
      ],
    },
  };
  const html = renderToStaticMarkup(
    React.createElement(InboundOutcomeRecord, {
      run,
      workspace: "workspace-1",
    }),
  );
  assert.match(html, /Simulated model/);
  assert.match(html, /tasks\/task-1\?workspace=workspace-1/);
  assert.match(html, /Proposed|proposed/);
  assert.doesNotMatch(html, /Message sent|Payment collected|<button/);
});

test("current settings permission and server readiness determine the inbound composer", () => {
  const { QueryClient, QueryClientProvider } = req("@tanstack/react-query");
  const { SyntheticInboundPanel } = load("ingress.tsx");
  const workspace = { id: "workspace-1", is_demo: true, permissions: [] };
  function render(current, setup) {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    if (setup) client.setQueryData(["ingress-setup", current.id], setup);
    return renderToStaticMarkup(
      React.createElement(
        QueryClientProvider,
        { client },
        React.createElement(SyntheticInboundPanel, {
          workspace: current,
          csrfToken: "csrf",
        }),
      ),
    );
  }
  const viewer = render(workspace);
  assert.match(viewer, /An Owner must review/);
  assert.doesNotMatch(
    viewer,
    /Capture synthetic sample|Enable synthetic inbound/,
  );
  const unavailable = render(
    { ...workspace, permissions: ["settings"] },
    {
      enabled: false,
      email_connections: [],
      storage_connections: [],
      activations: [],
    },
  );
  assert.match(unavailable, /Synthetic inbound is unavailable/);
  assert.doesNotMatch(
    unavailable,
    /Capture synthetic sample|Enable synthetic inbound/,
  );
  const fresh = render(
    { ...workspace, permissions: ["settings"] },
    {
      can_prepare: true,
      enabled: false,
      email_connections: [],
      storage_connections: [],
      activations: [],
    },
  );
  assert.match(fresh, />Prepare synthetic inbox<\/button>/);
  assert.doesNotMatch(
    fresh,
    /Capture synthetic sample|Enable synthetic inbound/,
  );
  const actual = render(
    { ...workspace, permissions: ["settings"] },
    {
      enabled: true,
      email_connections: [
        { id: "email-actual", provider: "demo_email", status: "READY" },
      ],
      storage_connections: [
        { id: "storage-actual", provider: "s3", status: "READY" },
      ],
      activations: [],
    },
  );
  assert.match(actual, /email-actual/);
  assert.match(actual, /storage-actual/);
  assert.match(actual, /<button[^>]*disabled=""[^>]*>Capture synthetic sample/);
  assert.doesNotMatch(actual, /secret|password|raw MIME|textarea/i);
});
test("simulation errors expose safe guard codes without rendering arbitrary provider details", async () => {
  const { ingressApi, IngressCommandError } = load("ingress-api.ts");
  const prior = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    json: async () => ({ detail: "<script>provider private detail</script>" }),
  });
  try {
    await assert.rejects(
      ingressApi.simulate("w", "a", {}, "csrf"),
      (reason) =>
        reason instanceof IngressCommandError &&
        reason.status === 403 &&
        !reason.message.includes("private detail"),
    );
  } finally {
    globalThis.fetch = prior;
  }
});
test("fresh-workspace setup calls the real preparation route with an idempotent explicit request", async () => {
  const { ingressApi } = load("ingress-api.ts");
  const prior = globalThis.fetch;
  const command = {
    request_id: "prepare-request-1",
    evidence: "Owner requested synthetic inbox preparation.",
  };
  globalThis.fetch = async (path, init) => {
    assert.equal(path, "/api/workspaces/workspace-1/email-ingress/setup");
    assert.equal(init.method, "POST");
    assert.equal(init.headers["x-csrf-token"], "csrf");
    assert.deepEqual(JSON.parse(init.body), command);
    return {
      ok: true,
      json: async () => ({
        can_prepare: true,
        enabled: true,
        email_connections: [],
        storage_connections: [],
        activations: [],
      }),
    };
  };
  try {
    assert.equal(
      (await ingressApi.prepare("workspace-1", command, "csrf")).enabled,
      true,
    );
  } finally {
    globalThis.fetch = prior;
  }
});
test("an opened unknown-sender thread preserves actual archived source without inventing invoice or customer links", () => {
  const output = ts.transpileModule(
    readFileSync(
      new URL("../src/features/communications/timeline.tsx", import.meta.url),
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
    name === "react"
      ? { ...React, useState: () => [true, () => {}] }
      : name === "@tanstack/react-query"
        ? {
            useQuery: () => ({
              data: {
                origin_invoice_id: null,
                customer_id: null,
                messages: [{ id: "message-unknown" }],
              },
              isPending: false,
              isError: false,
            }),
          }
        : name === "next/link"
          ? ({ href, children }) => React.createElement("a", { href }, children)
          : name === "@/features/receivables/shared"
            ? { stamp: (value) => value }
            : name === "./api"
              ? {}
              : req(name);
  new Function("require", "module", "exports", output)((n) => libDouble(n) ?? local(n), mod, mod.exports);
  const row = {
    id: "message-unknown",
    thread_id: "thread-unknown",
    origin_invoice_id: null,
    direction: "incoming",
    synthetic: true,
    event_at: "2026-11-18T00:00:00Z",
    kind: "MESSAGE",
    delivery_state: "received",
    source_state: "stored",
    invoice_links: [],
    sources: [
      {
        artifact_id: "actual-original",
        representation: "provider_original",
        storage_state: "stored",
        available: true,
      },
    ],
    delivery_events: [],
    content: {
      subject: "SYNTHETIC unknown sender",
      sender: "unknown@synthetic.invalid",
      to: ["capture@synthetic.invalid"],
      message_id: "message-native",
      in_reply_to: null,
      references: [],
      cc: [],
      text_body: "<script>untrusted source</script>",
      html_body: null,
      attachments: [],
    },
  };
  const html = renderToStaticMarkup(
    React.createElement(mod.exports.CommunicationRecord, {
      row,
      workspace: "workspace-1",
    }),
  );
  assert.match(html, /Origin invoice: unlinked/);
  assert.match(html, /Customer link unknown/);
  assert.match(html, /artifacts\/actual-original\/content/);
  assert.doesNotMatch(html, /receivables\/null|customers\/null|<script>/);
  assert.match(html, /&lt;script&gt;untrusted source/);
});

test("a live activation cannot be reused for a synthetic sample", () => {
  assert.throws(
    () =>
      load("ingress-api.ts").prepareSimulation(
        null,
        { ...activation, synthetic: false },
        ["invoice"],
        "invoice_reply",
      ),
    /synthetic activation/i,
  );
});
test("a real-class provider receipt never renders as a synthetic sample", () => {
  const { IngressReceiptRecord } = load("ingress.tsx");
  const html = renderToStaticMarkup(
    React.createElement(IngressReceiptRecord, {
      receipt: { ...receipt, synthetic: false },
      workspace: "tenant",
    }),
  );
  assert.match(html, /Provider notification/);
  assert.doesNotMatch(html, /Synthetic sample|Synthetic inbound receipt/);
});

test("fixed three-intent sample retains its actual selection and unchanged retry identity", () => {
  const { prepareSimulation } = load("ingress-api.ts");
  const first = prepareSimulation(
    null,
    activation,
    ["invoice-101", "invoice-107", "invoice-113"],
    "three_intent_reply",
    () => "s2-request",
  );
  const retry = prepareSimulation(
    first,
    activation,
    ["invoice-101", "invoice-107", "invoice-113"],
    "three_intent_reply",
    () => "must-not-replace",
  );
  assert.equal(retry.command.request_id, "s2-request");
  assert.equal(retry.command.scenario, "three_intent_reply");
  assert.deepEqual(retry.command.invoice_ids, [
    "invoice-101",
    "invoice-107",
    "invoice-113",
  ]);
  const changed = prepareSimulation(
    retry,
    activation,
    ["invoice-101"],
    "invoice_reply",
    () => "changed-source",
  );
  assert.equal(changed.command.request_id, "changed-source");
});
