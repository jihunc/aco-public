import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript"),
  React = req("react");
const { renderToStaticMarkup } = req("react-dom/server");
function load(file) {
  const path = new URL(`../src/features/onboarding/${file}`, import.meta.url);
  assert.ok(existsSync(path), "Connected demo reset UI/API is not implemented");
  const js = ts.transpileModule(readFileSync(path, "utf8"), {
    compilerOptions: {
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.CommonJS,
      esModuleInterop: true,
    },
  }).outputText;
  const mod = { exports: {} };
  const local = (name) =>
    name === "next/link"
      ? ({ href, children }) => React.createElement("a", { href }, children)
      : name === "@/features/receivables/shared"
        ? { stamp: (v) => v, ErrorState: () => null }
        : name === "./reset-api"
          ? load("reset-api.ts")
          : req(name);
  new Function("require", "module", "exports", js)(local, mod, mod.exports);
  return mod.exports;
}
const request = "f73b94aa-8451-4c3e-adfa-9bd427226220";
const invoice = "a480b232-645d-449e-8f73-722e0b537dd1";
const result = {
  request_id: request,
  previous_generation: 3,
  generation: 4,
  outbound_paused: true,
  automation_enabled: false,
  cancelled_action_ids: [],
  unknown_action_ids: [],
  preserved_uncertain_action_ids: [],
  cancelled_run_ids: [],
  released_reservation_ids: [],
  source_restoration: "RESTORED",
  restored_invoice_ids: [invoice],
  restored_customer_ids: ["431ff073-50ab-4f43-af67-050c31490c5f"],
  reversal_allocation_ids: [],
  source_event_ids: [],
};
const state = {
  generation: 3,
  outbound_paused: true,
  automation_enabled: false,
  can_reset: true,
  eligibility_reasons: [],
  last_reset: null,
  last_reset_at: null,
};
const owner = {
  id: "workspace",
  role: "owner",
  is_demo: true,
  permissions: ["demo_reset"],
};
const command = {
  request_id: request,
  expected_generation: 3,
  evidence: "Owner reviewed retained business history",
};

test("reset requires current Owner capability and CSRF, submits exactly captured generation/UUID, and retries unchanged", async () => {
  const { resetApi } = load("reset-api.ts");
  const old = globalThis.fetch,
    seen = [];
  try {
    globalThis.fetch = async (path, options) => {
      seen.push({ path, options });
      if (seen.length === 1) throw new TypeError("lost confirmation");
      return { ok: true, json: async () => result };
    };
    for (const workspace of [
      { ...owner, role: "finance_manager" },
      { ...owner, is_demo: false },
      { ...owner, permissions: [] },
    ]) {
      await assert.rejects(
        resetApi.submit(workspace, "csrf", command),
        /Owner|permission|demo/,
      );
    }
    await assert.rejects(resetApi.submit(owner, "", command), /CSRF|Sign in/i);
    assert.equal(seen.length, 0);
    await assert.rejects(
      resetApi.submit(owner, "csrf", command),
      /unconfirmed|same captured/i,
    );
    assert.deepEqual(await resetApi.submit(owner, "csrf", command), result);
    assert.deepEqual(JSON.parse(seen[0].options.body), command);
    assert.equal(seen[0].options.body, seen[1].options.body);
    assert.equal(seen[1].options.headers["x-csrf-token"], "csrf");
    assert.equal(seen[1].path, "/api/workspaces/workspace/demo/reset");
  } finally {
    globalThis.fetch = old;
  }
});
test("capture uses actual server generation and explicit retention review, refusing disabled or stale input", () => {
  const { captureReset, readResetReview } = load("reset-api.ts");
  assert.throws(
    () => captureReset({ ...state, can_reset: false }, command.evidence, true),
    /unavailable|eligible/i,
  );
  assert.throws(() => captureReset(state, command.evidence, false), /review/i);
  const captured = captureReset(state, command.evidence, true);
  assert.equal(captured.command.expected_generation, 3);
  assert.match(captured.command.request_id, /^[0-9a-f-]{36}$/);
  assert.equal(
    readResetReview(JSON.stringify(captured)).command.request_id,
    captured.command.request_id,
  );
  assert.equal(readResetReview('{"command":{"expected_generation":99}}'), null);
  assert.equal(readResetReview("invalid"), null);
});
test("actual persisted read and known conflicts remain distinct from source success", async () => {
  const { resetApi } = load("reset-api.ts");
  const old = globalThis.fetch;
  try {
    globalThis.fetch = async () => ({
      ok: true,
      json: async () => ({
        ...state,
        generation: 4,
        last_reset: result,
        last_reset_at: "2026-11-19T10:00:00Z",
      }),
    });
    assert.equal(
      (await resetApi.read("workspace")).last_reset.request_id,
      request,
    );
    for (const [detail, pattern] of [
      ["DEMO_GENERATION_CONFLICT", /refresh.*review/i],
      ["DEMO_RESET_SOURCE_REVIEW_REQUIRED", /history|source.*review/i],
    ]) {
      globalThis.fetch = async () => ({
        ok: false,
        status: 409,
        json: async () => ({ detail }),
      });
      await assert.rejects(resetApi.submit(owner, "csrf", command), pattern);
    }
    globalThis.fetch = async () => ({
      ok: true,
      json: async () => ({ ...result, request_id: invoice }),
    });
    await assert.rejects(
      resetApi.submit(owner, "csrf", command),
      /unconfirmed|confirmation/i,
    );
    globalThis.fetch = async () => ({
      ok: true,
      json: async () => ({ ...result, restored_invoice_ids: undefined }),
    });
    await assert.rejects(
      resetApi.submit(owner, "csrf", command),
      /unconfirmed|confirmation/i,
    );
  } finally {
    globalThis.fetch = old;
  }
});
test("saved reset renders actual source counts, retained holds/uncertainty and links without invented resolutions", () => {
  const { ResetSummary } = load("demo-reset.tsx");
  const html = renderToStaticMarkup(
    React.createElement(ResetSummary, { result, workspace: "workspace" }),
  );
  assert.match(html, /1 seed invoice/);
  assert.match(html, /1 seed customer/);
  assert.match(html, /cases|Cases/);
  assert.match(html, /holds/);
  assert.match(html, /uncertain|Uncertain/);
  assert.match(html, /receivables\?workspace=workspace/);
  assert.match(html, new RegExp(request));
  assert.doesNotMatch(
    html,
    /cases resolved|promises fulfilled|real payment|ROI|all history cleared/i,
  );
  const pending = renderToStaticMarkup(
    React.createElement(ResetSummary, {
      result: {
        ...result,
        source_restoration: "PENDING",
        restored_invoice_ids: [],
        restored_customer_ids: [],
      },
      workspace: "workspace",
    }),
  );
  assert.match(pending, /restoration.*pending/i);
  assert.doesNotMatch(pending, /seed invoice.*restored/i);
});
