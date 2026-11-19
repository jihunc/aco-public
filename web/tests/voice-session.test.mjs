import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript"),
  React = req("react");
const { renderToStaticMarkup } = req("react-dom/server");
function load(file, queryResult) {
  const output = ts.transpileModule(
    readFileSync(
      new URL(`../src/features/conversations/${file}`, import.meta.url),
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
    name === "./voice-live-controls"
      ? { VoiceLiveControls: () => null }
      : name === "@/features/conversations/voice-business-source"
        ? load("voice-business-source.tsx")
        : name === "next/link"
          ? ({ href, children }) => React.createElement("a", { href }, children)
          : name === "next/navigation"
            ? {
                useRouter: () => ({
                  push: () => {
                    throw Error("Unexpected navigation on mount");
                  },
                }),
              }
            : name === "@tanstack/react-query" && queryResult
              ? { useQuery: () => queryResult }
              : name === "@/features/receivables/shared"
                ? { stamp: (v) => v, ErrorState: () => null }
                : name === "@/lib/api"
                  ? {}
                  : name === "./voice-privacy"
                    ? {
                        ConsentPanel: () => null,
                        VoicePrivacyDetail: () => null,
                      }
                    : name === "./voice-privacy-api"
                      ? { voicePrivacyApi: {} }
                      : name === "./voice-api"
                        ? load("voice-api.ts")
                        : req(name);
  new Function("require", "module", "exports", output)(local, mod, mod.exports);
  return mod.exports;
}
const session = {
  id: "session-1",
  version: 3,
  approval_id: "approval-1",
  connection_id: "connection-1",
  invoice_ids: ["invoice-1"],
  objective: "<script>Inspect account</script>",
  state: "completed",
  provider_call_id: "offline-call",
  evidence_mode: "MOCKED_HTTP",
  transcript_available: false,
  recording_available: false,
  live_status: "BLOCKED",
  pstn_status: "SKIPPED",
  expires_at: "2026-11-20T00:00:00Z",
  changed_ids: ["task-1"],
  can_start: false,
  reasons: [],
  source_scope: [
    {
      invoice_id: "invoice-1",
      source_revision: "source-v7",
      captured_at: "2026-11-19T00:00:00Z",
    },
  ],
  events: [
    {
      id: "event-1",
      kind: "session_ended",
      created_at: "2026-11-19T00:02:00Z",
    },
  ],
  invocations: [
    {
      id: "tool-1",
      native_id: "native-1",
      tool: "get_invoice",
      arguments: { invoice_id: "invoice-1" },
      result: {
        outstanding_minor: "10000000000000001",
        currency: "AUD",
        observed_at: "2026-11-18T00:00:00Z",
      },
      action_id: null,
      created_at: "2026-11-19T00:01:00Z",
    },
    {
      id: "tool-2",
      native_id: "native-2",
      tool: "create_collection_task",
      arguments: { title: "<img src=x onerror=alert(1)>" },
      result: null,
      action_id: null,
      created_at: "2026-11-19T00:01:01Z",
    },
  ],
};
test("start retains captured version and fails closed on refresh or current ineligibility", () => {
  const { captureVoice, sameVoiceSnapshot, voiceStartCommand } =
    load("voice-api.ts");
  const captured = captureVoice(session);
  assert.equal(sameVoiceSnapshot(captured, { ...session, version: 4 }), false);
  assert.equal(
    sameVoiceSnapshot(captured, {
      ...session,
      source_scope: [
        { ...session.source_scope[0], source_revision: "changed" },
      ],
    }),
    false,
  );
  assert.deepEqual(voiceStartCommand(captured), { expected_version: 3 });
  session.source_scope[0].source_revision = "later";
  assert.equal(captured.source_scope[0].source_revision, "source-v7");
  session.source_scope[0].source_revision = "source-v7";
});
test("voice detail renders actual scope, inert tool results and committed IDs without claiming live capture", () => {
  const { VoiceRecord } = load("voice-session.tsx");
  const html = renderToStaticMarkup(
    React.createElement(VoiceRecord, { session, workspace: "workspace-1" }),
  );
  for (const value of [
    "MOCKED_HTTP",
    "BLOCKED",
    "Recording and transcript capture disabled",
    "source-v7",
    "Scope captured",
    "10000000000000001",
    "No committed result",
    "tasks/task-1",
    "review?workspace=workspace-1&amp;approval=approval-1",
    "session_ended",
  ])
    assert.ok(html.includes(value), value);
  assert.match(html, /&lt;script&gt;Inspect account&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>|<img|<audio/);
});
test("start posts exact reviewed version and CSRF once; conflicts never retry", async () => {
  const { voiceApi } = load("voice-api.ts");
  const previous = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async (path, init) => {
    calls++;
    assert.equal(
      path,
      "/api/workspaces/workspace-1/voice-sessions/session-1/start",
    );
    assert.equal(init.headers["x-csrf-token"], "csrf");
    assert.deepEqual(JSON.parse(init.body), { expected_version: 3 });
    assert.equal(init.credentials, "same-origin");
    return new Response("{}", { status: 409 });
  };
  try {
    await assert.rejects(
      voiceApi.start(
        "workspace-1",
        "session-1",
        { expected_version: 3 },
        "csrf",
      ),
      /changed|review/i,
    );
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = previous;
  }
});

test("current server eligibility and successful current read are required after explicit review", () => {
  const { canStartVoice, captureVoice } = load("voice-api.ts");
  const current = { ...session, can_start: true };
  const reviewed = captureVoice(current);
  assert.equal(canStartVoice(reviewed, current, true), true);
  assert.equal(canStartVoice(null, current, true), false);
  assert.equal(
    canStartVoice(reviewed, { ...current, can_start: false }, true),
    false,
  );
  assert.equal(canStartVoice(reviewed, current, false), false);
  assert.equal(
    canStartVoice(reviewed, { ...current, version: 99 }, true),
    false,
  );
});

test("server-denied preparation preserves history and does not expose mutation controls", () => {
  const { VoicePanel } = load("voice-session.tsx", {
    data: {
      items: [session],
      connections: [],
      can_prepare: false,
      reasons: ["VOICE_DISABLED"],
    },
    isPending: false,
    isError: false,
    isFetching: false,
  });
  const html = renderToStaticMarkup(
    React.createElement(VoicePanel, {
      workspace: { id: "workspace-1" },
      csrfToken: "csrf",
    }),
  );
  assert.match(html, /VOICE DISABLED/);
  assert.match(html, /Preparation is unavailable/);
  assert.match(html, /voice\/session-1/);
  assert.doesNotMatch(
    html,
    /Save session for approval|Session objective|<select/,
  );
});
test("eligible saved session never starts on mount and still requires explicit review", () => {
  const { VoicePanel } = load("voice-session.tsx", {
    data: { ...session, can_start: true },
    isPending: false,
    isError: false,
    isFetching: false,
  });
  const html = renderToStaticMarkup(
    React.createElement(VoicePanel, {
      workspace: { id: "workspace-1" },
      csrfToken: "csrf",
      sessionId: session.id,
    }),
  );
  assert.match(
    html,
    /<button type="button" disabled="">Start approved offline session<\/button>/,
  );
  assert.match(html, /I reviewed this saved objective/);
  assert.doesNotMatch(html, /checked=""/);
});

test("offline setup is an explicit server-eligible action with no configuration or secrets in its command", async () => {
  const data = {
    items: [],
    connections: [],
    can_prepare: false,
    reasons: ["VOICE_CONNECTION_UNAVAILABLE"],
    can_setup: true,
    setup_reasons: [],
  };
  const { VoicePanel } = load("voice-session.tsx", {
    data,
    isPending: false,
    isError: false,
    isFetching: false,
  });
  const html = renderToStaticMarkup(
    React.createElement(VoicePanel, {
      workspace: { id: "workspace-1" },
      csrfToken: "csrf",
    }),
  );
  assert.match(html, /Prepare offline voice connection/);
  assert.doesNotMatch(html, /API key|Webhook secret|<input[^>]+password/);
  const { voiceApi } = load("voice-api.ts");
  const previous = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async (path, init) => {
    calls++;
    assert.equal(path, "/api/workspaces/workspace-1/voice-sessions/setup");
    assert.equal(init.headers["x-csrf-token"], "csrf");
    assert.deepEqual(JSON.parse(init.body), { request_id: "request-1" });
    return Response.json(data);
  };
  try {
    assert.deepEqual(
      await voiceApi.setup("workspace-1", { request_id: "request-1" }, "csrf"),
      data,
    );
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = previous;
  }
});

test("voice outcomes link only their actual record kind and keep hold IDs distinct", () => {
  const { VoiceRecord } = load("voice-session.tsx");
  const outcomes = ["case", "promise", "hold"].map((kind) => ({
    invocation_id: `invoke-${kind}`,
    action_id: `action-${kind}`,
    invoice_id: "invoice-1",
    record_kind: kind,
    changed_ids: [`saved-${kind}`],
  }));
  const html = renderToStaticMarkup(
    React.createElement(VoiceRecord, {
      session: { ...session, outcomes },
      workspace: "workspace-1",
    }),
  );
  assert.match(html, /cases\/saved-case/);
  assert.match(html, /promises\/saved-promise/);
  assert.match(html, /receivables\/invoice-1/);
  assert.match(html, /Saved hold saved-hold/);
  assert.doesNotMatch(html, /tasks\/saved-(?:case|promise|hold)/);
});

test("LIVE record labels actual participant and accepted stop without claiming an ended call or synthetic fixture", () => {
  const { VoiceRecord } = load("voice-session.tsx");
  const html = renderToStaticMarkup(
    React.createElement(VoiceRecord, {
      workspace: "workspace-1",
      session: {
        ...session,
        state: "active",
        evidence_mode: "LIVE",
        live_status: "SELECTED",
        participant_email: "participant@synthetic.invalid",
        stop_outcome: "ACCEPTED",
      },
    }),
  );
  assert.match(html, /Browser voice session/);
  assert.match(html, /participant@synthetic.invalid/);
  assert.match(html, /Stop accepted.*waiting for authenticated completion/);
  assert.doesNotMatch(
    html,
    /Offline voice session|synthetic offline participant|Fixed synthetic scenario/,
  );
});

test("join and end post only exact session version and CSRF, never create a provider call or retry", async () => {
  const { voiceApi } = load("voice-api.ts");
  const previous = globalThis.fetch;
  const paths = [];
  globalThis.fetch = async (path, init) => {
    paths.push(path);
    assert.equal(init.headers["x-csrf-token"], "csrf");
    assert.equal(init.cache, "no-store");
    assert.deepEqual(JSON.parse(init.body), { expected_version: 3 });
    return { ok: false, status: 409 };
  };
  try {
    await assert.rejects(
      voiceApi.join(
        "workspace-1",
        "session-1",
        { expected_version: 3 },
        "csrf",
      ),
    );
    await assert.rejects(
      voiceApi.end("workspace-1", "session-1", { expected_version: 3 }, "csrf"),
    );
    assert.deepEqual(paths, [
      "/api/workspaces/workspace-1/voice-sessions/session-1/join",
      "/api/workspaces/workspace-1/voice-sessions/session-1/end",
    ]);
  } finally {
    globalThis.fetch = previous;
  }
});

test("authenticated completion takes precedence over historical accepted or unknown stop", () => {
  const { VoiceRecord } = load("voice-session.tsx");
  for (const stop_outcome of ["ACCEPTED", "UNKNOWN"]) {
    const html = renderToStaticMarkup(
      React.createElement(VoiceRecord, {
        workspace: "workspace-1",
        session: {
          ...session,
          evidence_mode: "LIVE",
          state: "completed",
          stop_outcome,
        },
      }),
    );
    assert.match(html, /Authenticated completion received/);
    assert.match(html, new RegExp(`Saved stop outcome:.*${stop_outcome}`));
    assert.doesNotMatch(html, /waiting for authenticated completion/);
  }
});
