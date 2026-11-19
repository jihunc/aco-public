import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
const req = createRequire(new URL("../package.json", import.meta.url));
const ts = req("typescript");
function load() {
  const js = ts.transpileModule(readFileSync(new URL("../src/features/communications/provider-review-api.ts", import.meta.url), "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
  const mod = { exports: {} };
  new Function("require", "module", "exports", js)(req, mod, mod.exports);
  return mod.exports;
}
const current = () => ({ connection_id: "company-email", expected_activation_id: "prior", reviewed_hash: "a".repeat(64),
  provider: "resend", evidence_mode: "LIVE", mode: "OUTBOUND_TEST_ONLY", sender: "onboarding@resend.dev",
  destinations: ["approved@example.test"], request_cap: 1, expires_at: "2026-11-19T00:00:00Z", max_requests: 2, max_usd: "1" });
test("captured company review cannot silently adopt new recipients or authority", () => {
  const { captureProviderReview, providerReviewIsCurrent, providerReviewCommand } = load();
  const configuration = current(), reviewed = captureProviderReview(configuration);
  configuration.destinations.push("unapproved@example.test");
  configuration.reviewed_hash = "b".repeat(64);
  assert.deepEqual(reviewed.destinations, ["approved@example.test"]);
  assert.equal(providerReviewIsCurrent(reviewed, [configuration]), false);
  assert.equal(providerReviewIsCurrent(reviewed, [current()]), true);
  assert.equal(providerReviewIsCurrent(reviewed, []), false);
  assert.deepEqual(providerReviewCommand(reviewed, "  Reviewed exact scope  "), { connection_id: "company-email", expected_activation_id: "prior", reviewed_hash: "a".repeat(64), evidence: "Reviewed exact scope" });
  assert.throws(() => providerReviewCommand(reviewed, " "), /evidence/);
});
