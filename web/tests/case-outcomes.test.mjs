import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { libDouble } from "./lib-support.mjs";
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
        : name === "./case-outcomes-api"
          ? load("case-outcomes-api.ts")
          : req(name);
  new Function("require", "module", "exports", source)(
    (n) => libDouble(n) ?? local(n),
    module,
    module.exports,
  );
  return module.exports;
}
const group = {
  synthetic: true,
  eligible_count: 3,
  resolved_count: 1,
  autonomous_resolved_count: 0,
  human_intervention_count: 2,
  open_backlog_count: 1,
  administrative_closed_count: 1,
  unknown_count: 0,
  autonomous_resolution_percent: "0",
  human_intervention_percent: "66.66666666666666666666666667",
  mean_resolution_seconds: "9007199254740993.125",
  mean_open_age_seconds: "30.125",
};
const row = {
  case_id: "case-real",
  synthetic: true,
  opened_at: "2026-11-01T00:00:00Z",
  status: "INVESTIGATING",
  closure_reason: null,
  resolved_at: null,
  eligible_resolved: false,
  autonomous: false,
  substantive_human: true,
  open_backlog: true,
  administrative_closed: false,
  completeness: "COMPLETE",
  issues: [],
  resolution_seconds: null,
  backlog_age_seconds: "30.125",
  resolved_episode_count: 1,
  history_ids: ["history-original", "history-reopened"],
  resolution_evidence_ids: ["case-evidence-real"],
};
const view = {
  ...group,
  tenant_id: "w",
  since: "2026-11-01T00:00:00Z",
  until: "2026-11-19T00:00:00Z",
  as_of_at: "2026-11-19T01:00:00Z",
  generated_at: "2026-11-19T01:00:01Z",
  algorithm: "case_history_v1",
  source: "RECORDED_CASE_HISTORY",
  reported_human_minutes: null,
  human_time_reason:
    "No recorded handling-time inputs; activity is not measured duration.",
  case_approval_count: null,
  approval_reason:
    "Approval decisions have no verified case binding; invoice overlap is not attribution.",
  cases: [row],
  groups: [group],
  mixed_sources: false,
  caveat:
    "Recorded issue outcomes do not establish that ACO caused collection or labour savings.",
};

test("recorded case groups show exact timing, actual denominator and no invented handling time or cost", () => {
  const { CaseOutcomeReport } = load("case-outcomes.tsx");
  const html = renderToStaticMarkup(
    React.createElement(CaseOutcomeReport, { view, workspace: "w" }),
  );
  assert.match(html, /Synthetic case history/);
  assert.match(html, /9007199254740993\.125 seconds/);
  assert.match(html, /66\.66666666666666666666666667%/);
  assert.match(html, /0 of 1 eligible resolved/);
  assert.match(html, /2 of 3 eligible cases/);
  assert.match(html, /activity is not measured duration/);
  assert.match(html, /invoice overlap is not attribution/);
  assert.match(html, /2026-11-19T01:00:00Z/);
  assert.match(html, /do not establish that ACO caused/);
  assert.doesNotMatch(html, /0 minutes|saved labour|achieved ROI/);
});

test("mixed provenance and incomplete histories do not imply a blended outcome rate or unknown zero", () => {
  const { CaseOutcomeReport } = load("case-outcomes.tsx");
  const html = renderToStaticMarkup(
    React.createElement(CaseOutcomeReport, {
      workspace: "w",
      view: {
        ...view,
        mixed_sources: true,
        autonomous_resolution_percent: "99",
        groups: [
          group,
          {
            ...group,
            synthetic: false,
            unknown_count: 1,
            autonomous_resolution_percent: null,
            human_intervention_percent: null,
            mean_resolution_seconds: null,
            mean_open_age_seconds: null,
          },
        ],
        cases: [
          {
            ...row,
            completeness: "INCOMPLETE",
            issues: ["reopen_scope_unknown"],
          },
        ],
      },
    }),
  );
  assert.match(html, /Observed case history/);
  assert.match(html, /No blended outcome rate/);
  assert.match(html, /Insufficient data/);
  assert.match(html, /reopen_scope_unknown/);
  assert.doesNotMatch(html, /99%/);
});

test("reopened and administrative rows retain real history and case evidence links without a resolved claim", () => {
  const { CaseOutcomeRecord } = load("case-outcomes.tsx");
  const html = renderToStaticMarkup(
    React.createElement(CaseOutcomeRecord, {
      record: row,
      workspace: "tenant id",
    }),
  );
  assert.match(html, /cases\/case-real\?workspace=tenant%20id/);
  assert.match(html, /INVESTIGATING/);
  assert.match(html, /1 recorded resolved episode/);
  assert.match(html, /history-reopened/);
  assert.match(html, /case-evidence-real/);
  assert.match(html, /Not currently eligible as resolved/);
  assert.doesNotMatch(html, /Autonomously resolved/);
  const admin = renderToStaticMarkup(
    React.createElement(CaseOutcomeRecord, {
      record: {
        ...row,
        status: "CLOSED",
        closure_reason: "withdrawn",
        open_backlog: false,
        administrative_closed: true,
      },
      workspace: "w",
    }),
  );
  assert.match(admin, /Administrative closure/);
  assert.match(admin, /withdrawn/);
});

