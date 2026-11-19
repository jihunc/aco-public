import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript"),
  React = req("react"),
  { renderToStaticMarkup } = req("react-dom/server");
function load(file) {
  const js = ts.transpileModule(
    readFileSync(
      new URL(`../src/features/agent/${file}`, import.meta.url),
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
    name === "./case-api"
      ? {
          caseApi: {
            dispositionReferences: () => {
              throw Error("Reference fetch requires explicit review");
            },
          },
        }
      : name === "./disposition-reference"
        ? load("disposition-reference.tsx")
        : name === "next/navigation"
          ? { useRouter: () => ({}) }
          : name === "@tanstack/react-query"
            ? { useQueryClient: () => ({}), useQuery: () => ({}) }
            : name === "@/features/communications/api"
              ? {}
              : name === "@/features/receivables/shared"
                ? { stamp: (v) => v }
                : req(name);
  new Function("require", "module", "exports", js)(local, mod, mod.exports);
  return mod.exports;
}
const { CaseControls } = load("case-commands.tsx");
const base = {
  id: "case",
  version: 4,
  status: "INVESTIGATING",
  invoice_links: [{ invoice_id: "i", issue_state: "OPEN" }],
  evidence: [
    {
      id: "source",
      communication_id: "comm",
      excerpt: "Original source",
      offset: 0,
    },
  ],
};
function render(issue, permissions = ["resolve", "task_write"]) {
  return renderToStaticMarkup(
    React.createElement(CaseControls, {
      workspace: { id: "w", permissions },
      record: { ...base, issue_type: issue },
      csrfToken: "csrf",
      onSaved: async () => {},
    }),
  );
}
test("specialist controls require explicit captured-source selection and retain restrictions", () => {
  for (const issue of ["refund", "contact_correction", "stop_contact"]) {
    const html = render(issue);
    assert.match(html, /Review current verified references/);
    assert.match(html, /Contact restrictions remain enforced/);
    assert.match(
      html,
      /<button type="submit" disabled="">Resolve selected invoice issue/,
    );
    assert.doesNotMatch(html, /Captured disposition source/);
  }
});
test("unsupported issues and nonresolvers do not receive specialist resolution authority", () => {
  assert.match(render("other"), /requires specialist review/);
  assert.doesNotMatch(
    render("stop_contact", ["task_write"]),
    /Review current verified references/,
  );
  assert.equal(render("refund", []), "");
});
test("retained voice source is selectable as issue origin without claiming documentary proof", () => {
  const html = renderToStaticMarkup(
    React.createElement(CaseControls, {
      workspace: { id: "w", permissions: ["resolve", "task_write"] },
      record: {
        ...base,
        issue_type: "stop_contact",
        evidence: [
          {
            id: "voice-origin",
            communication_id: null,
            voice_source: { id: "fact", facts_hash: "a".repeat(64) },
            excerpt: "Operational facts",
            offset: 0,
          },
        ],
      },
      csrfToken: "csrf",
      onSaved: async () => {},
    }),
  );
  assert.match(html, /value="voice-origin"/);
  assert.match(html, /Signed call issue origin/);
  assert.match(html, /independent verified disposition/);
});

test("voice payment claim keeps accounting allocation as the independent proof", () => {
  const html = renderToStaticMarkup(
    React.createElement(CaseControls, {
      workspace: { id: "w", permissions: ["resolve", "task_write"] },
      record: {
        ...base,
        issue_type: "payment_not_found",
        evidence: [
          {
            id: "voice-origin",
            communication_id: null,
            voice_source: { id: "fact", facts_hash: "a".repeat(64) },
            excerpt: "Operational payment claim",
            offset: 0,
          },
        ],
      },
      csrfToken: "csrf",
      onSaved: async () => {},
    }),
  );
  assert.match(html, /value="voice-origin"/);
  assert.match(html, /Verified accounting allocation reference/);
  assert.match(
    html,
    /Payment-not-found issues additionally require an actual unadjusted accounting allocation/,
  );
  assert.match(html, /call facts alone do not prove a payment/);
});
