import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript");
function api(includeHelpers = false) {
  const js = ts.transpileModule(
    readFileSync(
      new URL("../src/features/agent/business-api.ts", import.meta.url),
      "utf8",
    ),
    { compilerOptions: { module: ts.ModuleKind.CommonJS } },
  ).outputText;
  const mod = { exports: {} };
  new Function("require", "module", "exports", js)(
    (name) => (name === "@/lib/api" ? { readApi: async () => {} } : req(name)),
    mod,
    mod.exports,
  );
  return includeHelpers ? mod.exports : mod.exports.promiseApi;
}
const source = {
  id: "message-1",
  direction: "incoming",
  source_state: "stored",
  content: { text_body: "I promise AUD 40.00 by 2026-12-01 UTC." },
  invoice_links: [{ invoice_id: "invoice-1" }],
};
const invoice = {
  id: "invoice-1",
  customer: { id: "customer-1" },
  original: { currency: "AUD", exponent: 2 },
};
const terms = {
  invoice,
  source,
  excerpt: source.content.text_body,
  decimal: "100000000000000.01",
  dueDate: "2026-12-01",
  timezone: "UTC",
};
test("capture sends actual customer/invoice/exact source span, decimal-string money as strict JSON integer and CSRF", async () => {
  const p = api();
  assert.equal(
    typeof p.capture,
    "function",
    "An authenticated user can capture a source-backed promise",
  );
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async (path, options) => {
      assert.equal(path, "/api/workspaces/workspace-1/promises");
      assert.equal(options.headers["x-csrf-token"], "csrf-1");
      assert.equal(options.credentials, "same-origin");
      assert.match(options.body, /"minor_units":10000000000000001[,}]/);
      assert.doesNotMatch(options.body, /"minor_units":"/);
      const data = JSON.parse(options.body);
      assert.equal(data.customer_id, "customer-1");
      assert.deepEqual(data.invoice_ids, ["invoice-1"]);
      assert.deepEqual(data.evidence, {
        communication_id: "message-1",
        excerpt: source.content.text_body,
        offset: 0,
      });
      return { ok: true, json: async () => ({ id: "promise-1" }) };
    };
    assert.equal(
      (await p.capture("workspace-1", terms, "csrf-1")).id,
      "promise-1",
    );
  } finally {
    globalThis.fetch = original;
  }
});
test("capture refuses invented source spans, unlinked invoices and lossy amounts before network", async () => {
  const p = api();
  assert.equal(typeof p.capture, "function");
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    throw Error("unexpected network");
  };
  try {
    for (const change of [
      { excerpt: "Invented promise" },
      { source: { ...source, direction: "outgoing" } },
      { invoice: { ...invoice, id: "unlinked" } },
      { decimal: "1e2" },
      { decimal: "0" },
      { decimal: "1.001" },
    ])
      await assert.rejects(() =>
        p.capture("w", { ...terms, ...change }, "csrf"),
      );
    assert.equal(calls, 0);
  } finally {
    globalThis.fetch = original;
  }
});
test("confirmation binds current version and original documentary evidence without any financial fields", async () => {
  const p = api();
  assert.equal(typeof p.confirm, "function");
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async (path, options) => {
      assert.equal(path, "/api/workspaces/w/promises/p-1/confirm");
      assert.deepEqual(JSON.parse(options.body), {
        promise_id: "p-1",
        expected_version: 3,
        confirmation_evidence: {
          communication_id: "message-1",
          excerpt: source.content.text_body,
          offset: 0,
        },
        reason: "I reviewed the exact source commitment.",
      });
      return { ok: true, json: async () => ({ id: "p-1", status: "ACTIVE" }) };
    };
    assert.equal(
      (
        await p.confirm(
          "w",
          {
            id: "p-1",
            version: 3,
            source_communication_id: "message-1",
            source_text: source.content.text_body,
            source_offset: 0,
          },
          "I reviewed the exact source commitment.",
          "csrf",
        )
      ).status,
      "ACTIVE",
    );
  } finally {
    globalThis.fetch = original;
  }
});

