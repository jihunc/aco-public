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
        : name === "@/lib/money"
          ? load("../../lib/money.ts")
          : name === "./cohort-api"
            ? load("cohort-api.ts")
            : name === "./observed-cohorts"
              ? { ObservedCohortPanel: () => null }
              : req(name);
  new Function("require", "module", "exports", source)(
    local,
    module,
    module.exports,
  );
  return module.exports;
}
const result = {
  id: "result-before",
  version: 1,
  prior_result_id: null,
  label: "BASELINE_SYNTHETIC",
  status: "COMPLETE",
  as_of_at: "2026-11-01T00:00:00Z",
  last_synced_at: "2026-11-18T00:00:00Z",
  source_watermark: "watermark-1",
  source_status: "VERIFIED_AT_AS_OF",
  freshness: "FROZEN_SNAPSHOT",
  reason: "Initial frozen source.",
  eligible_count: 4,
  excluded_count: 0,
  opening_minor: "40000",
  raw_net_minor: "24000",
  recovery_numerator_minor: "24000",
  refunds_minor: "0",
  reversals_minor: "0",
  recovery_percent: "60",
  paid_in_full_count: 2,
  paid_in_full_percent: "50",
  days_to_collect: "21.25",
  days_beyond_terms: "-8.75",
  issues: [],
  adjustment_ids: [],
  observation_ids: ["observation-1"],
};
const cohort = {
  id: "cohort-before",
  side: "BASELINE_SYNTHETIC",
  period_start: "2026-08-01",
  period_end: "2026-08-05",
  timezone: "Australia/Melbourne",
  currency: "AUD",
  exponent: 2,
  credit_terms_days: 30,
  business_segment: "fixed synthetic SME",
  algorithm: "aco_cash_v1",
  precision: "date_only_local_midnight",
  members: [
    {
      invoice_id: "invoice-real",
      original_due_date: "2026-08-01",
      issue_date: "2026-07-02",
      opening_minor: "10000000000000001",
      original_minor: "10000000000000001",
      source_watermark: "member-watermark",
      opening_source_id: "opening-original",
      eligibility: "ELIGIBLE",
    },
  ],
  results: [result],
};
const pair = {
  id: "pair-1",
  source_connection_id: "source-1",
  synthetic: true,
  label: "Illustrative — Synthetic Data",
  comparison_allowed: true,
  unmatched_strata: [],
  recovery_change_pp: "15",
  recovery_relative_change_percent: "25",
  paid_in_full_change_pp: "25",
  cohorts: [
    cohort,
    {
      ...cohort,
      id: "cohort-after",
      side: "AFTER_SYNTHETIC",
      results: [
        {
          ...result,
          id: "result-after",
          label: "AFTER_SYNTHETIC",
          recovery_percent: "75",
          paid_in_full_count: 3,
          paid_in_full_percent: "75",
        },
      ],
    },
  ],
  caveat:
    "Fixed synthetic illustration. Differences do not establish that ACO caused better payment outcomes.",
};

test("frozen cohort results expose exact money, source/time/strata and honest absolute versus relative changes", () => {
  const { CohortPair } = load("cohorts.tsx");
  const html = renderToStaticMarkup(
    React.createElement(CohortPair, { pair, workspace: "w" }),
  );
  assert.match(html, /Illustrative — Synthetic Data/);
  assert.match(html, /Frozen source snapshot/);
  assert.match(html, /2026-11-01T00:00:00Z/);
  assert.match(html, /AUD 100,000,000,000,000.01/);
  assert.match(html, /15 percentage points/);
  assert.match(html, /25% relative/);
  assert.match(html, /fixed synthetic SME/);
  assert.match(html, /30-day/);
  assert.match(html, /receivables\/invoice-real\?workspace=w/);
  assert.match(html, /opening-original/);
  assert.match(html, /observation-1/);
  assert.doesNotMatch(html, /achieved|proven uplift|DSO reduced/);
});

