import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript");
function load() {
  const js = ts.transpileModule(
    readFileSync(
      new URL("../src/features/impact/observed-api.ts", import.meta.url),
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
  new Function("require", "module", "exports", js)(req, module, module.exports);
  return module.exports;
}
const preview = {
  preview_only: true,
  selection: {
    connection_id: "connection",
    period_start: "2026-01-01",
    period_end: "2026-02-01",
    currency: "AUD",
    credit_terms_days: 30,
    as_of_at: "2026-05-01T00:00:00Z",
  },
  reviewed_hash: "a".repeat(64),
  synthetic: true,
  timezone: "Australia/Melbourne",
  business_segments: [null],
  members: [
    {
      invoice_id: "invoice",
      opening_minor: null,
      original_minor: "10000000000000001",
      source_revision_ids: ["source"],
      original_due_date: "2026-01-01",
      current_due_date: "2026-01-05",
      source_change: "Retained original date",
      eligibility: "INCOMPLETE",
    },
  ],
  result: {
    opening_minor: null,
    raw_net_minor: null,
    recovery_numerator_minor: null,
    refunds_minor: "0",
    reversals_minor: "0",
    recovery_percent: null,
    paid_in_full_percent: null,
    days_to_collect: null,
    days_beyond_terms: null,
    status: "INCOMPLETE",
  },
  warnings: ["Missing original history."],
};
const current = {
  can_build: true,
  sources: [{ connection_id: "connection" }],
  cohorts: [],
};
test("observed review deep captures exact source and as-of without relabeling synthetic evidence", () => {
  const api = load();
  const review = api.captureObservedReview(
    preview,
    "Reviewed actual retained source evidence.",
    "request",
  );
  preview.selection.currency = "USD";
  assert.equal(review.command.selection.currency, "AUD");
  assert.equal(review.preview.synthetic, true);
  assert.equal(api.canSubmitObserved(review, current, true), true);
  assert.equal(
    api.canSubmitObserved(review, { ...current, can_build: false }, true),
    false,
  );
  assert.equal(api.canSubmitObserved(review, current, false), false);
  preview.selection.currency = "AUD";
});
test("current definition/result changes cannot silently replace captured restatement", () => {
  const api = load();
  const parent = {
    id: "cohort",
    versions: [{ id: "definition", results: [{ id: "result" }] }],
  };
  const review = api.captureObservedReview(
    preview,
    "Reviewed membership restatement.",
    "request",
    parent,
    "RESTATE",
  );
  const list = { ...current, cohorts: [parent] };
  assert.equal(api.canSubmitObserved(review, list, true), true);
  parent.versions[0].results.push({ id: "new-result" });
  assert.equal(api.canSubmitObserved(review, list, true), false);
  assert.equal(review.command.expected_result_id, "result");
});
test("observed exact strings refuse numeric money and preserve original source review across storage", () => {
  const api = load();
  assert.equal(
    api.validateObservedPreview(preview).members[0].original_minor,
    "10000000000000001",
  );
  assert.throws(
    () =>
      api.validateObservedPreview({
        ...preview,
        members: [{ ...preview.members[0], original_minor: 10000000000000000 }],
      }),
    /exact/i,
  );
  const review = api.captureObservedReview(
    preview,
    "Reviewed unknown opening remains unknown.",
    "request",
  );
  assert.deepEqual(
    api.readObservedReview(JSON.stringify({ review, phase: "captured" }))
      .review,
    review,
  );
  assert.equal(api.readObservedReview("not json"), null);
});
test("stale observed command sends captured hash once with CSRF and no automatic retry", async () => {
  const api = load();
  const old = globalThis.fetch;
  let calls = 0;
  const review = api.captureObservedReview(
    preview,
    "Explicit captured source review.",
    "request",
  );
  globalThis.fetch = async (url, options) => {
    calls++;
    assert.match(url, /\/observed$/);
    assert.equal(options.headers["x-csrf-token"], "csrf");
    assert.equal(
      JSON.parse(options.body).reviewed_hash,
      review.command.reviewed_hash,
    );
    return { ok: false, status: 409 };
  };
  try {
    await assert.rejects(
      api.observedApi.submit("workspace", review, "csrf"),
      /changed/i,
    );
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = old;
  }
});

function renderModule() {
  const React = req("react");
  const js = ts.transpileModule(
    readFileSync(
      new URL("../src/features/impact/observed-cohorts.tsx", import.meta.url),
      "utf8",
    ),
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.ReactJSX,
        esModuleInterop: true,
      },
    },
  ).outputText;
  const module = { exports: {} };
  const local = (name) =>
    name === "next/link"
      ? ({ href, children }) => React.createElement("a", { href }, children)
      : name === "@/features/receivables/shared"
        ? { stamp: (v) => v, ErrorState: () => null }
        : name === "./observed-api"
          ? load()
          : req(name);
  new Function("require", "module", "exports", js)(
    local,
    module,
    module.exports,
  );
  return module.exports;
}
test("observed rendering keeps unknown original exposure, exact large values and inert provenance", () => {
  const React = req("react"),
    { renderToStaticMarkup } = req("react-dom/server");
  const { ObservedRecord } = renderModule();
  const result = {
    ...preview.result,
    id: "result",
    version: 1,
    label: "INCOMPLETE",
    as_of_at: "2026-05-01T00:00:00Z",
    source_watermark: "actual-watermark",
    eligible_count: 0,
    excluded_count: 1,
    issues: ["Incomplete opening history"],
    adjustment_ids: [],
    observation_ids: ["actual-observation"],
  };
  const cohort = {
    id: "group",
    source_connection_id: "connection",
    execution_basis: "OBSERVED",
    synthetic: true,
    label: "Observed execution — Synthetic accounting source",
    caveat: "No causal comparison",
    versions: [
      {
        id: "definition",
        version: 1,
        prior_definition_id: null,
        reason: "<script>unsafe()</script>",
        selection: preview.selection,
        timezone: preview.timezone,
        members: preview.members,
        current_source_members: preview.members,
        results: [result],
        warnings: preview.warnings,
      },
    ],
  };
  const html = renderToStaticMarkup(
    React.createElement(ObservedRecord, { cohort, workspace: "workspace" }),
  );
  assert.match(html, /Synthetic financial source/);
  assert.match(html, /OBSERVED/);
  assert.match(html, /Insufficient data/);
  assert.match(html, /10000000000000001/);
  assert.match(html, /actual-observation/);
  assert.match(html, /receivables\/invoice\?workspace=workspace/);
  assert.match(html, /&lt;script&gt;unsafe/);
  assert.doesNotMatch(html, /<script>|0%|achieved ROI/);
});
