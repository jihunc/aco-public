import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { libDouble } from "./lib-support.mjs";
const requireFromWeb = createRequire(
  new URL("../package.json", import.meta.url),
);
const ts = requireFromWeb("typescript");
const React = requireFromWeb("react");
const { renderToStaticMarkup } = requireFromWeb("react-dom/server");
function load(file) {
  const source = readFileSync(
    new URL(`../src/features/agent/${file}`, import.meta.url),
    "utf8",
  );
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.CommonJS,
      esModuleInterop: true,
    },
  }).outputText;
  const module = { exports: {} };
  const localRequire = (name) =>
    name === "next/link"
      ? ({ href, children, ...props }) =>
          React.createElement("a", { href, ...props }, children)
      : name === "@/features/receivables/shared"
        ? { stamp: (value) => value }
        : name === "@/lib/api"
          ? { readApi: () => {} }
          : requireFromWeb(name);
  new Function("require", "module", "exports", compiled)(
    (n) => libDouble(n) ?? localRequire(n),
    module,
    module.exports,
  );
  return module.exports;
}
const { AgentActivity } = load("activity.tsx");
const run = {
  id: "run-1",
  status: "completed",
  version: 4,
  invoice_ids: ["invoice-1"],
  objective: "Review outstanding evidence",
  authenticity: "FAKE",
  model_id: "fake-investigator",
  model_route: "default",
  execution: "UNAVAILABLE",
  requests: 1,
  input_tokens: null,
  output_tokens: null,
  provider_attempted: false,
  usage_ids: ["usage-1"],
  created_at: "2026-11-18T00:00:00Z",
  updated_at: "2026-11-18T00:01:00Z",
  last_event_sequence: 5,
  error_code: null,
  result: {
    summary: "Source balance requires review",
    invoice_findings: [
      {
        invoice_id: "invoice-1",
        assessment: "No verified payment",
        evidence_step_ids: ["step-1", "step-2"],
      },
    ],
    proposals: [
      {
        invoice_id: "invoice-1",
        kind: "human_review",
        reason: "Resolve missing evidence",
        executed: false,
      },
    ],
    limitations: ["Source snapshots are last-known facts"],
  },
};
const steps = [
  {
    id: "step-1",
    run_id: "run-1",
    step_sequence: 1,
    kind: "read",
    status: "completed",
    tool_name: "get_invoice",
    description: "Read selected invoice",
    invoice_id: "invoice-1",
    input: { invoice_id: "invoice-1" },
    output: { source_revision: "revision-7", balance: "1200" },
    error_code: null,
    usage_ids: [],
    created_at: run.created_at,
    updated_at: run.updated_at,
  },
];
const render = (value = run, records = steps) =>
  renderToStaticMarkup(
    React.createElement(AgentActivity, {
      run: value,
      steps: records,
      workspace: "workspace-1",
    }),
  );
test("persisted findings link to actual evidence, read input/output and invoice", () => {
  const html = render();
  assert.match(html, /Simulated model/);
  assert.match(html, /Read selected invoice/);
  assert.match(html, /revision-7/);
  assert.match(html, /href="#agent-step-step-1"/);
  assert.match(html, /receivables\/invoice-1\?workspace=workspace-1/);
  assert.match(html, /Proposed · not executed/);
  assert.doesNotMatch(
    html,
    /<button[^>]*>Execute|Case created|Task created|Promise recorded/,
  );
});
test("failed tool and unavailable result never imply completed action", () => {
  const html = render(
    { ...run, status: "failed", error_code: "MODEL_UNAVAILABLE", result: null },
    [
      {
        ...steps[0],
        status: "failed",
        output: null,
        error_code: "UNSUPPORTED_CAPABILITY",
      },
    ],
  );
  assert.match(html, /Failed/);
  assert.match(html, /UNSUPPORTED_CAPABILITY/);
  assert.match(html, /No result has been committed/);
  assert.doesNotMatch(
    html,
    /Investigation completed|Source balance requires review/,
  );
});
test("live provenance shows returned model and unknown tokens without invented costs", () => {
  const html = render({
    ...run,
    authenticity: "LIVE",
    model_id: "actual-returned-model",
  });
  assert.match(html, /Live model · actual-returned-model/);
  assert.match(html, /Unknown/);
  assert.doesNotMatch(html, /Simulated model|\$0/);
});
test("untrusted tool and result text is escaped as inert content", () => {
  const html = render({
    ...run,
    result: { ...run.result, summary: "<script>send()</script>" },
  });
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});
test("executed task outcomes identify the committed action and actual task records", () => {
  const html = render({
    ...run,
    execution: "EXECUTED",
    result: {
      ...run.result,
      proposals: [
        {
          invoice_id: "invoice-1",
          kind: "create_task",
          title: "Request remittance evidence",
          reason: "Payment unverified",
          executed: true,
          action_id: "action-123",
          changed_ids: ["task-456"],
        },
      ],
    },
  });
  assert.match(html, /Executed · committed/);
  assert.match(html, /Request remittance evidence/);
  assert.match(html, /action-123/);
  assert.match(html, /task-456/);
  assert.match(html, /href="\/review\?workspace=workspace-1"/);
  assert.doesNotMatch(
    html,
    /Proposed · not executed|Action execution is unavailable/,
  );
});

