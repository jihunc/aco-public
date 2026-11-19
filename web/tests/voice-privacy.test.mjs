import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript");
function load() {
  const code = ts.transpileModule(
    readFileSync(
      new URL(
        "../src/features/conversations/voice-privacy-api.ts",
        import.meta.url,
      ),
      "utf8",
    ),
    { compilerOptions: { module: ts.ModuleKind.CommonJS } },
  ).outputText;
  const mod = { exports: {} };
  new Function("require", "module", "exports", code)(
    (name) => (name === "@/lib/api" ? {} : req(name)),
    mod,
    mod.exports,
  );
  return mod.exports;
}
test("each capture purpose defaults off and changed privacy version invalidates reviewed mutation", () => {
  const api = load();
  assert.deepEqual(Object.values(api.emptyCaptureScopes()), [
    false,
    false,
    false,
    false,
    false,
  ]);
  const reviewed = {
    version: 4,
    artifact: { id: "a" },
    legal_hold_state: "none",
    events: [],
  };
  assert.equal(
    api.samePrivacyReview(reviewed, { ...reviewed, version: 5 }),
    false,
  );
  assert.equal(
    api.samePrivacyReview(reviewed, {
      ...reviewed,
      legal_hold_state: "review_required",
    }),
    false,
  );
  assert.equal(
    api.samePrivacyReview(reviewed, structuredClone(reviewed)),
    true,
  );
});
test("private transcript parsing requires fixed synthetic speaker and time schema and preserves inert text", () => {
  const api = load();
  const source = {
    synthetic: true,
    evidence_mode: "FAKE",
    turns: [
      {
        speaker: "participant",
        offset_ms: 500,
        text: "<script>do not execute</script>",
      },
    ],
  };
  assert.deepEqual(api.transcriptTurns(source), source.turns);
  for (const bad of [
    { ...source, synthetic: false },
    { ...source, turns: [{ speaker: "p", offset_ms: -1, text: "bad" }] },
    { ...source, turns: [{ speaker: "p", offset_ms: 0, text: 12 }] },
  ])
    assert.throws(() => api.transcriptTurns(bad));
});

test("saved consent renders each independent permission and inert evidence without claiming real capture", () => {
  const React = req("react"),
    { renderToStaticMarkup } = req("react-dom/server");
  const code = ts.transpileModule(
    readFileSync(
      new URL(
        "../src/features/conversations/voice-privacy.tsx",
        import.meta.url,
      ),
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
  const mod = { exports: {} };
  new Function("require", "module", "exports", code)(
    (name) =>
      name === "next/link"
        ? () => null
        : name === "@tanstack/react-query"
          ? {}
          : name === "@/lib/api"
            ? {}
            : name === "@/features/receivables/shared"
              ? { stamp: (v) => v }
              : name === "./voice-privacy-api"
                ? load()
                : req(name),
    mod,
    mod.exports,
  );
  const text = renderToStaticMarkup(
    React.createElement(mod.exports.ConsentRecord, {
      consent: {
        id: "original-consent",
        version: 1,
        active: true,
        command: {
          decision: "GRANTED",
          purpose: "Synthetic fixture",
          notice: "<script>never execute</script>",
          jurisdiction: "AU",
          evidence: "Explicit owner evidence",
          subject: "SYNTHETIC_PARTICIPANT",
          method: "EXPLICIT_SYNTHETIC_REVIEW",
          expires_at: "2026-11-20",
          scopes: {
            audio_capture: false,
            transcript: true,
            private_storage: true,
            playback: false,
            provider_retention: true,
          },
        },
        recorded_by: "real-user",
        recorded_at: "2026-11-19",
        material_hash: "exact-hash",
        reasons: [],
      },
    }),
  );
  assert.match(text, /Audio capture: Not permitted/);
  assert.match(text, /Transcript: Permitted/);
  assert.match(text, /Playback: Not permitted/);
  assert.match(text, /FAKE originals only/);
  assert.match(text, /exact-hash/);
  assert.match(text, /&lt;script&gt;/);
  assert.doesNotMatch(text, /<script>/);
});
test("privacy mutation sends exact reviewed version and CSRF once, and does not retry conflicts", async () => {
  const old = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push([url, options]);
    return { ok: false, status: 409 };
  };
  try {
    const command = {
      request_id: "request",
      expected_version: 7,
      kind: "DELETE_APPROVED",
      parent_id: "saved-request",
      reason: "Explicit reviewed request",
    };
    await assert.rejects(
      load().voicePrivacyApi.disposition("tenant", "artifact", command, "csrf"),
      /changed/,
    );
    assert.equal(calls.length, 1);
    assert.deepEqual(JSON.parse(calls[0][1].body), command);
    assert.equal(calls[0][1].headers["x-csrf-token"], "csrf");
    assert.equal(calls[0][1].cache, "no-store");
  } finally {
    globalThis.fetch = old;
  }
});
