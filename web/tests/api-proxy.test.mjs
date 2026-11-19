import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";

const require = createRequire(new URL("../package.json", import.meta.url));
const ts = require("typescript");
const module = { exports: {} };
new Function("module", "exports", ts.transpileModule(
  readFileSync(new URL("../src/app/api/[...path]/route.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
).outputText)(module, module.exports);
const workspace = "37d8ce26-e495-43a9-b645-a62383034b9c";
const run = "38bbcd5d-57c4-42e5-8d44-4fd3519fc25b";

test("agent events proxy forwards replay and streams committed bytes until browser disconnect", async () => {
  const controller = new AbortController();
  let observed;
  let upstreamController;
  globalThis.fetch = async (url, options) => {
    observed = { url: String(url), ...options };
    return new Response(new ReadableStream({ start(c) { upstreamController = c; } }), {
      headers: { "content-type": "text/event-stream" },
    });
  };
  const incoming = new Request(`http://localhost:53000/api/workspaces/${workspace}/agent-runs/${run}/events`, {
    signal: controller.signal,
    headers: { cookie: "aco_session=synthetic", accept: "text/event-stream", "last-event-id": `${run}:8` },
  });
  const result = await module.exports.GET(incoming);
  assert.equal(observed.headers.get("last-event-id"), `${run}:8`);
  assert.equal(observed.headers.get("accept"), "text/event-stream");
  assert.equal(observed.headers.get("cookie"), "aco_session=synthetic");
  assert.equal(observed.redirect, "manual");
  assert.equal(result.headers.get("content-type"), "text/event-stream");
  assert.equal(result.headers.get("x-accel-buffering"), "no");
  upstreamController.enqueue(new TextEncoder().encode(`id: ${run}:9\nevent: agent.step.completed\ndata: {}\n\n`));
  const reader = result.body.getReader();
  const first = await reader.read();
  assert.match(new TextDecoder().decode(first.value), /agent.step.completed/);
  controller.abort();
  assert.equal(observed.signal.aborted, true);
  await reader.cancel();
});

test("ordinary JSON routes keep bounded timeout and do not forward event replay authority", async () => {
  let observed;
  globalThis.fetch = async (_url, options) => {
    observed = options;
    return Response.json({ status: "queued" });
  };
  const request = new Request(`http://localhost:53000/api/workspaces/${workspace}/agent-runs/${run}`, {
    headers: { "last-event-id": `${run}:8` },
  });
  const result = await module.exports.GET(request);
  assert.equal(observed.headers.get("last-event-id"), null);
  assert.notEqual(observed.signal, request.signal);
  assert.equal(result.headers.get("cache-control"), "no-store");
  assert.deepEqual(await result.json(), { status: "queued" });
});


test("private artifact download preserves exact bytes and API attachment, nosniff and sandbox headers",async()=>{
 const bytes=new TextEncoder().encode("Synthetic original MIME\r\nContent-Type: text/html\r\n\r\n<not-rendered>");
 globalThis.fetch=async()=>new Response(bytes,{headers:{"content-type":"application/octet-stream","content-disposition":`attachment; filename="artifact-${run}"`,"x-content-type-options":"nosniff","content-security-policy":"default-src 'none'; sandbox"}});
 const result=await module.exports.GET(new Request(`http://localhost:53000/api/workspaces/${workspace}/artifacts/${run}/content`));
 assert.equal(result.headers.get("content-disposition"),`attachment; filename="artifact-${run}"`);
 assert.equal(result.headers.get("x-content-type-options"),"nosniff");assert.equal(result.headers.get("content-security-policy"),"default-src 'none'; sandbox");assert.equal(result.headers.get("cache-control"),"no-store");assert.deepEqual(new Uint8Array(await result.arrayBuffer()),bytes);
});
test("only the exact ingress POST forwards signature headers and preserves the original signed bytes",async()=>{
 const body="{ \"id\":\"synthetic\", \"text\":\"🙂 exact bytes\" }".replaceAll('\\"','"');
 let observed;globalThis.fetch=async(url,options)=>{observed={url:String(url),...options};return Response.json([],{status:202});};
 const headers={"content-type":"application/json","svix-id":"msg_synthetic","svix-timestamp":"1700000000","svix-signature":"v1,synthetic-not-valid",authorization:"Bearer must-not-forward"};
 await module.exports.POST(new Request(`http://localhost:53000/api/email-ingress/${run}`,{method:"POST",headers,body}));
 assert.equal(observed.headers.get("svix-id"),headers["svix-id"]);assert.equal(observed.headers.get("svix-timestamp"),headers["svix-timestamp"]);assert.equal(observed.headers.get("svix-signature"),headers["svix-signature"]);assert.equal(observed.headers.get("authorization"),null);assert.equal(new TextDecoder().decode(observed.body),body);
 await module.exports.POST(new Request(`http://localhost:53000/api/workspaces/${workspace}/agent-runs`,{method:"POST",headers,body}));assert.equal(observed.headers.get("svix-id"),null);assert.equal(observed.headers.get("svix-signature"),null);
});


test("shared email webhook proxy forwards exact raw bytes and Svix headers only on the exact POST", async () => {
  const body = '{ "type": "email.received", "text": "🙂 signed" }';
  const headers = {"content-type":"application/json", "svix-id":"msg_shared",
    "svix-timestamp":"1700000000", "svix-signature":"v1,synthetic",
    authorization:"Bearer not-forwarded", "x-customer-tenant":"not-authority"};
  let observed;
  globalThis.fetch = async (url, options) => { observed = {url:String(url), ...options}; return Response.json([],{status:202}); };
  for (const [method,path,expected] of [
    ["POST",`/api/email-webhooks/${run}`,true],
    ["GET",`/api/email-webhooks/${run}`,false],
    ["POST",`/api/email-webhooks/${run}/extra`,false],
    ["POST","/api/email-webhooks/not-a-uuid",false],
  ]) {
    await module.exports[method](new Request(`http://localhost:53000${path}`, {
      method, headers, ...(method === "POST" ? {body} : {}),
    }));
    assert.equal(observed.headers.get("svix-id"),expected?headers["svix-id"]:null);
    assert.equal(observed.headers.get("svix-signature"),expected?headers["svix-signature"]:null);
    assert.equal(observed.headers.get("authorization"),null);
    assert.equal(observed.headers.get("x-customer-tenant"),null);
    if (method === "POST") assert.equal(new TextDecoder().decode(observed.body),body);
  }
});

test("business export preserves the attachment integrity reference without forwarding client authority", async () => {
  const digest="b".repeat(64);let observed;
  globalThis.fetch=async(_url,options)=>{observed=options;return new Response('{"raw_originals_included":false}',{headers:{"content-type":"application/json","content-disposition":"attachment; filename=business.json","x-content-sha256":digest}});};
  const result=await module.exports.POST(new Request(`http://localhost:53000/api/workspaces/${workspace}/exports`,{method:"POST",headers:{"content-type":"application/json","x-csrf-token":"synthetic-csrf",authorization:"Bearer forbidden","x-content-sha256":"client-not-authority"},body:'{"resources":["invoices"]}'}));
  assert.equal(result.headers.get("x-content-sha256"),digest);
  assert.equal(result.headers.get("cache-control"),"no-store");
  assert.equal(observed.headers.get("authorization"),null);assert.equal(observed.headers.get("x-content-sha256"),null);
  assert.equal(observed.headers.get("x-csrf-token"),"synthetic-csrf");
});

function recordTimeouts() {
  const original = AbortSignal.timeout;
  const bounds = [];
  AbortSignal.timeout = (ms) => { bounds.push(ms); return original.call(AbortSignal, ms); };
  return { bounds, restore: () => { AbortSignal.timeout = original; } };
}

test("sign-in exchange gets a longer upstream bound than ordinary JSON routes", async () => {
  globalThis.fetch = async () => new Response(null, { status: 303, headers: { location: "/onboarding" } });
  const timeouts = recordTimeouts();
  try {
    await module.exports.GET(new Request("http://localhost:53000/api/auth/callback?code=synthetic&state=s"));
    await module.exports.GET(new Request("http://localhost:53000/api/auth/login"));
    await module.exports.GET(new Request(`http://localhost:53000/api/workspaces/${workspace}`));
  } finally { timeouts.restore(); }
  assert.deepEqual(timeouts.bounds, [60000, 60000, 15000]);
});

test("a failed browser navigation redirects to a readable sign-in retry page", async () => {
  globalThis.fetch = async () => { throw new Error("upstream aborted"); };
  const result = await module.exports.GET(new Request("http://localhost:53000/api/auth/callback?code=c&state=s", {
    headers: { accept: "text/html,application/xhtml+xml" },
  }));
  assert.equal(result.status, 303);
  assert.equal(result.headers.get("location"), "/login?error=unavailable");
});

test("a failed API call still returns the JSON 503 contract", async () => {
  globalThis.fetch = async () => { throw new Error("upstream aborted"); };
  const result = await module.exports.GET(new Request(`http://localhost:53000/api/workspaces/${workspace}`, {
    headers: { accept: "application/json" },
  }));
  assert.equal(result.status, 503);
  assert.deepEqual(await result.json(), { detail: "Service unavailable. Try again." });
});