test("exact excerpt offsets count Unicode code points after non-BMP customer text", async () => {
  const p = api();
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async (_path, options) => {
      const command = JSON.parse(options.body);
      assert.equal(
        command.evidence.offset,
        2,
        "Canonical Python source offsets count the emoji once",
      );
      assert.equal(command.evidence.excerpt, source.content.text_body);
      return { ok: true, json: async () => ({ id: "unicode-promise" }) };
    };
    await p.capture(
      "w",
      {
        ...terms,
        source: {
          ...source,
          content: { text_body: `🙂 ${source.content.text_body}` },
        },
      },
      "csrf",
    );
  } finally {
    globalThis.fetch = original;
  }
});

test("multi-invoice capture requires exact explicit same-customer currency allocations and keeps all integers lossless", async () => {
  const p = api(),
    old = globalThis.fetch;
  const second = { ...invoice, id: "invoice-2" };
  const multi = {
    ...terms,
    source: {
      ...source,
      invoice_links: [{ invoice_id: invoice.id }, { invoice_id: second.id }],
    },
    invoices: [invoice, second],
    decimal: "200000000000000.03",
    allocations: [
      { invoice_id: invoice.id, decimal: "100000000000000.01" },
      { invoice_id: second.id, decimal: "100000000000000.02" },
    ],
  };
  try {
    globalThis.fetch = async (_path, options) => {
      assert.match(options.body, /"minor_units":20000000000000003/);
      assert.match(options.body, /"minor_units":10000000000000001/);
      assert.match(options.body, /"minor_units":10000000000000002/);
      const body = JSON.parse(options.body);
      assert.deepEqual(body.invoice_ids, ["invoice-1", "invoice-2"]);
      assert.equal(body.allocations.length, 2);
      return { ok: true, json: async () => ({ id: "multi" }) };
    };
    assert.equal((await p.capture("w", multi, "csrf")).id, "multi");
    let calls = 0;
    globalThis.fetch = async () => {
      calls++;
      throw Error("unexpected");
    };
    for (const change of [
      { allocations: [] },
      { allocations: [multi.allocations[0]] },
      { decimal: "200000000000000.04" },
      { invoices: [invoice, { ...second, customer: { id: "foreign" } }] },
      {
        invoices: [
          invoice,
          { ...second, original: { currency: "USD", exponent: 2 } },
        ],
      },
    ])
      await assert.rejects(() =>
        p.capture("w", { ...multi, ...change }, "csrf"),
      );
    assert.equal(calls, 0);
  } finally {
    globalThis.fetch = old;
  }
});

test("amendment capture and confirmation bind original ID, same invoice scope and reviewed prior version", async () => {
  const p = api(),
    old = globalThis.fetch;
  const prior = {
    id: "prior",
    version: 8,
    status: "ACTIVE",
    customer_id: invoice.customer.id,
    amount: { minor_units: "10000", currency: "AUD", exponent: 2 },
    invoice_links: [{ invoice_id: invoice.id }],
    source_communication_id: "original",
  };
  try {
    globalThis.fetch = async (path, options) => {
      const body = JSON.parse(options.body);
      if (path.endsWith("/confirm")) {
        assert.equal(body.replacement_expected_version, 8);
        assert.equal(body.expected_version, 2);
        assert.equal(body.confirmation_evidence.communication_id, "amended");
      } else {
        assert.equal(body.replaces_promise_id, "prior");
        assert.equal(body.evidence.communication_id, "amended");
      }
      return {
        ok: true,
        json: async () => ({ id: "replacement", status: "NEEDS_CONFIRMATION" }),
      };
    };
    const saved = await p.capture(
      "w",
      { ...terms, source: { ...source, id: "amended" }, replaces: prior },
      "csrf",
    );
    assert.equal(saved.status, "NEEDS_CONFIRMATION");
    const replacement = {
      ...prior,
      id: "replacement",
      version: 2,
      status: "NEEDS_CONFIRMATION",
      replaces_promise_id: "prior",
      source_communication_id: "amended",
      source_text: terms.excerpt,
      source_offset: 0,
    };
    await p.confirm(
      "w",
      replacement,
      "Reviewed both immutable commitment records.",
      "csrf",
      prior,
    );
    await assert.rejects(
      () => p.confirm("w", replacement, "Reviewed terms.", "csrf"),
      /prior|original|review/i,
    );
    await assert.rejects(
      () =>
        p.capture(
          "w",
          { ...terms, source: { ...source, id: "original" }, replaces: prior },
          "csrf",
        ),
      /new|original|amend/i,
    );
  } finally {
    globalThis.fetch = old;
  }
});

