import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript"),
  React = req("react");
const { renderToStaticMarkup } = req("react-dom/server");
function load(file) {
  const output = ts.transpileModule(
    readFileSync(
      new URL(`../src/features/review/${file}`, import.meta.url),
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
      : name === "@/features/receivables/shared"
        ? { stamp: (v) => v ?? "Unavailable", ErrorState: () => null }
        : name === "@/lib/api"
          ? {}
          : name === "./approval-api"
            ? load("approval-api.ts")
            : name === "@/lib/money"
              ? load("../../lib/money.ts")
              : req(name);
  new Function("require", "module", "exports", output)(local, mod, mod.exports);
  return mod.exports;
}
const record = {
  invoice_scope: {
    invoice_id: "invoice-1",
    number: "INV-031",
    customer_id: "customer-1",
    source_revision: "revision-7",
    outstanding: {
      minor_units: "10000000000000001",
      currency: "AUD",
      exponent: 2,
    },
    financial_status: "open",
    observed_at: "2026-11-18T00:00:00Z",
    synthetic: true,
    verified: false,
  },
  evidence: [
    {
      id: "source-1",
      invoice_id: "invoice-1",
      source_revision: "revision-7",
      origin: "demo",
      source_json:
        '{"envelope":{"amount_minor":10000000000000001},"result":"<script>source</script>"}',
    },
  ],
  id: "approval-1",
  version: 3,
  action_id: "action-1",
  action_version: 7,
  proposer_id: "proposer",
  initiator_id: "initiator",
  source_action_initiator_id: "source-initiator",
  customer_id: "customer-1",
  material_hash: "a".repeat(64),
  current_binding_hash: "b".repeat(64),
  proposal: {
    action_id: "action-1",
    action_version: 7,
    content: "<script>Pay invoice</script>",
    destination: "customer@example.test",
    channel: "email",
    invoice_ids: ["invoice-1"],
    evidence_ids: ["source-1"],
    policy_version_id: "policy-1",
    cost_bound: "0.012500",
    currency: "USD",
    consent_mode: "no_capture",
  },
  status: "pending",
  effective_status: "pending",
  approval_valid: false,
  new_approval_required: true,
  can_approve: true,
  can_deny: true,
  reasons: [],
  granted_at: null,
  expires_at: null,
  consumed_at: null,
  created_at: "2026-11-18T00:00:00Z",
  updated_at: "2026-11-18T00:00:00Z",
  decisions: [],
  dispatch_status: "NOT_ENABLED",
};
test("decision payload retains the reviewed snapshot after current data refreshes", () => {
  const { captureApproval, decisionCommand, sameApprovalSnapshot } =
    load("approval-api.ts");
  const captured = captureApproval(record);
  const refreshed = {
    ...record,
    version: 4,
    action_version: 8,
    material_hash: "c".repeat(64),
    current_binding_hash: "d".repeat(64),
  };
  assert.equal(sameApprovalSnapshot(captured, refreshed), false);
  assert.deepEqual(
    decisionCommand(captured, "approved", "Reviewed evidence", 3600),
    {
      expected_version: 3,
      reviewed_action_version: 7,
      reviewed_material_hash: "a".repeat(64),
      reviewed_binding_hash: "b".repeat(64),
      decision: "approved",
      reason: "Reviewed evidence",
      expires_in_seconds: 3600,
    },
  );
  record.proposal.content = "Changed in query cache";
  assert.equal(captured.proposal.content, "<script>Pay invoice</script>");
  record.proposal.content = "<script>Pay invoice</script>";
});
test("approval detail renders exact proposal, scope and cost while preserving role and server restrictions", () => {
  const { ApprovalRecord } = load("approvals.tsx");
  const html = renderToStaticMarkup(
    React.createElement(ApprovalRecord, {
      record,
      workspace: "workspace-1",
      canReview: false,
      onReview: () => {},
    }),
  );
  assert.match(html, /customer@example.test/);
  assert.match(html, /&lt;script&gt;Pay invoice&lt;\/script&gt;/);
  assert.match(html, /USD 0.012500/);
  assert.match(html, /receivables\/invoice-1/);
  assert.match(html, /Accounting source references: source-1/);
  assert.doesNotMatch(html, /communications\/source-1/);
  assert.match(html, /Sending is not enabled/);
  assert.doesNotMatch(html, /Review proposal|<script>/);
  const restricted = renderToStaticMarkup(
    React.createElement(ApprovalRecord, {
      record: {
        ...record,
        can_approve: false,
        can_deny: false,
        reasons: ["SELF_APPROVAL_FORBIDDEN"],
      },
      workspace: "workspace-1",
      canReview: true,
      onReview: () => {},
    }),
  );
  assert.match(restricted, /another reviewer/i);
  assert.doesNotMatch(restricted, /Review proposal/);
});
test("a consumed approval does not repeat stale-review restrictions", () => {
  const { ApprovalRecord } = load("approvals.tsx");
  const html = renderToStaticMarkup(
    React.createElement(ApprovalRecord, {
      record: { ...record, status: "consumed", effective_status: "consumed", can_approve: false, can_deny: false, reasons: ["MATERIAL_CHANGED", "CONSUMED"] },
      workspace: "workspace-1",
      canReview: true,
      onReview: () => {},
    }),
  );
  assert.doesNotMatch(html, /A fresh review is required/);
});
test("approval decision sends captured preconditions and does not retry conflicts", async () => {
  const { approvalApi, decisionCommand, captureApproval } =
    load("approval-api.ts");
  let calls = 0;
  const prior = globalThis.fetch;
  globalThis.fetch = async (path, init) => {
    calls++;
    assert.equal(
      path,
      "/api/workspaces/workspace-1/approvals/approval-1/decisions",
    );
    assert.equal(init.headers["x-csrf-token"], "csrf");
    assert.equal(JSON.parse(init.body).expected_version, 3);
    return {
      ok: false,
      status: 409,
      json: async () => ({ detail: "APPROVAL_STALE" }),
    };
  };
  try {
    await assert.rejects(
      approvalApi.decide(
        "workspace-1",
        "approval-1",
        decisionCommand(
          captureApproval(record),
          "denied",
          "Source mismatch",
          3600,
        ),
        "csrf",
      ),
      /changed|review/i,
    );
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = prior;
  }
});

test("approval shows exact accounting source and last-known financial scope without inventing verification", () => {
  const { ApprovalRecord } = load("approvals.tsx");
  const html = renderToStaticMarkup(
    React.createElement(ApprovalRecord, {
      record,
      workspace: "workspace-1",
      canReview: true,
      onReview: () => {},
    }),
  );
  assert.match(html, /INV-031/);
  assert.match(html, /AUD 100,000,000,000,000.01/);
  assert.match(html, /Synthetic/);
  assert.match(html, /Last-known/);
  assert.match(html, /10000000000000001/);
  assert.match(html, /&lt;script&gt;source&lt;\/script&gt;/);
  assert.doesNotMatch(
    html,
    /communications\/source-1|Source verified|<script>/,
  );
});

test("internal task approval shows current execution, exact read evidence and real task links without invented outreach", () => {
  const { ApprovalRecord } = load("approvals.tsx");
  const internal = {
    ...record,
    effect: "internal_task",
    source_run_id: "run-1",
    source_step_id: "step-action",
    proposal: {
      effect: "internal_task",
      action_id: "action-1",
      action_version: 7,
      run_id: "run-1",
      invoice_ids: ["invoice-1"],
      evidence_step_ids: ["read-1", "read-2"],
      title: "Review exact invoice evidence",
      notes: "<script>untrusted source</script>",
    },
    evidence: [],
    agent_evidence: [
      {
        step_id: "read-1",
        run_id: "run-1",
        tool_name: "get_invoice",
        source_json: '{"verified":true}',
      },
    ],
    effective_status: "consumed",
    execution_status: "executed",
    changed_ids: ["task-1"],
    dispatch_status: "NOT_APPLICABLE",
    can_approve: false,
    can_deny: false,
  };
  const html = renderToStaticMarkup(
    React.createElement(ApprovalRecord, {
      record: internal,
      workspace: "workspace-1",
      canReview: true,
      onReview() {},
    }),
  );
  assert.match(html, /Internal task proposal/);
  assert.match(html, /Review exact invoice evidence/);
  assert.match(html, /&lt;script&gt;untrusted source&lt;\/script&gt;/);
  assert.match(html, /Internal task created/);
  assert.match(html, /tasks\/task-1\?workspace=workspace-1/);
  assert.match(html, /agent-runs\/run-1\?workspace=workspace-1/);
  assert.match(html, /get_invoice/);
  assert.doesNotMatch(
    html,
    /Maximum proposed cost|Capture consent|To undefined|Call proposal|Review proposal|<script>/,
  );
  const queued = renderToStaticMarkup(
    React.createElement(ApprovalRecord, {
      record: {
        ...internal,
        effective_status: "approved",
        execution_status: "queued",
        changed_ids: [],
      },
      workspace: "workspace-1",
      canReview: false,
      onReview() {},
    }),
  );
  assert.match(queued, /Approved · awaiting guarded execution/);
  assert.doesNotMatch(queued, /Internal task created|tasks\/task-1/);
});

test("voice approval renders exact session scope and capture restriction without internal-task inference", () => {
  const { ApprovalRecord } = load("approvals.tsx");
  const voice = {
    ...record,
    effect: "voice_session",
    proposal: {
      effect: "voice_session",
      action_id: "voice-action",
      action_version: 1,
      session_id: "session-1",
      invoice_ids: ["invoice-1"],
      objective: "Review source balance",
      allowed_tools: ["get_invoice", "create_collection_task"],
      evidence_mode: "MOCKED_HTTP",
      capture: "DISABLED",
      participant: "SYNTHETIC_OFFLINE",
    },
  };
  const html = renderToStaticMarkup(
    React.createElement(ApprovalRecord, {
      record: voice,
      workspace: "workspace-1",
      canReview: true,
      onReview: () => {},
    }),
  );
  for (const text of [
    "Offline voice session proposal",
    "Review source balance",
    "MOCKED_HTTP",
    "DISABLED",
    "SYNTHETIC_OFFLINE",
    "get_invoice",
    "voice/session-1",
  ])
    assert.ok(html.includes(text), text);
  assert.doesNotMatch(
    html,
    /Internal task proposal|Maximum proposed cost|Accounting source references/,
  );
});

test("internal case review displays the captured case version and keeps it across refresh", () => {
  const { ApprovalRecord } = load("approvals.tsx");
  const { captureApproval, sameApprovalSnapshot } = load("approval-api.ts");
  const bound = {
    ...record,
    effect: "internal_task",
    execution_status: "pending_approval",
    proposal: {
      effect: "internal_task",
      action_id: record.action_id,
      action_version: record.action_version,
      run_id: "run",
      invoice_ids: ["invoice-1"],
      evidence_step_ids: ["step1", "step2", "step3"],
      title: "Review case",
      notes: "Documentary evidence",
      case_binding: { case_id: "case-actual", case_version: 7 },
    },
  };
  const captured = captureApproval(bound);
  const html = renderToStaticMarkup(
    React.createElement(ApprovalRecord, {
      record: captured,
      workspace: "w",
      canReview: true,
      onReview() {},
    }),
  );
  assert.match(html, /cases\/case-actual\?workspace=w/);
  assert.match(html, /Reviewed case version 7/);
  const changed = structuredClone(bound);
  changed.proposal.case_binding.case_version = 8;
  changed.material_hash = "c".repeat(64);
  changed.current_binding_hash = "d".repeat(64);
  assert.equal(sameApprovalSnapshot(captured, changed), false);
  assert.equal(captured.proposal.case_binding.case_version, 7);
});

test("material edit captures the original bindings, offers only permitted text and never retries conflict", async () => {
  const { modificationCommand, approvalApi } = load("approval-api.ts");
  const edit = { kind: "outbound_content", content: "Human corrected wording" };
  const command = modificationCommand(record, edit, " Explicit correction ");
  assert.deepEqual(command, {
    expected_version: 3,
    reviewed_action_version: 7,
    reviewed_material_hash: "a".repeat(64),
    reviewed_binding_hash: "b".repeat(64),
    edit,
    reason: "Explicit correction",
  });
  const prior = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async (path, init) => {
    calls++;
    assert.equal(path, "/api/workspaces/w/approvals/approval-1/modifications");
    assert.equal(init.headers["x-csrf-token"], "csrf");
    assert.deepEqual(JSON.parse(init.body), command);
    return { ok: false, status: 409 };
  };
  try {
    await assert.rejects(
      approvalApi.modify("w", record.id, command, "csrf"),
      /changed|review/i,
    );
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = prior;
  }
});

test("editable proposal shows immutable replacement history and current server eligibility", () => {
  const { ApprovalRecord } = load("approvals.tsx");
  const editable = {
    ...record,
    can_modify: true,
    editor_id: "editor-1",
    supersedes_id: "old-1",
    edit_reason: "Clarified words",
  };
  const props = {
    record: editable,
    workspace: "w",
    canReview: true,
    onReview() {},
    onModify() {},
  };
  const html = renderToStaticMarkup(React.createElement(ApprovalRecord, props));
  assert.match(html, /Modify proposal/);
  assert.match(html, /editor-1/);
  assert.match(html, /Clarified words/);
  assert.match(html, /approval=old-1/);
  const blocked = renderToStaticMarkup(
    React.createElement(ApprovalRecord, {
      ...props,
      record: { ...editable, can_modify: false, replacement_id: "new-2" },
    }),
  );
  assert.doesNotMatch(blocked, /Modify proposal/);
  assert.match(blocked, /approval=new-2/);
});

test("agent email approval renders exact subject and distinguishes guarded acceptance from disabled legacy sending", () => {
  const { ApprovalRecord } = load("approvals.tsx");
  const html = renderToStaticMarkup(
    React.createElement(ApprovalRecord, {
      record: {
        ...record,
        proposal: {
          ...record.proposal,
          effect: "agent_email",
          subject: "Exact reviewed subject",
        },
        dispatch_status: "ACCEPTED",
        execution_status: "executed",
        changed_ids: ["message-1"],
      },
      workspace: "workspace-1",
      canReview: false,
      onReview: () => {},
    }),
  );
  assert.match(html, /Exact reviewed subject/);
  assert.match(html, /Provider accepted/);
  assert.match(html, /communications\/message-1/);
  assert.doesNotMatch(html, /Sending is not enabled|Internal task created/);
});

test("agent email review distinguishes a shared authorization ceiling from predicted or billed cost", () => {
  const { ApprovalRecord } = load("approvals.tsx");
  const html = renderToStaticMarkup(
    React.createElement(ApprovalRecord, {
      record: {
        ...record,
        proposal: {
          ...record.proposal,
          effect: "agent_email",
          subject: "Reviewed scope",
          cost_basis: "SHARED_GRANT_AUTHORIZATION_CEILING",
          cost_bound: "0.03",
          currency: "USD",
          live_cost_scope: {
            grant_id: "grant-1",
            grant_version: 2,
            currency: "USD",
            max_usd: "0.03",
            max_requests: 3,
            max_messages: 1,
            max_received_messages: 0,
            request_usd_ceiling: "0.01",
            price_source_ref: "explicit-reviewed-ceiling",
            price_source_at: "2026-11-19T00:00:00Z",
          },
        },
        dispatch_status: "AWAITING_REVIEW",
      },
      workspace: "workspace-1",
      canReview: false,
      onReview: () => {},
    }),
  );
  assert.match(html, /Shared-grant authorization ceiling/);
  assert.match(html, /3 provider requests/);
  assert.match(html, /1 outbound message/);
  assert.match(html, /explicit-reviewed-ceiling/);
  assert.match(html, /not a predicted per-email fee/);
  assert.doesNotMatch(html, /this offline scope permits no paid provider call/);
  assert.doesNotMatch(html, /Maximum proposed cost/);
});
