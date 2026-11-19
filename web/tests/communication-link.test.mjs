import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { libDouble } from "./lib-support.mjs";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript"),
  React = req("react"),
  { renderToStaticMarkup } = req("react-dom/server");
function load(file, mocks = {}) {
  const js = ts.transpileModule(
    readFileSync(
      new URL(`../src/features/communications/${file}`, import.meta.url),
      "utf8",
    ),
    {
      compilerOptions: {
        jsx: ts.JsxEmit.ReactJSX,
        module: ts.ModuleKind.CommonJS,
      },
    },
  ).outputText;
  const mod = { exports: {} };
  const local = (name) =>
    mocks[name] ??
    (name === "next/link"
      ? ({ children, ...props }) => React.createElement("a", props, children)
      : name === "@tanstack/react-query"
        ? { useQuery: () => ({}), useQueryClient: () => ({}) }
        : name === "@/features/receivables/shared"
          ? { stamp: (v) => v }
          : name === "./api"
            ? {}
            : req(name));
  new Function("require", "module", "exports", js)((n) => libDouble(n) ?? local(n), mod, mod.exports);
  return mod.exports;
}
const source = {
  id: "source",
  direction: "incoming",
  synthetic: true,
  event_at: "2026-11-19",
  delivery_state: "received",
  source_state: "stored",
  invoice_links: [],
  content: {
    subject: "Unlinked original",
    sender: "unverified@example.test",
    to: ["inbox@example.test"],
  },
};
test("an unlinked retained original has a discoverable review action in Conversations", () => {
  const { CommunicationRecord } = load("timeline.tsx");
  const html = renderToStaticMarkup(
    React.createElement(CommunicationRecord, { row: source, workspace: "w" }),
  );
  assert.match(html, /Review invoice links/);
  assert.match(html, /href="\/communications\/source\?workspace=w"/);
  assert.match(html, /sender remains unverified/);
});

test("captured command retains reviewed version/hash and exact Unicode source offset", () => {
  const { capturedLinkCommand } = load("link-api.ts", {
    "@/lib/api": {},
    "./api": {},
  });
  const review = {
    communication_id: "source",
    communication_version: 7,
    source_sha256: "a".repeat(64),
    can_link: true,
    candidates: [
      {
        invoice_id: "i",
        customer_id: "c",
        accounting_connection_id: "a",
        number: "INV-101",
        provenance: "explicit_reference",
      },
    ],
  };
  const original = {
    ...source,
    content: { ...source.content, text_body: "🙂 INV-101" },
  };
  const command = capturedLinkCommand(review, original, ["i"], "INV-101", 2);
  assert.equal(command.expected_version, 7);
  assert.equal(command.source_sha256, review.source_sha256);
  assert.deepEqual(command.evidence, {
    communication_id: "source",
    excerpt: "INV-101",
    offset: 2,
  });
  assert.throws(
    () => capturedLinkCommand(review, original, ["foreign"], "INV-101", 2),
    /reviewed customer/,
  );
  assert.throws(
    () => capturedLinkCommand(review, original, ["i"], "invented", 2),
    /original text/,
  );
  assert.throws(
    () => capturedLinkCommand(review, original, ["i"], "INV-101", 3),
    /original text/,
  );
  assert.throws(
    () =>
      capturedLinkCommand(
        { ...review, can_link: false },
        original,
        ["i"],
        "INV-101",
        2,
      ),
    /Current retained-source/,
  );
});

test("server refusal leaves review visible without a mutation control or sender verification claim", () => {
  let query = 0;
  const { CommunicationLinkPanel } = load("link-review.tsx", {
    "@/lib/api": {},
    "./link-api": {},
    "@tanstack/react-query": {
      useQueryClient: () => ({}),
      useQuery: () =>
        ++query === 1
          ? { data: { csrf_token: "csrf" } }
          : {
              data: {
                can_link: false,
                reasons: ["COMMUNICATION_CUSTOMER_AMBIGUOUS"],
                candidates: [],
              },
            },
    },
  });
  const html = renderToStaticMarkup(
    React.createElement(CommunicationLinkPanel, {
      source: { ...source, channel: "email" },
      workspace: "w",
    }),
  );
  assert.match(html, /COMMUNICATION_CUSTOMER_AMBIGUOUS/);
  assert.match(html, /Sender identity remains unverified/);
  assert.doesNotMatch(
    html,
    /Save reviewed invoice links|Review current references/,
  );
});
