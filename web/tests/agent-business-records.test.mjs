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
let apiPayload;
let apiPath;
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
        esModuleInterop: true,
      },
    },
  ).outputText;
  const module = { exports: {} };
  const localRequire = (name) =>
    name === "@/features/conversations/voice-business-source"
      ? load("../conversations/voice-business-source.tsx")
      : name === "next/link"
        ? ({ href, children }) => React.createElement("a", { href }, children)
        : name === "@/features/receivables/shared"
          ? { stamp: (value) => value ?? "Unavailable", ErrorState: () => null }
          : [
                "./case-commands",
                "./case-review",
                "./handling-time",
                "./promise-capture",
              ].includes(name)
            ? {}
            : name === "./business-api"
              ? {}
              : name === "@/lib/api"
                ? {
                    readApi: async (path) => {
                      apiPath = path;
                      return apiPayload;
                    },
                  }
                : name === "@/lib/money"
                  ? load("../../lib/money.ts")
                  : requireFromWeb(name);
  new Function("require", "module", "exports", compiled)(
    (n) => libDouble(n) ?? localRequire(n),
    module,
    module.exports,
  );
  return module.exports;
}
const { CaseRecord } = load("cases.tsx");
const record = {
  id: "case-1",
  customer_id: "customer-1",
  issue_type: "wrong_amount",
  claim_key: "claim-1",
  substance: "Customer challenges invoice amount",
  status: "OPEN",
  version: 3,
  opened_at: "2026-11-18T00:00:00Z",
  updated_at: "2026-11-18T00:10:00Z",
  resolved_at: null,
  closed_at: null,
  closure_reason: null,
  review_required: true,
  task_id: "task-1",
  synthetic: true,
  invoice_links: [
    {
      invoice_id: "invoice-1",
      issue_state: "OPEN",
      impact: "disputed",
      hold_id: "hold-1",
      resolution_action: null,
      resolution_evidence_id: null,
      resolved_at: null,
    },
  ],
  evidence: [
    {
      id: "evidence-1",
      communication_id: "message-1",
      excerpt: "<script>wrong amount</script>",
      offset: 12,
      kind: "communication",
      synthetic: true,
      source_state: "PRESERVED",
      created_at: "2026-11-18T00:00:00Z",
    },
  ],
  history: [],
};
test("case read detail preserves per-invoice impact, source evidence and real record links", () => {
  const html = renderToStaticMarkup(
    React.createElement(CaseRecord, { record, workspace: "workspace-1" }),
  );
  assert.match(html, /Customer challenges invoice amount/);
  assert.match(html, /Synthetic case/);
  assert.match(html, /Review required/);
  assert.match(html, /hold-1/);
  assert.match(html, /communications\/message-1\?workspace=workspace-1/);
  assert.match(html, /tasks\/task-1\?workspace=workspace-1/);
  assert.match(html, /&lt;script&gt;wrong amount&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>|Mark Paid|<button/);
});

const { PromiseRecord } = load("promises.tsx");
const promise = {
  id: "promise-1",
  customer_id: "customer-1",
  status: "NEEDS_CONFIRMATION",
  version: 2,
  source_communication_id: "message-1",
  source_text: "We will pay all invoices Friday",
  source_offset: 0,
  source_at: "2026-11-18T00:00:00Z",
  source_state: "PRESERVED",
  synthetic: true,
  amount: { minor_units: "10000000000000001", currency: "AUD", exponent: 2 },
  due_date: null,
  timezone: null,
  cutoff_at: null,
  confirmed_at: null,
  confirmation_communication_id: null,
  needs_confirmation: true,
  confirmation_reasons: ["ambiguous_scope"],
  invoice_links: [{ invoice_id: "invoice-1", amount: null, hold_id: "hold-1" }],
  candidate_invoice_ids: ["invoice-2"],
  installments: [],
  replaces_promise_id: null,
  superseded_by_id: null,
  review_task_id: "task-1",
  created_at: "2026-11-18T00:00:00Z",
  updated_at: "2026-11-18T00:01:00Z",
  history: [],
  scheduling_status: "PENDING_CONFIRMATION",
  verification_status: "UNVERIFIED",
};
test("unconfirmed promise preserves unknown allocation and source text without claiming payment or scheduling", () => {
  const html = renderToStaticMarkup(
    React.createElement(PromiseRecord, {
      record: promise,
      workspace: "workspace-1",
    }),
  );
  assert.match(html, /Needs confirmation/);
  assert.match(html, /We will pay all invoices Friday/);
  assert.match(html, /AUD 100,000,000,000,000.01/);
  assert.match(html, /Allocation not confirmed/);
  assert.match(html, /Pending confirmation/);
  assert.match(html, /communications\/message-1\?workspace=workspace-1/);
  assert.doesNotMatch(
    html,
    /Payment verified|Payment check scheduled|Mark Paid|<button/,
  );
});

