import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript");
function api() {
  const file = new URL("../src/features/agent/case-api.ts", import.meta.url);
  if (!existsSync(file)) return {};
  const js = ts.transpileModule(readFileSync(file, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText;
  const mod = { exports: {} };
  new Function("require", "module", "exports", js)(req, mod, mod.exports);
  return mod.exports.caseApi;
}
const source = {
  id: "message",
  direction: "incoming",
  source_state: "stored",
  content: { text_body: "🙂 The PO is wrong for both invoices." },
  invoice_links: [{ invoice_id: "i1" }, { invoice_id: "i2" }],
};
const invoice = { id: "i1", customer: { id: "customer" } };
const terms = {
  invoice,
  source,
  excerpt: "The PO is wrong for both invoices.",
  invoiceIds: ["i1", "i2"],
  issueType: "wrong_po",
  claimKey: "po-review",
  substance: "Documented purchase order mismatch",
};
test("current human case capture preserves reviewed multi-invoice scope, exact Unicode source and CSRF", async () => {
  const c = api();
  assert.equal(
    typeof c.create,
    "function",
    "Case creation must be usable through the application",
  );
  const old = globalThis.fetch;
  try {
    globalThis.fetch = async (path, options) => {
      assert.equal(path, "/api/workspaces/w/cases");
      assert.equal(options.method, "POST");
      assert.equal(options.headers["x-csrf-token"], "csrf");
      assert.equal(options.credentials, "same-origin");
      const body = JSON.parse(options.body);
      assert.deepEqual(body.invoice_ids, ["i1", "i2"]);
      assert.equal(body.customer_id, "customer");
      assert.deepEqual(body.evidence, {
        communication_id: "message",
        excerpt: terms.excerpt,
        offset: 2,
      });
      assert.equal(body.matching, "new");
      assert.equal(body.issue_type, "wrong_po");
      assert.equal(body.claim_key, "po-review");
      return {
        ok: true,
        json: async () => ({ case: { id: "case" }, created: true }),
      };
    };
    assert.equal((await c.create("w", terms, "csrf")).case.id, "case");
  } finally {
    globalThis.fetch = old;
  }
});
test("invented, outbound and unlinked case evidence is refused before network", async () => {
  const c = api();
  assert.equal(typeof c.create, "function");
  const old = globalThis.fetch;
  let calls = 0;
  try {
    globalThis.fetch = async () => {
      calls++;
      throw Error("unexpected network");
    };
    for (const change of [
      { excerpt: "invented" },
      { source: { ...source, direction: "outgoing" } },
      { invoiceIds: ["i1", "foreign"] },
      { invoiceIds: [] },
      { claimKey: "" },
    ])
      await assert.rejects(() =>
        c.create("w", { ...terms, ...change }, "csrf"),
      );
    assert.equal(calls, 0);
  } finally {
    globalThis.fetch = old;
  }
});
test("resolution and status commands bind the current case version without financial writes", async () => {
  const c = api();
  assert.equal(typeof c.resolveInvoice, "function");
  assert.equal(typeof c.transition, "function");
  const old = globalThis.fetch;
  const record = {
    id: "case",
    version: 7,
    invoice_links: [{ invoice_id: "i1" }],
  };
  const evidence = {
    communication_id: "message",
    excerpt: terms.excerpt,
    offset: 2,
  };
  const seen = [];
  try {
    globalThis.fetch = async (path, options) => {
      seen.push({ path, options, body: JSON.parse(options.body) });
      return { ok: true, json: async () => ({ ...record, version: 8 }) };
    };
    await c.resolveInvoice(
      "w",
      record,
      "i1",
      evidence,
      "documentary_confirmation",
      "PO confirmed",
      null,
      "csrf",
    );
    assert.deepEqual(seen[0].body, {
      case_id: "case",
      invoice_id: "i1",
      expected_version: 7,
      evidence,
      resolution_kind: "documentary_confirmation",
      resolution_action: "PO confirmed",
      resolution_reference_id: null,
    });
    await c.transition("w", record, "INVESTIGATING", "Review evidence", "csrf");
    assert.equal(seen[1].options.method, "PATCH");
    assert.deepEqual(seen[1].body, {
      case_id: "case",
      expected_version: 7,
      status: "INVESTIGATING",
      reason: "Review evidence",
    });
    await assert.rejects(() =>
      c.resolveInvoice(
        "w",
        record,
        "foreign",
        evidence,
        "documentary_confirmation",
        "bad",
        null,
        "csrf",
      ),
    );
    assert.equal(seen.length, 2);
  } finally {
    globalThis.fetch = old;
  }
});
test("specialist resolution captures reviewed reference without stale retry", async () => {
  const c = api();
  assert.equal(typeof c.dispositionReferences, "function");
  const old = globalThis.fetch;
  const reference = {
    id: "ref",
    version: 4,
    material_hash: "a".repeat(64),
    resolution_kind: "verified_contact_restriction",
  };
  const record = {
    id: "case",
    version: 7,
    invoice_links: [{ invoice_id: "i1" }],
  };
  let writes = 0;
  try {
    globalThis.fetch = async (path, options) => {
      if (options.method !== "POST") {
        assert.equal(
          path,
          "/api/workspaces/w/cases/case/disposition-references?invoice_id=i1",
        );
        assert.equal(options.cache, "no-store");
        return {
          ok: true,
          json: async () => ({
            case_id: "case",
            case_version: 7,
            invoice_id: "i1",
            items: [reference],
          }),
        };
      }
      writes++;
      const body = JSON.parse(options.body);
      assert.equal(body.expected_reference_version, 4);
      assert.equal(body.expected_reference_hash, reference.material_hash);
      assert.equal(body.resolution_reference_id, "ref");
      return {
        ok: false,
        status: 409,
        json: async () => ({ detail: "DISPOSITION_REFERENCE_CHANGED" }),
      };
    };
    const snapshot = await c.dispositionReferences("w", "case", "i1");
    await assert.rejects(
      () =>
        c.resolveInvoice(
          "w",
          record,
          "i1",
          { communication_id: "message", excerpt: "Stop", offset: 0 },
          reference.resolution_kind,
          "Restriction remains enforced",
          "ref",
          "csrf",
          snapshot.items[0],
        ),
      /Refresh and review/,
    );
    assert.equal(writes, 1);
  } finally {
    globalThis.fetch = old;
  }
});