const { TaskRecord } = load("task.tsx");
test("committed task detail renders actual persisted fields and navigation", () => {
  const html = renderToStaticMarkup(
    React.createElement(TaskRecord, {
      workspace: "workspace-1",
      task: {
        id: "task-456",
        title: "Request remittance",
        notes: "Evidence missing",
        status: "open",
        origin: "generated",
        version: 2,
        priority: 3,
        due_at: null,
        invoice_id: "invoice-1",
        assignee_id: null,
        source_action_id: null,
        deleted_at: null,
      },
    }),
  );
  assert.match(html, /Request remittance/);
  assert.match(html, /Evidence missing/);
  assert.match(html, /Version 2/);
  assert.match(html, /Generated/);
  assert.match(html, /review\?workspace=workspace-1/);
  assert.doesNotMatch(html, /Mark Paid|<button/);
});

test("executed case and promise outcomes link only their committed primary records", () => {
  const html = render({
    ...run,
    execution: "EXECUTED",
    result: {
      ...run.result,
      proposals: [
        {
          invoice_id: "invoice-1",
          kind: "upsert_case",
          reason: "Exact source issue",
          executed: true,
          action_id: "case-action",
          changed_ids: ["case-123"],
        },
        {
          invoice_id: "invoice-1",
          kind: "record_promise",
          reason: "Captured commitment",
          executed: true,
          action_id: "promise-action",
          changed_ids: ["promise-456"],
        },
        {
          invoice_id: "invoice-1",
          kind: "record_promise",
          reason: "Unexecuted proposal",
          executed: false,
          action_id: null,
          changed_ids: ["uncommitted-789"],
        },
      ],
    },
  });
  assert.match(html, /href="\/cases\/case-123\?workspace=workspace-1"/);
  assert.match(html, /href="\/promises\/promise-456\?workspace=workspace-1"/);
  assert.match(html, /Open case case-123/);
  assert.match(html, /Open promise promise-456/);
  assert.doesNotMatch(html, /href="[^"]*uncommitted-789/);
});

