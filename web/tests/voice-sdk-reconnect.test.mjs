import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { test } from "node:test";

const webRequire = createRequire(new URL("../package.json", import.meta.url));
const sdkPath = webRequire.resolve("@vapi-ai/web");
const sdkRequire = createRequire(sdkPath);

test("pinned Vapi constructor and existing-call reconnect make no create request or recording", async () => {
  const packageInfo = JSON.parse(
    readFileSync(join(dirname(dirname(sdkPath)), "package.json"), "utf8"),
  );
  assert.equal(packageInfo.version, "3.1.1");
  const joins = [];
  let recording = 0;
  let destroyed = 0;
  let providerCreates = 0;
  const daily = {
    iframe: () => null,
    on() {
      return this;
    },
    once() {
      return this;
    },
    async join(options) {
      joins.push(options);
    },
    async destroy() {
      destroyed += 1;
    },
    startRemoteParticipantsAudioLevelObserver() {},
    async updateInputSettings() {},
    startRecording() {
      recording += 1;
    },
    async stopRecording() {
      recording += 1;
    },
  };
  const module = { exports: {} };
  const client = {
    baseUrl: "",
    setSecurityData(token) {
      assert.equal(token, "");
    },
    call: {
      callControllerCreateWebCall() {
        providerCreates += 1;
        throw Error("Client create forbidden");
      },
    },
  };
  const imports = (name) =>
    name === "@daily-co/daily-js"
      ? {
          createCallObject(options) {
            assert.equal(options.videoSource, false);
            return daily;
          },
        }
      : name === "./client"
        ? { client }
        : sdkRequire(name);
  // Execute the actual installed release; only its external Daily/API boundaries are doubles.
  new Function("require", "module", "exports", readFileSync(sdkPath, "utf8"))(
    imports,
    module,
    module.exports,
  );
  const ts = webRequire("typescript");
  const wrapper = { exports: {} };
  const compiled = ts.transpileModule(
    readFileSync(
      new URL(
        "../src/features/conversations/voice-browser.ts",
        import.meta.url,
      ),
      "utf8",
    ),
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
        esModuleInterop: true,
      },
    },
  ).outputText;
  new Function("require", "module", "exports", compiled)(
    (name) => (name === "@vapi-ai/web" ? module.exports : webRequire(name)),
    wrapper,
    wrapper.exports,
  );
  const controller = new AbortController();
  const connected = await wrapper.exports.connectExistingVoice(
    {
      session_id: "saved-session",
      provider_call_id: "saved-native-id",
      web_call_url: "https://vapi.daily.co/synthetic-owned-room",
      expires_at: new Date(Date.now() + 60000).toISOString(),
    },
    controller.signal,
    () => {},
    () => {},
  );
  assert.deepEqual(joins, [
    {
      url: "https://vapi.daily.co/synthetic-owned-room",
      subscribeToTracksAutomatically: false,
    },
  ]);
  assert.equal(providerCreates, 0);
  assert.equal(recording, 0);
  await connected.disconnect();
  await connected.disconnect();
  assert.equal(destroyed, 1);
  assert.equal(providerCreates, 0);
});
