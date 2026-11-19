import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url)),
  ts = req("typescript"),
  React = req("react");
const { renderToStaticMarkup } = req("react-dom/server");
function load(file) {
  const js = ts.transpileModule(
    readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8"),
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
    name === "next/navigation"
      ? {}
      : name === "@/lib/api"
        ? {}
        : name === "@/lib/money"
          ? load("lib/money.ts")
          : name === "@/features/communications/api"
            ? {}
            : name === "@/features/receivables/shared"
              ? {}
              : name === "./promise-installments"
                ? {}
                : name === "./business-api"
                  ? load("features/agent/business-api.ts")
                  : req(name);
  new Function("require", "module", "exports", js)(
    local,
    module,
    module.exports,
  );
  return module.exports;
}
test("captured promise review renders exact per-invoice amounts and preserves the active-original boundary", () => {
  const { PromiseCaptureReview } = load("features/agent/promise-capture.tsx");
  const html = renderToStaticMarkup(
    React.createElement(PromiseCaptureReview, {
      review: {
        amount: {
          minor_units: "10000000000000003",
          currency: "AUD",
          exponent: 2,
        },
        dueDate: "2026-12-03",
        timezone: "UTC",
        evidence: {
          communication_id: "source-new",
          excerpt: "Exact revised source statement.",
          offset: 2,
        },
        allocations: [
          {
            invoice_id: "i1",
            number: "INV-101",
            amount: {
              minor_units: "10000000000000001",
              currency: "AUD",
              exponent: 2,
            },
          },
          {
            invoice_id: "i2",
            number: "INV-107",
            amount: { minor_units: "2", currency: "AUD", exponent: 2 },
          },
        ],
        replaces: { id: "prior", version: 8 },
      },
    }),
  );
  assert.match(html, /AUD 100,000,000,000,000\.03/);
  assert.match(html, /AUD 100,000,000,000,000\.01/);
  assert.match(html, /AUD 0\.02/);
  assert.match(html, /INV-107/);
  assert.match(html, /reviewed version 8/);
  assert.match(html, /original remains active/);
  assert.match(html, /separate authorized confirmation/);
  assert.match(html, /not a payment or automatic confirmation/);
});

test("review shows each source-stated installment and distinguishes an explicit absent portion", () => {
  const { PromiseCaptureReview } = load("features/agent/promise-capture.tsx");
  const amount = (minor_units) => ({
    minor_units,
    currency: "AUD",
    exponent: 2,
  });
  const allocations = [
    {
      invoice_id: "i1",
      number: "INV-101",
      amount: amount("10000000000000001"),
    },
    { invoice_id: "i2", number: "INV-107", amount: amount("2") },
  ];
  const html = renderToStaticMarkup(
    React.createElement(PromiseCaptureReview, {
      review: {
        amount: amount("10000000000000003"),
        dueDate: "2026-12-03",
        timezone: "UTC",
        evidence: {
          communication_id: "stored-source",
          excerpt: "Exact source installments",
          offset: 0,
        },
        allocations,
        replaces: null,
        installments: [
          {
            dueDate: "2026-11-26",
            amount: amount("10000000000000001"),
            allocations: [allocations[0]],
          },
          {
            dueDate: "2026-12-03",
            amount: amount("2"),
            allocations: [allocations[1]],
          },
        ],
      },
    }),
  );
  assert.match(html, /Reviewed installment plan/);
  assert.match(html, /2026-11-26/);
  assert.match(html, /2026-12-03/);
  assert.match(html, /AUD 100,000,000,000,000\.01/);
  assert.match(html, /AUD 0\.02/);
  assert.equal((html.match(/No portion in this installment/g) ?? []).length, 2);
});

test("installment input fields require source amounts and dates without inferred defaults", () => {
  const { PromiseInstallmentInputs } = load(
    "features/agent/promise-installments.tsx",
  );
  const invoices = ["INV-101", "INV-107"].map((number, index) => ({
    id: `i${index + 1}`,
    number,
    original: { currency: "AUD", exponent: 2, minor_units: "10000" },
  }));
  const html = renderToStaticMarkup(
    React.createElement(PromiseInstallmentInputs, {
      invoices,
      rows: [{ key: "captured", dueDate: "", decimal: "", portions: {} }],
      onChange: () => {},
      disabled: true,
    }),
  );
  assert.match(html, /fieldset disabled/);
  assert.match(html, /Installment 1 date/);
  assert.match(html, /Installment 1 amount/);
  assert.match(html, /Installment 1 allocation for INV-101/);
  assert.match(html, /Installment 1 allocation for INV-107/);
  assert.equal((html.match(/value=""/g) ?? []).length, 4);
  assert.match(html, /never inferred or equally split/);
  assert.match(html, /0 means this installment has no portion/);
  assert.match(html, /Confirmation remains a separate reviewed action/);
});
