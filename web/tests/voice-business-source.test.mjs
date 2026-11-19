import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript"),
  React = req("react");
const { renderToStaticMarkup } = req("react-dom/server");
function component() {
  const file = new URL(
    "../src/features/conversations/voice-business-source.tsx",
    import.meta.url,
  );
  const output = ts.transpileModule(readFileSync(file, "utf8"), {
    compilerOptions: {
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.CommonJS,
      esModuleInterop: true,
    },
  }).outputText;
  const mod = { exports: {} };
  new Function("require", "module", "exports", output)(
    (name) =>
      name === "next/link"
        ? ({ href, children }) => React.createElement("a", { href }, children)
        : req(name),
    mod,
    mod.exports,
  );
  return mod.exports.VoiceBusinessSource;
}
export const source = {
  id: "source-voice",
  session_id: "session-voice",
  invocation_id: "invocation-voice",
  provider_call_id: "native-call",
  native_invocation_id: "native-invocation",
  fact_kind: "promise",
  facts_json:
    '{"amount":{"minor_units":10000000000000001,"currency":"AUD","exponent":2},"due_date":"2026-12-01"}',
  facts_hash: "a".repeat(64),
  observed_at: "2026-11-19T00:00:00Z",
  synthetic: true,
  evidence_mode: "MOCKED_HTTP",
  source_state: "VOICE_OPERATIONAL_METADATA",
};
test("retained signed facts display exact large money/native references without a fake original or transcript", () => {
  const html = renderToStaticMarkup(
    React.createElement(component(), { source, workspace: "tenant-a" }),
  );
  assert.match(html, /Unverified operational business facts/);
  assert.match(html, /not a transcript/i);
  assert.match(html, /10000000000000001/);
  assert.match(html, /native-call/);
  assert.match(html, /native-invocation/);
  assert.match(html, /\/voice\/session-voice\?workspace=tenant-a/);
  assert.doesNotMatch(
    html,
    /\/communications\/|download|<audio|payment verified/i,
  );
});
test("untrusted normalized values remain escaped text and all references wrap on narrow screens", () => {
  const html = renderToStaticMarkup(
    React.createElement(component(), {
      source: {
        ...source,
        facts_json: '{"claim_reference":"<script>alert(1)</script>"}',
      },
      workspace: "tenant-a",
    }),
  );
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /overflow-wrap:anywhere/);
});