test("case outcome API reads the exact aware reporting window and as-of, and rejects numeric Decimal fields", async () => {
  const { caseOutcomesApi } = load("case-outcomes-api.ts");
  const previous = globalThis.fetch;
  let numeric = false;
  globalThis.fetch = async (url, init) => {
    const parsed = new URL(url, "http://localhost");
    assert.equal(parsed.pathname, "/api/workspaces/w/impact/case-outcomes");
    assert.equal(parsed.searchParams.get("since"), view.since);
    assert.equal(parsed.searchParams.get("until"), view.until);
    assert.equal(parsed.searchParams.get("as_of"), view.as_of_at);
    assert.equal(init.credentials, "same-origin");
    return {
      ok: true,
      json: async () =>
        numeric
          ? {
              ...view,
              groups: [
                {
                  ...group,
                  mean_resolution_seconds: Number("9007199254740993"),
                },
              ],
            }
          : view,
    };
  };
  try {
    assert.deepEqual(
      await caseOutcomesApi.read("w", {
        since: view.since,
        until: view.until,
        as_of: view.as_of_at,
      }),
      view,
    );
    numeric = true;
    await assert.rejects(
      caseOutcomesApi.read("w", {
        since: view.since,
        until: view.until,
        as_of: view.as_of_at,
      }),
      /precision|decimal strings/i,
    );
  } finally {
    globalThis.fetch = previous;
  }
});

test("empty case population is explicitly insufficient rather than an invented successful zero rate", () => {
  const { CaseOutcomeReport } = load("case-outcomes.tsx");
  const html = renderToStaticMarkup(
    React.createElement(CaseOutcomeReport, {
      workspace: "w",
      view: {
        ...view,
        cases: [],
        groups: [],
        eligible_count: 0,
        autonomous_resolution_percent: null,
        human_intervention_percent: null,
      },
    }),
  );
  assert.match(html, /No recorded cases opened in this window/);
  assert.match(html, /unavailable for an empty population/);
  assert.doesNotMatch(html, /0%|0 minutes|0 seconds/);
});

test("case outcome current-membership and invalid-window errors do not masquerade as empty data", async () => {
  const { caseOutcomesApi } = load("case-outcomes-api.ts");
  const previous = globalThis.fetch;
  try {
    globalThis.fetch = async () => ({ ok: false, status: 403 });
    await assert.rejects(
      caseOutcomesApi.read("w", { since: view.since, until: view.until }),
      /current workspace membership/,
    );
    globalThis.fetch = async () => ({ ok: false, status: 422 });
    await assert.rejects(
      caseOutcomesApi.read("w", { since: view.since, until: view.until }),
      /Narrow.*window|as-of/,
    );
  } finally {
    globalThis.fetch = previous;
  }
});

test("manual reported subtotal remains exact and partial with missing events and real entry references", () => {
  const { CaseOutcomeReport } = load("case-outcomes.tsx");
  const handling = {
    known_reported_human_minutes: "9007199254740993.125",
    missing_handling_event_count: 2,
    reported_human_minutes: null,
  };
  const html = renderToStaticMarkup(
    React.createElement(CaseOutcomeReport, {
      workspace: "w",
      view: {
        ...view,
        ...handling,
        groups: [{ ...group, ...handling }],
        cases: [
          { ...row, ...handling, handling_entry_ids: ["handling-entry-real"] },
        ],
      },
    }),
  );
  assert.match(html, /9007199254740993\.125 minutes/);
  assert.match(html, /MANUAL_REPORTED/);
  assert.match(html, /2 substantive handling events without time entries/);
  assert.match(html, /handling-entry-real/);
  assert.match(html, /Known subtotal does not cover missing time/);
  assert.doesNotMatch(html, /measured human minutes|time saved:|ROI:/i);
});

test("manual time precision validation covers source-group and per-case subtotals", async () => {
  const { caseOutcomesApi } = load("case-outcomes-api.ts");
  const previous = globalThis.fetch;
  try {
    globalThis.fetch = async () => ({
      ok: true,
      json: async () => ({
        ...view,
        groups: [
          {
            ...group,
            known_reported_human_minutes: Number("9007199254740993.125"),
          },
        ],
      }),
    });
    await assert.rejects(
      caseOutcomesApi.read("w", { since: view.since, until: view.until }),
      /precision|decimal strings/i,
    );
    globalThis.fetch = async () => ({
      ok: true,
      json: async () => ({
        ...view,
        cases: [
          {
            ...row,
            known_reported_human_minutes: Number("9007199254740993.125"),
          },
        ],
      }),
    });
    await assert.rejects(
      caseOutcomesApi.read("w", { since: view.since, until: view.until }),
      /precision|decimal strings/i,
    );
  } finally {
    globalThis.fetch = previous;
  }
});

test("case approval drilldown links only exact returned requests and retains original decision references", () => {
  const { CaseOutcomeRecord } = load("case-outcomes.tsx");
  const html = renderToStaticMarkup(
    React.createElement(CaseOutcomeRecord, {
      record: {
        ...row,
        approval_request_ids: ["request-bound"],
        approval_decision_ids: ["decision-bound"],
      },
      workspace: "w",
    }),
  );
  assert.match(html, /review\?workspace=w&amp;approval=request-bound/);
  assert.match(html, /decision-bound/);
  assert.doesNotMatch(html, /autonomously resolved/i);
});