const { promiseApi } = load("business-api.ts");
test("promise API refuses numeric money before the browser can display rounded financial evidence", async () => {
  apiPayload = {
    amount: {
      minor_units: Number("10000000000000001"),
      currency: "AUD",
      exponent: 2,
    },
    invoice_links: [],
    installments: [],
  };
  await assert.rejects(
    promiseApi.get("workspace-1", "promise-1"),
    /exact minor-unit strings/,
  );
});

for (const [state, label] of Object.entries({
  QUEUED: "Queued",
  RUNNING: "Running",
  COMPLETED: "Completed",
  REVIEW_REQUIRED: "Review required",
  PENDING_ENGINE: "Pending engine",
})) {
  test(`promise displays actual ${state} scheduling without converting it to payment success`, () => {
    const html = renderToStaticMarkup(
      React.createElement(PromiseRecord, {
        record: {
          ...promise,
          status: "FULFILLED",
          scheduling_status: state,
          verification_status: "PENDING_REVIEW",
          needs_confirmation: false,
        },
        workspace: "workspace-1",
      }),
    );
    assert.match(html, new RegExp(`Check scheduling:</strong> ${label}`));
    assert.match(html, /Verification pending review/);
    assert.match(html, /Prior commitment status is retained/);
    assert.doesNotMatch(html, /Payment verified|Financially paid/);
  });
}

test("promise check plans retain exact action, invoice and immutable confirmation references", () => {
  const { PromiseCheckPlans } = load("promises.tsx");
  const html = renderToStaticMarkup(
    React.createElement(PromiseCheckPlans, {
      workspace: "workspace-1",
      plans: [
        {
          promise_id: "promise-1",
          promise_version: 8,
          event_id: "confirmation-1",
          due_at: "2026-11-20T13:00:00Z",
          invoice_ids: ["invoice-1"],
          installment_id: "installment-1",
          status: "COMPLETED",
          action_ids: ["action-1"],
        },
      ],
    }),
  );
  assert.match(html, /Completed/);
  assert.match(html, /confirmation-1/);
  assert.match(html, /installment-1/);
  assert.match(html, /Version 8/);
  assert.match(
    html,
    /api\/workspaces\/workspace-1\/scheduled-actions\/action-1/,
  );
  assert.match(html, /receivables\/invoice-1\?workspace=workspace-1/);
  assert.doesNotMatch(html, /Payment verified|Fulfilled|<button/);
});

test("promise check-plan API reads the actual workspace scoped nonmutating route", async () => {
  apiPayload = [];
  assert.deepEqual(await promiseApi.checkPlans("workspace-1", "promise-1"), []);
  assert.equal(
    apiPath,
    "/api/workspaces/workspace-1/promises/promise-1/check-plans",
  );
});

test("pending source history preserves immutable evidence and labels rejected source as unverified", () => {
  const html = renderToStaticMarkup(
    React.createElement(PromiseRecord, {
      workspace: "workspace-1",
      record: {
        ...promise,
        status: "FULFILLED",
        verification_status: "PENDING_REVIEW",
        history: [
          {
            id: "event-immutable",
            event: "promise.verification_pending",
            actor_id: "actor-1",
            created_at: promise.created_at,
            details: {
              prior_status: "FULFILLED",
              status: "FULFILLED",
              performance: null,
              reason: "provider_failure:TIMEOUT",
              observation_ids: ["observation-1"],
              allocation_ids: ["cash-1"],
              reversal_ids: ["refund-1"],
              excluded_allocation_ids: ["other-promise-cash"],
              source_failure: {
                rejection_evidence: { authenticity: "UNVERIFIED" },
              },
            },
          },
        ],
      },
    }),
  );
  for (const ref of [
    "event-immutable",
    "observation-1",
    "cash-1",
    "refund-1",
    "other-promise-cash",
  ])
    assert.match(html, new RegExp(ref));
  assert.match(html, /Commitment status: FULFILLED → FULFILLED/);
  assert.match(html, /Rejected source references are unverified/);
  assert.doesNotMatch(html, /Recorded performance:|Payment verified/);
});

test("amendment confirmation shows original and replacement versions and requires a fresh separate review", () => {
  const { PromiseDecisionControls } = load("promises.tsx");
  const html = renderToStaticMarkup(
    React.createElement(PromiseDecisionControls, {
      workspace: "w",
      record: {
        ...promise,
        id: "replacement",
        version: 3,
        replaces_promise_id: "original",
      },
      replacement: { ...promise, id: "original", version: 7, status: "ACTIVE" },
      csrfToken: "csrf",
      onChanged: async () => {},
    }),
  );
  assert.match(html, /Reviewed original version 7/);
  assert.match(html, /Replacement version 3/);
  assert.match(html, /Capture alone has not replaced it/);
  assert.match(html, /promises\/original\?workspace=w/);
  assert.match(html, /<button[^>]*disabled[^>]*>Confirm commitment/);
});