test("installment capture preserves exact row and invoice portions without double allocation or guessed shares", async () => {
  const p = api(),
    old = globalThis.fetch;
  const second = { ...invoice, id: "invoice-2" };
  const draft = {
    ...terms,
    decimal: "200000000000000.03",
    invoices: [invoice, second],
    source: {
      ...source,
      invoice_links: [{ invoice_id: invoice.id }, { invoice_id: second.id }],
    },
    allocations: [
      { invoice_id: invoice.id, decimal: "100000000000000.01" },
      { invoice_id: second.id, decimal: "100000000000000.02" },
    ],
    installments: [
      {
        dueDate: "2026-12-01",
        decimal: "100000000000000.01",
        allocations: [
          { invoice_id: invoice.id, decimal: "100000000000000.01" },
          { invoice_id: second.id, decimal: "0" },
        ],
      },
      {
        dueDate: "2026-12-08",
        decimal: "100000000000000.02",
        allocations: [
          { invoice_id: invoice.id, decimal: "0" },
          { invoice_id: second.id, decimal: "100000000000000.02" },
        ],
      },
    ],
  };
  try {
    globalThis.fetch = async (_path, options) => {
      const body = JSON.parse(options.body);
      assert.equal(
        Array.isArray(body.installments),
        true,
        "Explicit installment commands must be sent",
      );
      assert.equal(body.installments.length, 2);
      assert.deepEqual(
        body.installments.map((row) => row.due_date),
        ["2026-12-01", "2026-12-08"],
      );
      assert.equal(body.installments[0].allocations.length, 1);
      assert.equal(body.installments[0].allocations[0].invoice_id, invoice.id);
      assert.match(options.body, /"minor_units":10000000000000001/);
      assert.match(options.body, /"minor_units":10000000000000002/);
      return { ok: true, json: async () => ({ id: "installment-promise" }) };
    };
    await p.capture("w", draft, "csrf");
    let calls = 0;
    globalThis.fetch = async () => {
      calls++;
      throw Error("unexpected");
    };
    for (const installments of [
      [{ ...draft.installments[0], decimal: "1.00" }, draft.installments[1]],
      [{ ...draft.installments[0], allocations: [] }, draft.installments[1]],
      [
        { ...draft.installments[0], dueDate: "2026-02-30" },
        draft.installments[1],
      ],
      [
        {
          ...draft.installments[0],
          allocations: [
            { invoice_id: invoice.id, decimal: "100000000000000.00" },
            { invoice_id: second.id, decimal: "0.01" },
          ],
        },
        draft.installments[1],
      ],
    ])
      await assert.rejects(() =>
        p.capture("w", { ...draft, installments }, "csrf"),
      );
    assert.equal(calls, 0);
  } finally {
    globalThis.fetch = old;
  }
});

test("signed voice commitment review sends its exact immutable source/hash and current version, never a fake message", async () => {
  const p = api(),
    previous = globalThis.fetch,
    calls = [];
  const record = {
    id: "voice-promise",
    version: 7,
    source_communication_id: null,
    voice_source: { id: "voice-source", facts_hash: "a".repeat(64) },
    replaces_promise_id: null,
  };
  globalThis.fetch = async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    return Response.json(record);
  };
  try {
    await p.confirm(
      "w",
      record,
      "Reviewed exact unverified call facts",
      "csrf",
    );
    await p.cancel(
      "w",
      record,
      "Reviewed cancellation of exact call facts",
      "csrf",
    );
    for (const call of calls) {
      assert.equal(call.body.expected_version, 7);
      const evidence = call.body.confirmation_evidence ?? call.body.evidence;
      assert.deepEqual(evidence, {
        kind: "voice_operational",
        voice_evidence_id: "voice-source",
        facts_hash: "a".repeat(64),
      });
      assert.equal("communication_id" in evidence, false);
    }
  } finally {
    globalThis.fetch = previous;
  }
});