test("incomplete or unmatched cohorts never render comparison deltas or null metrics as zero", () => {
  const { CohortPair } = load("cohorts.tsx");
  const html = renderToStaticMarkup(
    React.createElement(CohortPair, {
      workspace: "w",
      pair: {
        ...pair,
        comparison_allowed: false,
        unmatched_strata: ["different terms"],
        cohorts: [
          {
            ...cohort,
            results: [
              {
                ...result,
                status: "INCOMPLETE",
                label: "INCOMPLETE",
                recovery_percent: null,
                paid_in_full_percent: null,
                paid_in_full_count: null,
                raw_net_minor: null,
                recovery_numerator_minor: null,
                days_to_collect: null,
                days_beyond_terms: null,
                issues: ["missing opening evidence"],
              },
            ],
          },
        ],
      },
    }),
  );
  assert.match(html, /Comparison unavailable/);
  assert.match(html, /missing opening evidence/);
  assert.match(html, /different terms/);
  assert.match(html, /Insufficient data/);
  assert.doesNotMatch(html, /15 percentage points|25% relative|0%/);
});

test("refresh capture retains both exact reviewed result IDs and does not mutate when new results arrive", () => {
  const { captureCohortReview } = load("cohort-api.ts");
  const captured = captureCohortReview(
    "refresh",
    "Owner reviewed both frozen source results.",
    "request-1",
    pair,
  );
  assert.deepEqual(captured.expected_result_ids, [
    "result-before",
    "result-after",
  ]);
  pair.cohorts[0].results.push({
    ...result,
    id: "new-result",
    version: 2,
    prior_result_id: "result-before",
    label: "RESTATED",
  });
  assert.deepEqual(captured.expected_result_ids, [
    "result-before",
    "result-after",
  ]);
  pair.cohorts[0].results.pop();
});

test("cohort writes send exact captured preconditions, same-origin CSRF and retain meaningful conflicts", async () => {
  const { cohortApi, captureCohortReview } = load("cohort-api.ts");
  const command = captureCohortReview(
    "refresh",
    "Owner reviewed both frozen source results.",
    "request-1",
    pair,
  );
  const old = globalThis.fetch;
  let body;
  globalThis.fetch = async (path, init) => {
    assert.equal(path, "/api/workspaces/w/impact/cohorts/pair-1/refresh");
    assert.equal(init.headers["x-csrf-token"], "csrf");
    assert.equal(init.credentials, "same-origin");
    body = JSON.parse(init.body);
    return {
      ok: false,
      status: 409,
      json: async () => ({ detail: "COHORT_REVIEWED_RESULT_CHANGED" }),
    };
  };
  try {
    await assert.rejects(
      cohortApi.submit("w", command, "csrf"),
      /changed.*review|review.*changed/i,
    );
    assert.deepEqual(body, {
      request_id: "request-1",
      evidence: command.evidence,
      expected_result_ids: ["result-before", "result-after"],
    });
  } finally {
    globalThis.fetch = old;
  }
});

test("cohort API rejects rounded numeric source money and Decimal percentages", async () => {
  const { cohortApi } = load("cohort-api.ts");
  const old = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => ({
      tenant_id: "w",
      can_seed: false,
      can_refresh: true,
      pairs: [
        {
          ...pair,
          cohorts: [
            {
              ...cohort,
              results: [
                { ...result, opening_minor: Number("10000000000000001") },
              ],
            },
          ],
        },
      ],
    }),
  });
  try {
    await assert.rejects(cohortApi.read("w"), /exact|precision/i);
    globalThis.fetch = async () => ({
      ok: true,
      json: async () => ({
        tenant_id: "w",
        can_seed: false,
        can_refresh: true,
        pairs: [{ ...pair, recovery_change_pp: 15 }],
      }),
    });
    await assert.rejects(cohortApi.read("w"), /exact|precision/i);
  } finally {
    globalThis.fetch = old;
  }
});

test("stored captured review requires matching exact result IDs and known sides", () => {
  const { captureCohortReview, readPendingCohortReview } =
    load("cohort-api.ts");
  const review = captureCohortReview(
    "refresh",
    "Owner reviewed both result snapshots.",
    "request-1",
    pair,
  );
  const saved = { review, phase: "uncertain" };
  assert.deepEqual(readPendingCohortReview(JSON.stringify(saved)), saved);
  assert.equal(
    readPendingCohortReview(
      JSON.stringify({
        ...saved,
        review: { ...review, expected_result_ids: ["swapped", "result-after"] },
      }),
    ),
    null,
  );
  assert.equal(
    readPendingCohortReview(
      JSON.stringify({
        ...saved,
        review: {
          ...review,
          reviewed: [review.reviewed[0], review.reviewed[0]],
        },
      }),
    ),
    null,
  );
  assert.equal(readPendingCohortReview("invalid-json"), null);
});
