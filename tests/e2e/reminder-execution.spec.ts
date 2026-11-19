import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import type { EmailExecutionView } from "../../web/src/lib/generated-api.js";

const env = Object.fromEntries((existsSync(".env") ? readFileSync(".env", "utf8") : "")
  .split("\n").filter(line => line.includes("="))
  .map(line => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)]));
const password = process.env.ACO_TEST_DEMO_PASSWORD || env.DEMO_PASSWORD;
const origin = "http://127.0.0.1:53000";

type Invoice = { id: string; number: string; customer: { id: string }; due_date: string;
  financial_status: string; outstanding: { minor_units: string } };
type Recipient = { contact_id: string; customer_id: string; destination: string; source_revision: string; version: number };
type Action = { id: string; invoice_id: string; version: number; status: string; due_at: string; last_error: string | null };

function localSlot(instant: Date) {
  // Prefer a real weekday/business hour in a reviewed synthetic recipient zone.
  const zones = ["Etc/GMT-3", "Etc/GMT-2", "Etc/GMT-1", "UTC", "Etc/GMT+1", "Etc/GMT+2",
    "Etc/GMT+3", "Etc/GMT+4", "Etc/GMT+5", "Etc/GMT+6", "Etc/GMT+7", "Etc/GMT+8",
    "Etc/GMT+9", "Etc/GMT+10", "Etc/GMT+11", "Etc/GMT+12", "Pacific/Kiritimati"];
  for (const zone of zones) {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
      timeZone: zone, weekday: "short", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).formatToParts(instant).map(part => [part.type, part.value]));
    const hour = Number(parts.hour);
    if (["Mon", "Tue", "Wed", "Thu", "Fri"].includes(parts.weekday) && hour >= 10 && hour <= 15) {
      return { zone, date: `${parts.year}-${parts.month}-${parts.day}`, time: `${parts.hour}:${parts.minute}` };
    }
  }
  return null;
}

function browserState(command: "ensure-storage" | "evidence", workspace: string, owner: string, action?: string) {
  const args = ["tests/reminder_browser_state.py", command, "--workspace", workspace, "--owner", owner];
  if (action) args.push("--action", action);
  const output = execFileSync(".venv/bin/python", args, {
    cwd: process.cwd(), encoding: "utf8", timeout: 30_000,
    env: { ...process.env, PYTHONPATH: "src:tests:." },
  });
  return JSON.parse(output);
}

