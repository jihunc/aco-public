/** Explicit disposable-product rehearsal; never imported by offline test runners. */
import { readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { chromium, expect } from "@playwright/test";

const config = resolve(process.argv[2] || ".tooling/product/local/product.env");
const evidencePath = resolve(dirname(config), "smoke-result.json");
const baselinePath = resolve(dirname(config), "smoke-baseline.json");
const flags = new Set(process.argv.slice(3));
if ([...flags].some((flag) => flag !== "--verify-restart" && flag !== "--inbound" && flag !== "--scheduled")) throw new Error("Unsupported rehearsal option");
if (statSync(config).mode & 0o077) throw new Error("Private configuration requires mode 0600");
const values = Object.fromEntries(readFileSync(config, "utf8").trim().split("\n").map((line) => {
  const index = line.indexOf("=");
  return [line.slice(0, index), JSON.parse(line.slice(index + 1))];
}));
if (!/^aco-product-[a-z0-9][a-z0-9-]{0,30}$/.test(values.PRODUCT_PROJECT)) throw new Error("Dedicated product project required");
const origin = `http://127.0.0.1:${values.PRODUCT_WEB_PORT}`;
const evidence = { project: values.PRODUCT_PROJECT, origin, stages: [], workspaces: [] };
let stage = "launch";
let observedPage;
const browser = await chromium.launch({ channel: "chrome", headless: true });
try {
  const context = await browser.newContext({ baseURL: origin, viewport: { width: 1440, height: 1200 } });
  const page = await context.newPage();
  observedPage = page;
  page.setDefaultTimeout(15000);
  stage = "product-readiness";
  let ready = false;
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      const response = await page.request.get("/api/health", { timeout: 2000 });
      if (response.ok()) { ready = true; evidence.readiness_attempts = attempt + 1; break; }
    } catch { /* bounded restart/startup gap */ }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  if (!ready) throw new Error("Product did not become ready");
  stage = "exact-issuer-browser-login";
  await page.goto("/login");
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  if (new URL(page.url()).hostname !== "identity.aco.localhost") throw new Error("Unexpected issuer host");
  await page.getByLabel("Username or email").fill("owner@example.test");
  await page.getByLabel("Password", { exact: true }).fill(values.DEMO_PASSWORD);
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await page.waitForURL(`${origin}/onboarding`);
  evidence.stages.push(stage);
  const meResponse = await page.request.get("/api/me");
  if (!meResponse.ok()) throw new Error("Authenticated session missing");
  const me = await meResponse.json();
  const headers = { origin, "x-csrf-token": me.csrf_token };
  if (flags.has("--scheduled")) {
    const previous = JSON.parse(readFileSync(baselinePath, "utf8"));
    if (previous.status !== "PASS" || previous.project !== values.PRODUCT_PROJECT) throw new Error("Matching prior proof required");
    // Each scheduled rehearsal uses a fresh SYNTHETIC workspace. Prior policy,
    // contact caps, communications and all audit history remain untouched.
    const created = await page.request.post("/api/workspaces", { headers, data: { name: `Synthetic scheduled proof ${randomUUID()}`, timezone: "Australia/Melbourne" } });
    if (created.status() !== 201) throw new Error("Scheduled synthetic workspace creation failed");
    const workspace = await created.json();
    const base = `/api/workspaces/${workspace.id}`;
    const seeded = await page.request.post(`${base}/demo/accounting/seed`, { headers, data: {} });
    if (seeded.status() !== 201) throw new Error("Scheduled synthetic source import failed");
    stage = "reviewed-synthetic-schedule";
    const prep = await page.request.post(`${base}/email-ingress/setup`, { headers, data: { request_id: randomUUID(), evidence: "Explicit synthetic scheduled reminder evaluation and private original storage." } });
    if (!prep.ok()) throw new Error("Synthetic storage preparation failed");
    const invoices = await (await page.request.get(`${base}/receivables?page_size=100`)).json();
    const invoice = invoices.items.find(row => row.number === "INV-101");
    const recipients = await (await page.request.get(`${base}/policy-recipients`)).json();
    const recipient = recipients.find(row => row.customer_id === invoice.customer.id);
    const target = new Date(Math.ceil((Date.now() + 70000) / 60000) * 60000);
    let slot;
    for (let offset = -12; offset <= 14; offset++) {
      const zone = offset === 0 ? "UTC" : `Etc/GMT${offset > 0 ? "-" : "+"}${Math.abs(offset)}`;
      const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: zone, weekday: "short", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(target).map(row => [row.type, row.value]));
      if (["Mon", "Tue", "Wed", "Thu", "Fri"].includes(parts.weekday) && Number(parts.hour) >= 10 && Number(parts.hour) <= 15) {
        slot = { zone, date: `${parts.year}-${parts.month}-${parts.day}`, time: `${parts.hour}:${parts.minute}` }; break;
      }
    }
    if (!slot) throw new Error("No actual synthetic recipient business window; schedule test NOT RUN");
    const reviewed = await page.request.put(`${base}/policy-recipients/${recipient.contact_id}`, { headers, data: { expected_version: recipient.version, expected_contact_revision: recipient.source_revision, expected_destination: recipient.destination, timezone: slot.zone, timezone_verified: true, owner_confirmed_tenant_timezone: false, destination_verified: true, allowed_channels: ["email"], opt_out: false, evidence: "Owner reviewed actual synthetic source contact and local test window" } });
    if (!reviewed.ok()) throw new Error("Synthetic recipient review failed");
    const offsetDays = Math.round((Date.parse(`${slot.date}T00:00:00Z`) - Date.parse(`${invoice.due_date}T00:00:00Z`)) / 86400000);
    const policy = await page.request.post(`${base}/policies`, { headers, data: { name: `Docker scheduled proof ${randomUUID()}`, expected_version: 0, activate: true, native_reminders_disabled: true, configuration_reviewed: true, definition: { rules: [{ id: randomUUID(), offset_days: offsetDays, local_time: slot.time, channel: "email", template_id: "routine" }], templates: [{ id: "routine", subject: "Synthetic invoice reminder", body: "Review the synthetic source balance." }], weekdays: [0, 1, 2, 3, 4], opens_at: "09:00", closes_at: "17:00", cap_24h: 1, cap_7d: 3, overrides: [], hold_disputes: true, hold_promises: true, ai_external_approval_required: true, autonomy: "internal_only", approval_threshold_minor: null, approval_currency: null, voice_enabled: false, voice_after_days: 30, voice_approval_required: true } } });
    if (policy.status() !== 201) {
      writeFileSync(resolve(dirname(config), "schedule-validation.json"), JSON.stringify(await policy.json()), { mode: 0o600 });
      throw new Error(`Scheduled policy creation failed (${policy.status()})`);
    }
    await page.goto(`/settings/email?workspace=${workspace.id}`);
    await page.getByLabel("Sender email").fill("collections@aco.example.test");
    await page.getByLabel("Permitted recipient emails").fill(recipient.destination);
    await page.getByLabel("Maximum requests").fill("1");
    await page.getByLabel("Review evidence").fill("Owner reviewed synthetic sender, recipient and retained original");
    await page.getByLabel("I reviewed synthetic source capture.").check();
    await page.getByLabel("I reviewed this sender and every destination.").check();
    const activated = page.waitForResponse(row => row.url().endsWith(`${base}/email-execution`) && row.request().method() === "PUT");
    await page.getByRole("button", { name: "Save synthetic activation" }).click();
    if (!(await activated).ok()) throw new Error("Synthetic activation refused");
    await page.getByRole("button", { name: "Request workspace resume" }).click();
    await page.getByText("Workspace outbound resume requested; current guards still apply.", { exact: true }).waitFor();
    const rows = await (await page.request.get(`${base}/scheduled-actions?kind=policy_due&status=queued`)).json();
    const action = rows.find(row => row.invoice_id === invoice.id);
    if (!action || Date.parse(action.due_at) <= Date.now()) throw new Error("Future schedule missing");
    stage = "maintained-worker-scheduled-synthetic-send";
    let succeeded = false;
    for (let attempt = 0; attempt < 180; attempt++) {
      const done = await (await page.request.get(`${base}/scheduled-actions?kind=policy_due&status=succeeded`)).json();
      if (done.some(row => row.id === action.id)) { succeeded = true; break; }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    if (!succeeded) throw new Error("Scheduled synthetic action did not complete");
    const communications = await (await page.request.get(`${base}/communications?invoice_id=${invoice.id}`)).json();
    const sent = communications.items.find(row => row.action_id === action.id && row.direction === "outgoing");
    if (!sent || sent.delivery_state !== "accepted" || !sent.synthetic || !sent.sources.some(row => row.available)) throw new Error("Retained synthetic communication missing");
    const after = await (await page.request.get(`${base}/receivables/${invoice.id}`)).json();
    if (after.invoice.financial_status !== invoice.financial_status || after.invoice.outstanding.minor_units !== invoice.outstanding.minor_units) throw new Error("Scheduling altered financial truth");
    evidence.workspaces = previous.workspaces;
    evidence.scheduled_workspace_id = workspace.id;
    evidence.financial_truth_unchanged = true;
    evidence.action_id = action.id;
    evidence.communication_id = sent.id;
    evidence.stages.push(stage);
  } else if (flags.has("--inbound")) {
    const previous = JSON.parse(readFileSync(baselinePath, "utf8"));
    if (previous.status !== "PASS" || previous.project !== values.PRODUCT_PROJECT) throw new Error("Matching prior proof required");
    const workspace = previous.workspaces[0];
    const base = `/api/workspaces/${workspace.id}`;
    stage = "owner-prepare-private-inbox";
    const prepared = await page.request.post(`${base}/email-ingress/setup`, { headers, data: { request_id: randomUUID(), evidence: "Explicit local-only product rehearsal of synthetic source and private storage." } });
    if (!prepared.ok()) throw new Error("Actual inbox preparation failed");
    const setup = await prepared.json();
    const activationResponse = await page.request.post(`${base}/email-ingress/activations`, { headers, data: { email_connection_id: setup.email_connections[0]?.id, storage_connection_id: setup.storage_connections[0]?.id, expires_in_seconds: 3600, evidence: "Current Owner authorizes fixed synthetic-only product rehearsal; no outbound or paid AI." } });
    if (!activationResponse.ok()) throw new Error("Current Owner activation failed");
    const activation = await activationResponse.json();
    evidence.stages.push(stage);
    stage = "signed-source-worker-private-original";
    const command = { request_id: randomUUID(), expected_activation_version: activation.version, invoice_ids: [workspace.invoice_ids[0]], scenario: "invoice_reply" };
    const url = `${base}/email-ingress/activations/${activation.id}/simulate`;
    const simulated = await page.request.post(url, { headers, data: command });
    if (simulated.status() !== 202) throw new Error("Synthetic source notification failed");
    let receipt = await simulated.json();
    for (let attempt = 0; attempt < 60; attempt += 1) {
      receipt = await (await page.request.get(`${base}/email-ingress/receipts/${receipt.id}`)).json();
      if (["processed", "failed", "review_required"].includes(receipt.state)) break;
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    if (receipt.state !== "processed" || !receipt.communication_id || !receipt.agent_run_id || receipt.sender_authority !== "UNVERIFIED" || receipt.sensitive_action_allowed !== false) throw new Error("Maintained source capture incomplete");
    stage = "captured-source-view";
    const communication = await (await page.request.get(`${base}/communications/${receipt.communication_id}`)).json();
    const original = communication.sources.find((item) => item.representation === "provider_original");
    if (!original?.available || original.storage_state !== "stored" || !communication.synthetic) throw new Error("Stored synthetic original missing");
    const artifactPath = `${base}/artifacts/${original.artifact_id}/content`;
    stage = "private-original-integrity";
    const content = await page.request.get(artifactPath);
    const bytes = await content.body();
    const { createHash } = await import("node:crypto");
    evidence.private_original = {
      status: content.status(),
      sha256_matches: createHash("sha256").update(bytes).digest("hex") === original.sha256,
      attachment_header: Boolean(content.headers()["content-disposition"]?.startsWith("attachment;")),
      nosniff_header: content.headers()["x-content-type-options"] === "nosniff",
      sandbox_header: content.headers()["content-security-policy"] === "default-src 'none'; sandbox",
      no_store: content.headers()["cache-control"] === "no-store",
      no_referrer: content.headers()["referrer-policy"] === "no-referrer",
    };
    if (!content.ok() || !evidence.private_original.sha256_matches || !evidence.private_original.attachment_header || !evidence.private_original.nosniff_header || !evidence.private_original.sandbox_header || !evidence.private_original.no_store || !evidence.private_original.no_referrer) throw new Error("Private original integrity failed");
    stage = "anonymous-original-denial";
    const outsider = await browser.newContext({ baseURL: origin });
    const denied = await outsider.request.get(artifactPath);
    if (denied.status() !== 401) throw new Error("Anonymous private read allowed");
    await outsider.close();
    stage = "cross-tenant-original-denial";
    const crossTenant = await page.request.get(`/api/workspaces/${previous.workspaces[1].id}/artifacts/${original.artifact_id}/content`);
    if (crossTenant.status() !== 404) throw new Error("Cross-tenant private read allowed");
    stage = "exact-original-replay";
    const replay = await page.request.post(url, { headers, data: command });
    const replayed = await replay.json();
    if (replay.status() !== 202 || replayed.id !== receipt.id || replayed.communication_id !== receipt.communication_id) throw new Error("Source replay duplicated original");
    evidence.workspaces = previous.workspaces;
    evidence.run_id = previous.run_id;
    evidence.task_id = previous.task_id;
    evidence.receipt_id = receipt.id;
    evidence.communication_id = receipt.communication_id;
    evidence.artifact_id = original.artifact_id;
    evidence.stages.push(stage);
  } else if (flags.has("--verify-restart")) {
    const previous = JSON.parse(readFileSync(baselinePath, "utf8"));
    if (previous.status !== "PASS" || previous.project !== values.PRODUCT_PROJECT) throw new Error("Matching prior proof required");
    stage = "application-restart-preserves-records";
    for (const retained of previous.workspaces) {
      const response = await page.request.get(`/api/workspaces/${retained.id}/receivables?page_size=100`);
      if (!response.ok()) throw new Error("Persisted authenticated source unavailable");
      const ids = (await response.json()).items.map((row) => row.id).sort();
      if (JSON.stringify(ids) !== JSON.stringify(retained.invoice_ids)) throw new Error("Source IDs changed across restart");
    }
    const retained = await page.request.get(`/api/workspaces/${previous.workspaces[0].id}/tasks/${previous.task_id}`);
    if (!retained.ok() || (await retained.json()).id !== previous.task_id) throw new Error("Task not retained");
    const retainedRun = await (await page.request.get(`/api/workspaces/${previous.workspaces[0].id}/agent-runs/${previous.run_id}`)).json();
    if (retainedRun.status !== "completed" || retainedRun.authenticity !== "FAKE") throw new Error("Saved investigation not retained");
    const manualTask = await (await page.request.get(`/api/workspaces/${previous.workspaces[0].id}/tasks/${previous.manual_task_id}`)).json();
    if (manualTask.notes !== "Edited and persisted in Docker PostgreSQL") throw new Error("Manual task edit not retained");
    evidence.workspaces = previous.workspaces;
    evidence.run_id = previous.run_id;
    evidence.task_id = previous.task_id;
    evidence.stages.push(stage);
  } else {
  for (let index = 0; index < 2; index += 1) {
    stage = `current-owner-ui-create-and-seed-${index + 1}`;
    await page.goto("/onboarding");
    await page.getByLabel("Workspace name", { exact: true }).fill(`Product rehearsal ${randomUUID()}`);
    await page.getByLabel("Timezone", { exact: true }).fill("Australia/Melbourne");
    const creation = page.waitForResponse((response) => response.request().method() === "POST" && new URL(response.url()).pathname === "/api/workspaces");
    await page.getByRole("button", { name: "Create demo workspace", exact: true }).click();
    const created = await creation;
    if (created.status() !== 201) throw new Error("Workspace creation failed");
    const workspace = (await created.json()).id;
    const before = await (await page.request.get(`/api/workspaces/${workspace}/receivables?page_size=100`)).json();
    if (before.items.length !== 0) throw new Error("Workspace creation unexpectedly seeded data");
    const panel = page.getByRole("region", { name: "Synthetic accounting setup" });
    const imported = page.waitForResponse((response) => response.request().method() === "POST" && new URL(response.url()).pathname === `/api/workspaces/${workspace}/demo/accounting/seed`);
    await panel.getByRole("button", { name: "Import synthetic demo data", exact: true }).click();
    const seeded = await imported;
    if (seeded.status() !== 201) throw new Error("Authenticated source seed failed");
    const response = await page.request.get(`/api/workspaces/${workspace}/receivables?page_size=100`);
    if (!response.ok()) throw new Error("Source invoice list failed");
    const invoices = await response.json();
    if (invoices.items.length < 20 || invoices.items.length > 30 || invoices.items.length !== invoices.total) throw new Error("Unexpected synthetic seed size");
    await panel.getByText(new RegExp(`${invoices.total} imported invoices`)).waitFor();
    evidence.workspaces.push({ id: workspace, invoice_ids: invoices.items.map((row) => row.id).sort() });
    evidence.stages.push(stage);
  }
  const workspace = evidence.workspaces[0];
  stage = "dashboard-kpis-and-chart";
  await page.goto(`/overview?workspace=${workspace.id}`);
  await page.getByTestId("outstanding-AUD").waitFor();
  const kpi = await page.getByTestId("outstanding-AUD").innerText();
  if (!kpi || kpi.includes("N/A")) throw new Error("Source-backed KPI unavailable");
  await page.locator(".recharts-surface").first().waitFor();
  evidence.stages.push(stage);
  stage = "persistent-task-crud-and-drag";
  await page.goto(`/review?workspace=${workspace.id}`);
  const titles = [`Docker task A ${randomUUID()}`, `Docker task B ${randomUUID()}`];
  for (const title of titles) {
    stage = "task-create";
    await page.getByRole("button", { name: "New task", exact: true }).click();
    await page.getByLabel("Task title", { exact: true }).fill(title);
    await page.getByRole("textbox", { name: /^Notes/ }).fill("Synthetic Docker evaluation task");
    await page.getByRole("button", { name: "Create task", exact: true }).click();
    await page.getByRole("article", { name: title, exact: true }).waitFor();
  }
  stage = "task-edit-after-reload";
  await page.reload();
  await page.getByRole("button", { name: `Edit ${titles[0]}`, exact: true }).click();
  await page.getByRole("textbox", { name: /^Notes/ }).fill("Edited and persisted in Docker PostgreSQL");
  await page.getByRole("button", { name: "Save task", exact: true }).click();
  await page.getByRole("article", { name: titles[0], exact: true }).getByText("Edited and persisted in Docker PostgreSQL", { exact: true }).waitFor();
  stage = "task-keyboard-drag";
  const beforeOrder = await (await page.request.get(`/api/workspaces/${workspace.id}/review-queue`)).json();
  // Exercise the existing dnd-kit keyboard drag sensor, including activation frames.
  const handle = page.getByRole("button", { name: `Drag ${beforeOrder.items[1].title}`, exact: true });
  await expect(handle).toBeEnabled();
  await handle.focus();
  await page.keyboard.press("Space");
  await page.waitForTimeout(500);
  await page.keyboard.press("ArrowUp");
  await page.waitForTimeout(500);
  await page.keyboard.press("Space");
  await page.getByText("Review order saved.", { exact: true }).waitFor();
  await page.reload();
  const afterOrder = await (await page.request.get(`/api/workspaces/${workspace.id}/review-queue`)).json();
  const beforeIds = beforeOrder.items.map(row => row.id);
  const afterIds = afterOrder.items.map(row => row.id);
  if (JSON.stringify(beforeIds) === JSON.stringify(afterIds)) throw new Error("Drag order did not persist");
  evidence.manual_task_id = afterOrder.items.find(row => row.title === titles[0]).id;
  evidence.review_order = afterIds;
  stage = "task-delete";
  await page.getByRole("button", { name: `Delete ${titles[1]}`, exact: true }).click();
  await page.getByRole("button", { name: "Confirm delete", exact: true }).click();
  await page.getByText("Task deleted. Its audit history is retained.", { exact: true }).waitFor();
  await expect(page.getByRole("article", { name: titles[1], exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("article", { name: titles[1], exact: true })).toHaveCount(0);
  evidence.stages.push(stage);
  stage = "maintained-worker-fake-run";
  const started = await page.request.post(`/api/workspaces/${workspace.id}/agent-runs`, { headers, data: { invoice_ids: [workspace.invoice_ids[0]], request_id: randomUUID(), objective: "Inspect selected invoice and source evidence; create an internal review task." } });
  if (started.status() !== 202) throw new Error("Run start failed");
  const run = await started.json();
  if (run.authenticity !== "FAKE") throw new Error("Expected fake execution only");
  const sse = await page.evaluate(async (url) => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(url, { headers: { accept: "text/event-stream" }, signal: controller.signal });
      const chunk = await response.body?.getReader().read();
      return { status: response.status, type: response.headers.get("content-type"), data: new TextDecoder().decode(chunk?.value) };
    } finally { clearTimeout(timeout); controller.abort(); }
  }, `/api/workspaces/${workspace.id}/agent-runs/${run.id}/events`);
  if (sse.status !== 200 || !sse.type?.includes("text/event-stream") || !sse.data.includes("data:")) throw new Error("Actual proxied SSE missing");
  evidence.stages.push("authenticated-production-proxy-sse");
  let finished;
  for (let attempt = 0; attempt < 60; attempt += 1) {
    finished = await (await page.request.get(`/api/workspaces/${workspace.id}/agent-runs/${run.id}`)).json();
    if (["completed", "failed", "cancelled", "partially_completed"].includes(finished.status)) break;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  const proposal = finished?.result?.proposals.find((item) => item.kind === "create_task" && item.executed);
  if (finished?.status !== "completed" || finished.authenticity !== "FAKE" || !proposal?.changed_ids.length) throw new Error("Canonical task outcome missing");
  const taskId = proposal.changed_ids[0];
  const task = await page.request.get(`/api/workspaces/${workspace.id}/tasks/${taskId}`);
  if (!task.ok() || (await task.json()).id !== taskId) throw new Error("Persisted task missing");
  evidence.run_id = run.id;
  evidence.task_id = taskId;
  evidence.stages.push(stage);
  stage = "production-ui-persisted-run";
  await page.goto(`/agent-runs/${run.id}?workspace=${workspace.id}`);
  await page.getByText("Simulated model", { exact: true }).waitFor();
  await page.getByRole("heading", { name: "Findings & outcomes" }).waitFor();
  await page.reload();
  await page.getByRole("heading", { name: "Findings & outcomes" }).waitFor();
  evidence.stages.push(stage);
  }
  evidence.status = "PASS";
} catch (error) {
  // Only fixed stage and assertion name are public. Diagnostic DOM is synthetic and private.
  evidence.error_type = error?.name;
  if (observedPage && !["launch", "exact-issuer-browser-login"].includes(stage)) {
    writeFileSync(resolve(dirname(config), "smoke-error.txt"), String(error?.message).replaceAll(values.DEMO_PASSWORD, "[redacted]"), { mode: 0o600 });
    writeFileSync(resolve(dirname(config), "smoke-diagnostic.txt"), await observedPage.locator("body").innerText(), { mode: 0o600 });
  }
  evidence.status = "FAIL";
  evidence.failed_stage = stage;
  process.exitCode = 1;
} finally {
  await browser.close();
  // No password, cookie, raw provider payload, or resolved environment is emitted.
  writeFileSync(evidencePath, JSON.stringify(evidence, null, 2), { mode: 0o600 });
  if (evidence.status === "PASS" && flags.size === 0) writeFileSync(baselinePath, JSON.stringify(evidence, null, 2), { mode: 0o600 });
  console.log(JSON.stringify({ status: evidence.status, stages: evidence.stages, failed_stage: evidence.failed_stage }));
}
