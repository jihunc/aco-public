import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript"),
  React = req("react");
const { renderToStaticMarkup } = req("react-dom/server");
function load(file) {
  const source = ts.transpileModule(
    readFileSync(
      new URL(`../src/features/impact/${file}`, import.meta.url),
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
  const local = (name) =>
    name === "next/link"
      ? ({ href, children }) => React.createElement("a", { href }, children)
      : name === "@/features/receivables/shared"
        ? { stamp: (v) => v ?? "Unavailable", ErrorState: () => null }
        : name === "./economics-api"
          ? load("economics-api.ts")
          : req(name);
  new Function("require", "module", "exports", source)(
    local,
    module,
    module.exports,
  );
  return module.exports;
}
const draft = {
  id: "a",
  version: 1,
  previous_version_id: null,
  creator_id: "owner",
  created_at: "2026-11-19T00:00:00Z",
  reviewed_hash: "a".repeat(64),
  command: {
    request_id: "save",
    expected_previous_version_id: null,
    since: "2026-11-01T00:00:00Z",
    until: "2026-11-20T00:00:00Z",
    currency: "AUD",
    synthetic: true,
    baseline_minutes_per_issue: "10",
    wage_per_hour: "60",
    fixed_cost: "20",
    fixed_cost_evidence: "Synthetic fixed allocation",
    evidence: "<script>untrusted assumption note</script>",
  },
  approval_id: null,
  approved_by: null,
  approved_at: null,
  approval_evidence: null,
};
const event = {
  event_id: "event",
  revision_id: "revision",
  revision_version: 1,
  provider_connection_id: "connection",
  operation_key: "operation",
  component: "voice_total",
  provider: "mock",
  currency: "AUD",
  synthetic: true,
  amount: "0.000000000003",
  quality: "PROVIDER_REPORTED",
  additive: true,
  created_at: "2026-11-19T00:00:00Z",
  allocation: {
    id: null,
    event_id: "event",
    version: 0,
    previous_version_id: null,
    basis_revision_id: null,
    basis: "EQUAL_VERIFIED_LEAVES",
    leaves: [{ case_id: "case", invoice_id: "invoice", weight: "1" }],
    supported_leaves: [{ case_id: "case", invoice_id: "invoice", weight: "1" }],
    source_ids: ["source"],
    source_hash: "b".repeat(64),
    current_source_matches: true,
    evidence: null,
    created_at: null,
  },
};
const view = {
  tenant_id: "w",
  since: draft.command.since,
  until: draft.command.until,
  as_of_at: "2026-11-19T00:00:00Z",
  can_manage: false,
  current_approval_id: null,
  latest_assumption_id: "a",
  assumptions: [draft],
  events: [event],
  case_costs: [],
  invoice_costs: [],
  source_basis: "RECORDED_OPERATING_EVIDENCE",
  caveat: "No realized payroll savings or causal ROI.",
  usage: {
    currencies: [],
    providers: [],
    bundle_issues: [],
    limits: [],
    notices: [],
  },
  groups: [
    {
      currency: "AUD",
      synthetic: true,
      eligible_case_count: 1,
      resolved_case_count: 1,
      case_ids: ["case"],
      handling_entry_ids: ["handling"],
      human_time_basis: "MANUAL_REPORTED",
      reported_human_minutes: "40",
      measured_human_cost: null,
      estimated_reported_human_cost: "40",
      estimated_cost_per_resolution: null,
      known_provider_cost: "0.000000000003",
      provider_basis_complete: false,
      assumptions_id: "a",
      costs: {
        provider: null,
        human: null,
        fixed: "20",
        complete_cost_per_resolution: null,
        status: "partial",
      },
      capacity: {
        estimated_time_saved_minutes: "-30",
        estimated_capacity_value: "-30",
        estimated_net_benefit: null,
        incremental_cost_denominator: null,
        estimated_roi_percent: null,
        label: "Insufficient data",
        realized_payroll_savings_claim: false,
      },
      reasons: ["Unknown provider component; not zero."],
    },
  ],
};
test("review retains exact immutable draft and prior approval; refresh cannot silently replace it", () => {
  const { captureEconomicsReview, canApproveEconomics, approvalCommand } =
    load("economics-api.ts");
  const captured = captureEconomicsReview(draft, null);
  draft.command.wage_per_hour = "61";
  assert.equal(captured.assumption.command.wage_per_hour, "60");
  assert.equal(
    approvalCommand(captured, "decision", "Explicit Owner review")
      .reviewed_hash,
    "a".repeat(64),
  );
  assert.equal(
    canApproveEconomics(
      captured,
      { ...view, assumptions: [captured.assumption], can_manage: true },
      true,
    ),
    true,
  );
  assert.equal(
    canApproveEconomics(
      captured,
      { ...view, can_manage: true, current_approval_id: "different" },
      true,
    ),
    false,
  );
  assert.equal(
    canApproveEconomics(
      captured,
      { ...view, can_manage: true, latest_assumption_id: "new" },
      true,
    ),
    false,
  );
  assert.equal(
    canApproveEconomics(captured, { ...view, can_manage: true }, false),
    false,
  );
});
test("cost display keeps exact amounts, negative capacity, N/A measured cost, source links and inert evidence", () => {
  const { EconomicsReport } = load("economics.tsx");
  const html = renderToStaticMarkup(
    React.createElement(EconomicsReport, { view, workspace: "w" }),
  );
  assert.match(html, /0\.000000000003/);
  assert.match(html, /-30/);
  assert.match(html, /MANUAL_REPORTED/);
  assert.match(html, /Insufficient data/);
  assert.match(html, /1.*resolved/);
  assert.match(html, /cases\/case/);
  assert.match(html, /revision/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.doesNotMatch(html, /Approve reviewed assumptions/);
});
test("approval and allocation post exact captured commands and CSRF once; conflicts never retry", async () => {
  const { economicsApi } = load("economics-api.ts");
  const prior = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return { ok: false, status: 409 };
  };
  try {
    await assert.rejects(
      () =>
        economicsApi.approve(
          "w",
          "a",
          {
            request_id: "id",
            reviewed_hash: "a".repeat(64),
            expected_current_approval_id: "prior",
            evidence: "Reviewed exactly",
          },
          "csrf",
        ),
      /changed/,
    );
    assert.equal(calls.length, 1);
    assert.equal(calls[0].init.headers["x-csrf-token"], "csrf");
    assert.equal(
      JSON.parse(calls[0].init.body).expected_current_approval_id,
      "prior",
    );
  } finally {
    globalThis.fetch = prior;
  }
});
test("allocation review captures exact source/revision and precision validator refuses numeric costs", () => {
  const { captureAllocation, allocationCommand, validateEconomics } =
    load("economics-api.ts");
  const captured = captureAllocation(event);
  event.revision_id = "new-revision";
  const command = allocationCommand(
    captured,
    "request",
    "Reviewed exact original source",
  );
  assert.equal(command.expected_revision_id, "revision");
  assert.equal(command.reviewed_source_hash, "b".repeat(64));
  assert.equal(command.leaves[0].weight, "1");
  assert.throws(
    () =>
      validateEconomics({
        ...view,
        groups: [{ ...view.groups[0], known_provider_cost: 0.000000000003 }],
      }),
    /decimal strings/,
  );
});
