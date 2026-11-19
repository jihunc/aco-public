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
      new URL(`../src/features/receivables/${file}`, import.meta.url),
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
      : name === "./payment-api"
        ? load("payment-api.ts")
        : name === "@/lib/money"
          ? { formatMoney: (v) => `${v.currency} ${v.minor_units}` }
          : name === "./shared"
            ? { stamp: (v) => v, ErrorState: () => null }
            : name === "@/lib/api"
              ? {}
              : req(name);
  new Function("require", "module", "exports", output)(local, mod, mod.exports);
  return mod.exports;
}
const invoice = {
  id: "invoice-1",
  number: "DUPLICATE-DISPLAY",
  accounting_provider: "demo",
  verified: true,
  financial_status: "open",
  source: {
    synthetic: true,
    connection_id: "actual-connection",
    external_id: "native-invoice-77",
    revision: "r1",
  },
  outstanding: {
    minor_units: "10000000000000001",
    currency: "AUD",
    exponent: 2,
  },
};
test("decimal source payment conversion is exact beyond JavaScript safe integers and for zero/three decimal currencies", () => {
  const { paymentAmount } = load("payment-api.ts");
  assert.deepEqual(
    paymentAmount("100000000000000.01", invoice.outstanding),
    invoice.outstanding,
  );
  assert.deepEqual(
    paymentAmount("123.456", {
      minor_units: "123456",
      currency: "BHD",
      exponent: 3,
    }),
    { minor_units: "123456", currency: "BHD", exponent: 3 },
  );
  assert.equal(
    paymentAmount("12", { minor_units: "12", currency: "JPY", exponent: 0 })
      .minor_units,
    "12",
  );
  for (const amount of [
    "0",
    "-1",
    "1e2",
    "1,000.00",
    "0.001",
    "100000000000000.02",
  ])
    assert.throws(() => paymentAmount(amount, invoice.outstanding));
});
test("payment command binds actual source identity and retains operation UUID on an exact retry", () => {
  const { preparePayment } = load("payment-api.ts");
  let ids = 0;
  const first = preparePayment(
    null,
    invoice,
    "25.00",
    () => `operation-${++ids}`,
  );
  assert.equal(first.external_invoice_id, "native-invoice-77");
  assert.equal(first.provider_connection_id, "actual-connection");
  assert.equal(
    preparePayment(first, invoice, "25", () => `operation-${++ids}`)
      .operation_id,
    first.operation_id,
  );
  assert.notEqual(
    preparePayment(first, invoice, "26", () => `operation-${++ids}`)
      .operation_id,
    first.operation_id,
  );
  assert.equal(ids, 2);
});
test("payment request encodes the strict canonical Money integer without Number rounding or string coercion", async () => {
  const { paymentApi, preparePayment } = load("payment-api.ts");
  const prior = globalThis.fetch;
  const command = preparePayment(
    null,
    invoice,
    "100000000000000.01",
    () => "operation-1",
  );
  globalThis.fetch = async (path, init) => {
    assert.equal(
      path,
      "/api/workspaces/workspace-1/demo/accounting/simulate-payment",
    );
    assert.equal(init.credentials, "same-origin");
    assert.equal(init.headers["x-csrf-token"], "csrf");
    assert.match(init.body, /"minor_units":10000000000000001[,}]/);
    assert.doesNotMatch(init.body, /"minor_units":"/);
    return {
      ok: true,
      json: async () => ({ invoice_id: "invoice-1", synthetic: true }),
    };
  };
  try {
    assert.equal(
      (await paymentApi.simulate("workspace-1", command, "csrf")).invoice_id,
      "invoice-1",
    );
  } finally {
    globalThis.fetch = prior;
  }
});
test("payment outcome displays only committed reconciliation evidence and separate current source status", () => {
  const { PaymentResult } = load("payment.tsx");
  const html = renderToStaticMarkup(
    React.createElement(PaymentResult, {
      result: {
        event_id: "event-1",
        invoice_id: "invoice-1",
        financial_status: "paid",
        collection_lifecycle: "closed",
        source_revision: "r2",
        synthetic: true,
      },
      workspace: "w",
    }),
  );
  assert.match(html, /Synthetic source payment recorded/);
  assert.match(html, /event-1/);
  assert.match(html, /r2/);
  assert.match(html, /overview\?workspace=w/);
  assert.doesNotMatch(
    html,
    /case resolved|promise fulfilled|email sent|revenue uplift/i,
  );
});
test("current role permissions keep source-payment actions hidden from read-only members", () => {
  const { QueryClient, QueryClientProvider } = req("@tanstack/react-query");
  const { DemoPaymentPanel } = load("payment.tsx");
  function render(permissions) {
    return renderToStaticMarkup(
      React.createElement(
        QueryClientProvider,
        { client: new QueryClient() },
        React.createElement(DemoPaymentPanel, {
          workspace: { id: "w", is_demo: true, permissions },
          invoice,
          csrfToken: "csrf",
        }),
      ),
    );
  }
  assert.doesNotMatch(
    render([]),
    /Record synthetic source payment|Demo payment amount/,
  );
  assert.match(render([]), /An Owner or Finance manager/);
  assert.match(render(["demo_payment"]), /Demo payment amount/);
  assert.match(
    render(["demo_payment"]),
    /<button[^>]*disabled=""[^>]*>Record synthetic source payment/,
  );
});
test("invalid integer tokens cannot enter raw JSON and rejected payments are never retried automatically", async () => {
  const { paymentBody, paymentApi, SourcePaymentError } =
    load("payment-api.ts");
  assert.throws(() =>
    paymentBody({
      amount: {
        minor_units: '1,"currency":"USD"',
        currency: "AUD",
        exponent: 2,
      },
    }),
  );
  const prior = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return { ok: false, status: 422 };
  };
  try {
    await assert.rejects(
      paymentApi.simulate(
        "w",
        {
          provider_connection_id: "c",
          external_invoice_id: "i",
          operation_id: "op",
          amount: { minor_units: "1", currency: "AUD", exponent: 2 },
        },
        "csrf",
      ),
      (reason) => reason instanceof SourcePaymentError && reason.rejected,
    );
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = prior;
  }
});

test("invoice reminder history displays actual queue records without inventing cancellation counts", () => {
  const { QueryClient, QueryClientProvider } = req("@tanstack/react-query");
  const { InvoiceReminderHistory } = load("reminders.tsx");
  const client = new QueryClient();
  client.setQueryData(
    ["invoice-reminders", "w", "invoice-1"],
    [
      {
        id: "action-real",
        invoice_id: "invoice-1",
        version: 3,
        status: "cancelled",
        due_at: "2026-11-20T00:00:00Z",
        last_error: "VERIFIED_SOURCE_PAID",
      },
      {
        id: "other-tenant-invoice",
        invoice_id: "invoice-2",
        version: 1,
        status: "queued",
        due_at: "2026-11-20T00:00:00Z",
        last_error: null,
      },
    ],
  );
  const html = renderToStaticMarkup(
    React.createElement(
      QueryClientProvider,
      { client },
      React.createElement(InvoiceReminderHistory, {
        workspace: "w",
        invoiceId: "invoice-1",
      }),
    ),
  );
  assert.match(html, /action-real/);
  assert.match(html, /cancelled/);
  assert.match(html, /VERIFIED_SOURCE_PAID/);
  assert.doesNotMatch(
    html,
    /other-tenant-invoice|reminders cancelled|all reminders/i,
  );
});
