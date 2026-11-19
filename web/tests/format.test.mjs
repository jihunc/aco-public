import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";

const require = createRequire(new URL("../package.json", import.meta.url));
const ts = require("typescript");
const module = { exports: {} };
new Function("module", "exports", ts.transpileModule(
  readFileSync(new URL("../src/lib/format.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
).outputText)(module, module.exports);
const { formatMinor, priorityLabel, dueLabel, formatDuration, shortInvoiceLabel } = module.exports;

test("minor units render as exact currency amounts", () => {
  assert.equal(formatMinor(10000, "AUD"), "AUD 100.00");
  assert.equal(formatMinor(0, "AUD"), "AUD 0.00");
  assert.equal(formatMinor(-500, "AUD"), "AUD -5.00");
  assert.equal(formatMinor(5, "AUD"), "AUD 0.05");
  assert.equal(formatMinor(123, "JPY", 0), "JPY 123");
  assert.equal(formatMinor(null, "AUD"), "Unavailable");
  assert.equal(formatMinor(undefined, "AUD"), "Unavailable");
});

test("priority ranks use the same words as the task form", () => {
  assert.deepEqual([0, 1, 2, 3].map(priorityLabel), ["Low", "Normal", "High", "Urgent"]);
  assert.equal(priorityLabel(7), "Priority 7");
});

test("a missing due date reads as no due date", () => {
  const stamp = (iso) => `stamped ${iso}`;
  assert.equal(dueLabel(null, stamp), "No due date");
  assert.equal(dueLabel(undefined, stamp), "No due date");
  assert.equal(dueLabel("2026-11-20T08:54:00Z", stamp), "stamped 2026-11-20T08:54:00Z");
});

test("durations read as minutes and seconds instead of raw fractional seconds", () => {
  assert.equal(formatDuration(147.387014), "2 min 27 s");
  assert.equal(formatDuration(42.4), "42 s");
  assert.equal(formatDuration(0), "0 s");
  assert.equal(formatDuration(3725), "1 h 2 min");
  assert.equal(formatDuration(null), "Insufficient data");
});

test("invoices are labelled by number when known, else by a short reference", () => {
  assert.equal(shortInvoiceLabel("0dbd6dff-868b-4c81-ada0-bb535ab09fcd", "INV-101"), "INV-101");
  assert.equal(shortInvoiceLabel("0dbd6dff-868b-4c81-ada0-bb535ab09fcd", null), "Invoice 0dbd6dff");
  assert.equal(shortInvoiceLabel("0dbd6dff-868b-4c81-ada0-bb535ab09fcd"), "Invoice 0dbd6dff");
});