test("reviewed synthetic reminder reaches native provider, original and reloaded invoice UI", async ({ page }) => {
  test.setTimeout(420_000);
  expect(process.env.ACO_TEST_PROFILE).toBe("synthetic-reminders");
  await page.goto("/login");
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Username or email").fill("owner@example.test");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await expect(page).toHaveURL(/\/onboarding$/);
  const me = await (await page.request.get("/api/me")).json();
  const mutationHeaders = { origin, "x-csrf-token": me.csrf_token };
  const workspaceResponse = await page.request.post("/api/workspaces", {
    headers: mutationHeaders,
    data: { name: `Reminder browser ${randomUUID()}`, timezone: "Australia/Melbourne" },
  });
  expect(workspaceResponse.status()).toBe(201);
  const workspace = (await workspaceResponse.json()).id as string;
  const base = `/api/workspaces/${workspace}`;
  const seed = await page.request.post(`${base}/demo/accounting/seed`, { headers: mutationHeaders, data: {} });
  expect(seed.status()).toBe(201);
  const storage = browserState("ensure-storage", workspace, me.id);
  expect(storage.storage_connection_id).toBeTruthy();

  const invoicePage = await (await page.request.get(`${base}/receivables?page_size=100`)).json();
  const invoice = (invoicePage.items as Invoice[]).find(row => row.number === "INV-101");
  expect(invoice?.financial_status).toBe("open");
  if (!invoice) throw new Error("Expected actual seeded synthetic invoice is unavailable");
  const financialBefore = { status: invoice.financial_status, outstanding: invoice.outstanding.minor_units };
  const recipients = await (await page.request.get(`${base}/policy-recipients`)).json() as Recipient[];
  const recipient = recipients.find(row => row.customer_id === invoice.customer.id);
  expect(recipient?.destination).toBeTruthy();
  if (!recipient) throw new Error("Source contact for seeded invoice is unavailable");
  const target = new Date(Math.ceil((Date.now() + 180_000) / 60_000) * 60_000);
  const slot = localSlot(target);
  test.skip(!slot, "No genuine weekday 10:00–16:00 synthetic recipient window is available now");
  if (!slot) return;
  const offset = Math.round((Date.parse(`${slot.date}T00:00:00Z`) - Date.parse(`${invoice.due_date}T00:00:00Z`)) / 86_400_000);

  await page.goto(`/settings/policies?workspace=${workspace}`);
  await expect(page.getByRole("heading", { name: "Policy library" })).toBeVisible();
  const recipientReview = await page.request.put(`${base}/policy-recipients/${recipient.contact_id}`, {
    headers: mutationHeaders,
    data: { expected_version: recipient.version, expected_contact_revision: recipient.source_revision,
      expected_destination: recipient.destination, timezone: slot.zone, timezone_verified: true,
      owner_confirmed_tenant_timezone: false, destination_verified: true,
      allowed_channels: ["email"], opt_out: false,
      evidence: "Owner reviewed actual synthetic accounting contact and recipient-local test hour" },
  });
  expect(recipientReview.status()).toBe(200);
  const policy = await page.request.post(`${base}/policies`, {
    headers: mutationHeaders,
    data: { name: `Browser reminder ${randomUUID()}`, expected_version: 0, activate: true,
      native_reminders_disabled: true, configuration_reviewed: true,
      definition: { rules: [{ id: randomUUID(), offset_days: offset, local_time: slot.time,
        channel: "email", template_id: "routine" }],
      templates: [{ id: "routine", subject: "Synthetic invoice reminder",
        body: "Please review the verified synthetic invoice balance." }],
      weekdays: [0, 1, 2, 3, 4], opens_at: "09:00", closes_at: "17:00",
      cap_24h: 1, cap_7d: 3, overrides: [], hold_disputes: true, hold_promises: true,
      ai_external_approval_required: true, autonomy: "internal_only",
      approval_threshold_minor: null, approval_currency: null, voice_enabled: false,
      voice_after_days: 30, voice_approval_required: true },
    },
  });
  expect(policy.status()).toBe(201);

  await page.goto(`/settings/email?workspace=${workspace}`);
  await expect(page.getByText("Email execution is enabled, subject to reviewed scope and current guards.", { exact: true })).toBeVisible();
  await expect(page.getByText("Provider acceptance is not delivery confirmation. Synthetic data and mocked transport are labeled separately from live provider execution.", { exact: true })).toBeVisible();
  await page.getByLabel("Sender email").fill("collections@aco.example.test");
  await page.getByLabel("Permitted recipient emails").fill(recipient.destination);
  await page.getByLabel("Maximum requests").fill("1");
  await page.getByLabel("Review evidence").fill("Owner reviewed synthetic sender, recipient and original capture");
  await page.getByLabel("I reviewed synthetic source capture.").check();
  await page.getByLabel("I reviewed this sender and every destination.").check();
  const activationSaved = page.waitForResponse(response =>
    new URL(response.url()).pathname === `${base}/email-execution` && response.request().method() === "PUT");
  await page.getByRole("button", { name: "Save synthetic activation" }).click();
  const activationResponse = await activationSaved;
  expect(activationResponse.status()).toBe(200);
  const reviewedExecution = await activationResponse.json() as EmailExecutionView;
  expect(reviewedExecution.system_enabled).toBe(true);
  expect(reviewedExecution.mode).toBe("SYNTHETIC");
  expect(reviewedExecution.provider).toBe("demo_email");
  expect(reviewedExecution.synthetic).toBe(true);
  expect(reviewedExecution.activation?.current).toBe(true);
  await expect(page.getByText("Current channel: Synthetic demo.", { exact: true })).toBeVisible();
  await expect(page.getByText("Synthetic email activation reviewed and saved.")).toBeVisible();
  await page.getByRole("button", { name: "Request workspace resume" }).click();
  await expect(page.getByText("Workspace outbound resume requested; current guards still apply.")).toBeVisible();

  const queued = await (await page.request.get(`${base}/scheduled-actions?kind=policy_due&status=queued`)).json() as Action[];
  const action = queued.find(row => row.invoice_id === invoice.id);
  expect(action?.due_at).toBeTruthy();
  if (!action) throw new Error("Reviewed future action for source invoice was not scheduled");
  expect(new Date(action.due_at).getTime()).toBeGreaterThan(Date.now());
  // The maintained worker is already running. It may claim the row as soon as due.
  while (Date.now() < new Date(action.due_at).getTime()) {
    await page.waitForTimeout(Math.min(15_000, new Date(action.due_at).getTime() - Date.now()));
  }
  await page.reload();
  const dueButton = page.getByRole("button", { name: "Queue due reminder" });
  if (await dueButton.isVisible().catch(() => false)) {
    const response = page.waitForResponse(res => res.url().endsWith(`${base}/demo/reminders/run-due`));
    await dueButton.click();
    const runDue = await response;
    expect([202, 409]).toContain(runDue.status());
    if (runDue.status() === 202) {
      await expect(page.getByText("Due reminder accepted into the worker queue. Check delivery evidence in Conversations.")).toBeVisible();
    }
  }
  await expect.poll(async () => {
    const rows = await (await page.request.get(`${base}/scheduled-actions?kind=policy_due&status=succeeded`)).json() as Action[];
    return rows.some(row => row.id === action.id);
  }, { timeout: 180_000, intervals: [5_000, 10_000, 15_000] }).toBe(true);
  const evidence = browserState("evidence", workspace, me.id, action.id);
  expect(evidence).toEqual({ native_operations: 1, native_send_invocations: 1, communications: 1 });

  const communications = await (await page.request.get(`${base}/communications?invoice_id=${invoice.id}`)).json();
  expect(communications.total).toBe(1);
  const message = communications.items[0];
  expect(message.action_id).toBe(action.id);
  expect(message.direction).toBe("outgoing");
  expect(message.delivery_state).toBe("accepted");
  const source = message.sources.find((item: { available: boolean }) => item.available);
  expect(source?.artifact_id).toBeTruthy();
  const original = await page.request.get(`${base}/artifacts/${source.artifact_id}/content`);
  expect(original.status()).toBe(200);
  const mime = await original.body();
  expect(createHash("sha256").update(mime).digest("hex")).toBe(source.sha256);
  expect(mime.toString("utf8")).toContain("Synthetic invoice reminder");
  const financialAfter = await (await page.request.get(`${base}/receivables/${invoice.id}`)).json();
  expect(financialAfter.invoice.financial_status).toBe(financialBefore.status);
  expect(financialAfter.invoice.outstanding.minor_units).toBe(financialBefore.outstanding);

  await page.goto(`/conversations?workspace=${workspace}`);
  await expect(page.getByRole("heading", { name: "Synthetic invoice reminder" })).toBeVisible();
  await expect(page.getByText("Accepted by provider; delivery unconfirmed")).toBeVisible();
  await page.getByRole("button", { name: "Open message" }).click();
  await expect(page.getByRole("link", { name: "Download authenticated source" })).toBeVisible();
  await page.goto(`/receivables/${invoice.id}?workspace=${workspace}`);
  await expect(page.getByRole("heading", { name: "Invoice communications" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Synthetic invoice reminder" })).toBeVisible();
});