test("approval proposal deep-links its actual decision and exposes task only after committed execution", () => {
  const proposal = {
    invoice_id: "invoice-1",
    kind: "request_task_approval",
    reason: "Review before creating task",
    executed: false,
    approval_id: "approval-1",
    approval_status: "pending",
    action_id: "action-1",
    changed_ids: [],
  };
  const html = render({
    ...run,
    status: "awaiting_approval",
    result: { ...run.result, proposals: [proposal] },
  });
  assert.match(html, /Awaiting independent approval/);
  assert.match(html, /review\?workspace=workspace-1&amp;approval=approval-1/);
  assert.doesNotMatch(html, /Executed · committed|href="\/tasks\//);
  const completed = render({
    ...run,
    result: {
      ...run.result,
      proposals: [
        {
          ...proposal,
          executed: true,
          approval_status: "executed",
          changed_ids: ["task-1"],
        },
      ],
    },
  });
  assert.match(completed, /href="\/tasks\/task-1\?workspace=workspace-1"/);
  assert.match(completed, /Executed · committed/);
  assert.match(completed, /Original AI recommendation/);
  assert.match(
    completed,
    /approved proposal history and committed record contain the executed text/,
  );
  const denied = render({
    ...run,
    result: {
      ...run.result,
      proposals: [{ ...proposal, approval_status: "denied" }],
    },
  });
  assert.match(denied, /Denied · not executed/);
});

test("committed contact suppression has human labels and invoice/review navigation, never task links for hold IDs", () => {
  const proposal = {
    invoice_id: "invoice-1",
    kind: "request_collection_hold",
    reason: "Customer requested no further contact",
    executed: true,
    title: null,
    action_id: "hold-action",
    changed_ids: ["hold-1", "hold-2"],
  };
  const html = render({
    ...run,
    execution: "EXECUTED",
    result: { ...run.result, proposals: [proposal] },
  });
  assert.match(html, /Pause contact pending review/);
  assert.match(html, /Customer contact holds were saved pending human review/);
  assert.match(html, /Saved hold references/);
  assert.match(html, /href="\/review\?workspace=workspace-1"/);
  assert.match(html, /receivables\/invoice-1\?workspace=workspace-1/);
  assert.doesNotMatch(
    html,
    /tasks\/hold-|Open task hold-|recipient verified|message sent/i,
  );
  const pending = render({
    ...run,
    result: {
      ...run.result,
      proposals: [{ ...proposal, executed: false, changed_ids: [] }],
    },
  });
  assert.match(pending, /Proposed · not executed/);
  assert.doesNotMatch(pending, /Customer contact holds were saved/);
});

test("human review and scheduled followup outcomes link actual tasks without outreach claims", () => {
  for (const kind of [
    "request_human_review",
    "human_review",
    "verify_accounting",
    "schedule_followup",
    "follow_up",
  ]) {
    const html = render({
      ...run,
      execution: "EXECUTED",
      result: {
        ...run.result,
        proposals: [
          {
            invoice_id: "invoice-1",
            kind,
            reason: "Current source needs human work",
            executed: true,
            action_id: "human-work-action",
            changed_ids: ["human-work-task"],
          },
        ],
      },
    });
    assert.match(
      html,
      /href="\/tasks\/human-work-task\?workspace=workspace-1"/,
    );
    assert.match(html, /href="\/review\?workspace=workspace-1"/);
    assert.match(
      html,
      ["schedule_followup", "follow_up"].includes(kind)
        ? /Scheduled human follow-up task/
        : /Human review task/,
    );
    assert.doesNotMatch(
      html,
      /Email sent|Accounting refreshed|Agent scheduled/i,
    );
  }
});

test("email outcomes distinguish guarded waiting and retained original draft from accepted communication", () => {
  const email = {
    invoice_id: "invoice-1",
    kind: "request_email_send",
    reason: "Exact independent review",
    executed: true,
    approval_id: "review-1",
    approval_status: "executed",
    action_id: "action-1",
    changed_ids: ["message-1"],
    email_command: {
      invoice_id: "invoice-1",
      subject: "Original synthetic subject",
      content: "Original synthetic body",
    },
  };
  const html = renderToStaticMarkup(
    React.createElement(AgentActivity, {
      run: {
        ...run,
        status: "awaiting_action",
        result: { ...run.result, proposals: [email] },
      },
      steps: [],
      workspace: "workspace-1",
    }),
  );
  assert.match(html, /Awaiting guarded email execution/);
  assert.match(html, /Original synthetic subject/);
  assert.match(html, /Original synthetic body/);
  assert.match(html, /communications\/message-1/);
  assert.match(html, /provider acceptance is not delivery/i);
});

test("agent voice request links the saved session and review without claiming a call", () => {
  const html = render({
    ...run,
    result: {
      ...run.result,
      proposals: [
        {
          invoice_id: "invoice-1",
          kind: "request_voice_call",
          reason: "Independent conversation review",
          executed: true,
          action_id: "action-voice",
          approval_id: "approval-voice",
          voice_session_id: "session-voice",
          changed_ids: [],
        },
      ],
    },
  });
  assert.match(html, /Voice review requested · call not started/);
  assert.match(html, /href="\/voice\/session-voice\?workspace=workspace-1"/);
  assert.match(html, /approval=approval-voice/);
  assert.match(html, /Capture remains disabled/);
  assert.doesNotMatch(
    html,
    /Call started|Voice call completed|Open created task|Executed · committed/,
  );
});

test("committed task update links the existing task and does not claim case or financial resolution", () => {
  const html = render({
    ...run,
    execution: "EXECUTED",
    result: {
      ...run.result,
      proposals: [
        {
          invoice_id: "invoice-1",
          kind: "update_collection_task",
          reason: "Existing internal work updated after current review",
          task_update: {
            task_id: "existing-task",
            expected_version: 3,
            status: "done",
          },
          executed: true,
          action_id: "update-action",
          changed_ids: ["existing-task"],
        },
      ],
    },
  });
  assert.match(html, /href="\/tasks\/existing-task\?workspace=workspace-1"/);
  assert.match(html, /Task fields updated/);
  assert.match(html, /does not resolve a case or verify payment/);
  assert.doesNotMatch(html, /case resolved|invoice paid|new task created/i);
});
