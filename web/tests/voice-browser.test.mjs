import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript"),
  mod = { exports: {} };
const code = ts.transpileModule(
  readFileSync(
    new URL("../src/features/conversations/voice-browser.ts", import.meta.url),
    "utf8",
  ),
  { compilerOptions: { module: ts.ModuleKind.CommonJS } },
).outputText;
new Function("require", "module", "exports", code)(req, mod, mod.exports);
const join = {
  session_id: "saved-session",
  provider_call_id: "saved-native",
  web_call_url: "https://vapi.daily.co/saved-room",
  expires_at: new Date(Date.now() + 60000).toISOString(),
};

test("expired or foreign private capabilities never initialize browser audio", async () => {
  let created = 0;
  const forbidden = async () => {
    created++;
    throw Error("Forbidden browser audio initialization");
  };
  for (const saved of [
    { ...join, expires_at: "2000-01-01T00:00:00Z" },
    { ...join, expires_at: "invalid" },
    { ...join, web_call_url: "https://foreign.invalid/room" },
    {
      ...join,
      web_call_url: "https://vapi.daily.co/room?credential=forbidden",
    },
  ]) {
    await assert.rejects(
      mod.exports.connectExistingVoice(
        saved,
        new AbortController().signal,
        () => {},
        () => {},
        forbidden,
      ),
    );
  }
  assert.equal(created, 0);
});

test("unmount during SDK loading or join disconnects local audio without another provider create", async () => {
  const controller = new AbortController();
  let stops = 0,
    joins = 0;
  await assert.rejects(
    mod.exports.connectExistingVoice(
      join,
      controller.signal,
      () => {},
      () => {},
      async () => {
        controller.abort();
        return {
          on() {},
          async stop() {
            stops++;
          },
          async reconnect() {
            joins++;
          },
        };
      },
    ),
  );
  assert.equal(joins, 0);
  assert.equal(stops, 1);
  const pending = new AbortController();
  stops = 0;
  await assert.rejects(
    mod.exports.connectExistingVoice(
      join,
      pending.signal,
      () => {},
      () => {},
      async () => ({
        on() {},
        async stop() {
          stops++;
        },
        async reconnect(saved) {
          joins++;
          assert.equal(saved.id, join.provider_call_id);
          pending.abort();
        },
      }),
    ),
  );
  assert.equal(joins, 1);
  assert.ok(stops >= 1);
});