test("reviewed source and version do not silently adopt a refreshed hash", () => {
  const { samePromiseReview } = api(true);
  const current = {
    id: "promise-1",
    version: 7,
    source_communication_id: null,
    source_text: "Normalized facts",
    source_offset: 0,
    voice_source: { id: "voice-source", facts_hash: "a".repeat(64) },
  };
  const captured = structuredClone(current);
  assert.equal(samePromiseReview(captured, current), true);
  current.voice_source.facts_hash = "b".repeat(64);
  assert.equal(samePromiseReview(captured, current), false);
  assert.equal(captured.voice_source.facts_hash, "a".repeat(64));
  current.voice_source.facts_hash = captured.voice_source.facts_hash;
  current.version++;
  assert.equal(samePromiseReview(captured, current), false);
});

test("pending clarification preserves known terms and captures original version with new source", async () => {
  const { preparePromiseCapture } = api(true);
  const pending = {
    id: "pending",
    version: 3,
    status: "NEEDS_CONFIRMATION",
    customer_id: "customer-1",
    source_communication_id: "old-source",
    amount: { minor_units: "4000", currency: "AUD", exponent: 2 },
    due_date: null,
    timezone: null,
    invoice_links: [{ invoice_id: "invoice-1", amount: null }],
    installments: [],
  };
  const input = { ...terms, decimal: "40", replaces: pending };
  const prepared = preparePromiseCapture(input);
  const body = JSON.parse(prepared.body);
  assert.equal(body.replacement_expected_version, 3);
  assert.equal(body.replaces_promise_id, "pending");
  assert.equal(body.evidence.communication_id, "message-1");
  for (const change of [
    { decimal: "41" },
    { replaces: { ...pending, due_date: "2026-12-02" } },
    { replaces: { ...pending, timezone: "Australia/Melbourne" } },
    { replaces: { ...pending, invoice_links: [{ invoice_id: "other" }] } },
  ])
    assert.throws(
      () => preparePromiseCapture({ ...input, ...change }),
      /known|scope|clarif/i,
    );
  const unknown = preparePromiseCapture({
    ...input,
    replaces: { ...pending, invoice_links: [], amount: null },
  });
  assert.deepEqual(JSON.parse(unknown.body).invoice_ids, ["invoice-1"]);
});

test("confirmation binds reviewed pending lineage and active ancestor; stale ancestor never posts", async () => {
  const p = api(),
    original = globalThis.fetch;
  const old = { id: "pending", version: 2, status: "NEEDS_CONFIRMATION" };
  const active = { id: "active", version: 7, status: "ACTIVE" };
  const record = {
    id: "new",
    version: 1,
    replaces_promise_id: old.id,
    source_communication_id: "new-source",
    source_text: "Exact new clarification",
    source_offset: 0,
    replacement_lineage: [old, active],
  };
  let calls = 0;
  globalThis.fetch = async (_, options) => {
    calls++;
    const body = JSON.parse(options.body);
    assert.equal(body.replacement_expected_version, 2);
    assert.equal(body.active_predecessor_expected_version, 7);
    return { ok: true, json: async () => ({ ...record, status: "ACTIVE" }) };
  };
  try {
    await p.confirm("w", record, "Reviewed lineage", "csrf", old, [
      old,
      active,
    ]);
    await assert.rejects(
      () =>
        p.confirm("w", record, "Reviewed lineage", "csrf", old, [
          old,
          { ...active, version: 8 },
        ]),
      /lineage|ancestor/i,
    );
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = original;
  }
});
