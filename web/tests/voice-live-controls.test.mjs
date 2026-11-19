import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript"),
  React = req("react"),
  { renderToStaticMarkup } = req("react-dom/server");
let calls = 0;
const forbidden = () => {
  calls++;
  throw Error("No action on mount");
};
const output = ts.transpileModule(
  readFileSync(
    new URL(
      "../src/features/conversations/voice-live-controls.tsx",
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
new Function("require", "module", "exports", output)(
  (name) =>
    name === "./voice-browser"
      ? { connectExistingVoice: forbidden }
      : name === "./voice-api"
        ? {
            captureVoice: structuredClone,
            sameVoiceSnapshot: forbidden,
            voiceStartCommand: forbidden,
            voiceApi: { join: forbidden, end: forbidden },
          }
        : req(name),
  mod,
  mod.exports,
);
const base = {
  id: "saved",
  version: 4,
  evidence_mode: "LIVE",
  participant_email: "exact-participant@synthetic.invalid",
  can_join: true,
  can_end: true,
  state: "active",
};
const render = (session) =>
  renderToStaticMarkup(
    React.createElement(mod.exports.VoiceLiveControls, {
      session,
      workspace: "workspace",
      csrf: "synthetic-csrf",
      fresh: true,
      refresh: forbidden,
    }),
  );

test("eligible existing call still requires explicit current review and never joins or ends on mount", () => {
  const html = render(base);
  assert.match(html, /exact-participant@synthetic.invalid/);
  assert.match(html, /agree to join.*microphone, capture off/);
  assert.match(html, /disabled="">Join existing browser call/);
  assert.match(html, /disabled="">Request end call/);
  assert.doesNotMatch(
    html,
    /vapi.daily.co|api_key|controlUrl|type="checkbox" checked/,
  );
  assert.equal(calls, 0);
});

test("current denied participant or consumed stop cannot expose an actionable control", () => {
  const html = render({
    ...base,
    can_join: false,
    can_end: false,
    join_reasons: ["VOICE_LIVE_PARTICIPANT_REQUIRED"],
    end_reasons: ["VOICE_LIVE_STOP_ALREADY_ATTEMPTED"],
  });
  assert.match(html, /VOICE LIVE PARTICIPANT REQUIRED/);
  assert.match(html, /VOICE LIVE STOP ALREADY ATTEMPTED/);
  assert.doesNotMatch(html, /<button|<input/);
  assert.equal(calls, 0);
});
