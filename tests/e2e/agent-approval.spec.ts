import { expect, test, type Page } from "@playwright/test";
import { randomUUID } from "node:crypto";
import type { RunView, ApprovalView } from "../../web/src/lib/generated-api";

async function signIn(page: Page, password: string, email?: string) {
  await page.goto("/login");
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  if (email) {
    await page.getByRole("link", { name: "Register", exact: true }).click();
    for (const [name, value] of [
      ["Username", email],
      ["Email", email],
      ["First name", "Synthetic"],
      ["Last name", "Approval initiator"],
      ["Password", password],
      ["Confirm password", password],
    ])
      await page.getByRole("textbox", { name, exact: true }).fill(value);
    await page.getByRole("button", { name: "Register", exact: true }).click();
  } else {
    await page.getByLabel("Username or email").fill("owner@example.test");
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Sign In", exact: true }).click();
  }
  await expect(page).toHaveURL(/\/onboarding$/);
}

test("independent human approval resumes the same maintained investigation and creates one real task through the UI", async ({
  page,
  browser,
}) => {
  test.setTimeout(180_000);
  expect(process.env.ACO_TEST_PROFILE).toBe("synthetic-agents");
  const password = process.env.ACO_TEST_DEMO_PASSWORD;
  if (!password) throw Error("Explicit synthetic browser password required");
  await signIn(
    page,
    password,
    `internal-approval-${randomUUID()}@example.test`,
  );
  const me = await (await page.request.get("/api/me")).json();
  const headers = {
    origin: "http://127.0.0.1:53000",
    "x-csrf-token": me.csrf_token,
  };
  const created = await page.request.post("/api/workspaces", {
    headers,
    data: { name: `Independent approval ${randomUUID()}`, timezone: "UTC" },
  });
  expect(created.status()).toBe(201);
  const workspace = (await created.json()).id as string;
  const base = `/api/workspaces/${workspace}`;
  expect(
    (
      await page.request.post(`${base}/demo/accounting/seed`, {
        headers,
        data: {},
      })
    ).status(),
  ).toBe(201);
  const invited = await page.request.post(`${base}/invitations`, {
    headers,
    data: { email: "owner@example.test", role: "finance_manager" },
  });
  expect(invited.status()).toBe(201);
  const inviteCode = (await invited.json()).invite_code as string;
  await page.goto(`/agents?workspace=${workspace}`);
  await page.getByLabel("Find invoices", { exact: true }).fill("INV-101");
  await page
    .getByRole("button", { name: "Search invoices", exact: true })
    .click();
  await page.getByLabel("Select INV-101", { exact: true }).check();
  await page
    .getByLabel("Investigation objective")
    .fill("Request approval before creating an internal task");
  const started = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `${base}/agent-runs`,
  );
  await page
    .getByRole("button", { name: "Start investigation", exact: true })
    .click();
  const response = await started;
  expect(response.status()).toBe(202);
  const runId = (await response.json()).id as string;
  const readRun = async () =>
    (await (
      await page.request.get(`${base}/agent-runs/${runId}`)
    ).json()) as RunView;
  await expect
    .poll(async () => (await readRun()).status, { timeout: 45_000 })
    .toBe("awaiting_approval");
  const pending = await readRun();
  const proposal = pending.result?.proposals.find(
    (item) => item.kind === "request_task_approval",
  );
  expect(pending.authenticity).toBe("FAKE");
  expect(proposal?.executed).toBe(false);
  expect(proposal?.changed_ids).toEqual([]);
  if (!proposal?.approval_id)
    throw Error("Persisted independent approval required");
  const approvalId = proposal.approval_id;
  const self = (await (
    await page.request.get(`${base}/approvals/${approvalId}`)
  ).json()) as ApprovalView;
  expect(self.can_approve).toBe(false);
  expect(self.execution_status).toBe("pending_approval");
  expect(self.changed_ids).toEqual([]);
  await expect(
    page.getByRole("link", {
      name: "Open approval and decision history",
      exact: true,
    }),
  ).toHaveAttribute(
    "href",
    `/review?workspace=${workspace}&approval=${approvalId}`,
  );
  const context = await browser.newContext();
  try {
    const reviewer = await context.newPage();
    await signIn(reviewer, password);
    await reviewer.getByLabel("Join code", { exact: true }).fill(inviteCode);
    await reviewer
      .getByRole("button", { name: "Accept invitation", exact: true })
      .click();
    await reviewer.goto(
      `/review?workspace=${workspace}&approval=${approvalId}`,
    );
    const approval = reviewer.getByRole("article", {
      name: `Approval ${approvalId}`,
      exact: true,
    });
    await expect(approval).toContainText("Internal task proposal");
    await expect(approval).toContainText("Awaiting independent approval");
    await approval
      .getByRole("button", { name: "Review proposal", exact: true })
      .click();
    const captured = reviewer.getByRole("region", {
      name: "Review captured proposal",
      exact: true,
    });
    await captured
      .getByLabel("Decision reason", { exact: true })
      .fill(
        "Independent Finance Manager reviewed exact internal task, invoice and genuine saved source reads. No outreach authorized.",
      );
    const decision = reviewer.waitForResponse(
      (result) =>
        result.request().method() === "POST" &&
        new URL(result.url()).pathname ===
          `${base}/approvals/${approvalId}/decisions`,
    );
    await captured
      .getByRole("button", { name: "Approve proposal", exact: true })
      .click();
    expect((await decision).status()).toBe(200);
    await expect
      .poll(async () => (await readRun()).status, { timeout: 45_000 })
      .toBe("completed");
    const final = await readRun();
    const result = final.result?.proposals.find(
      (item) => item.approval_id === approvalId,
    );
    expect(result?.executed).toBe(true);
    expect(result?.approval_status).toBe("executed");
    expect(result?.changed_ids).toHaveLength(1);
    const saved = (await (
      await reviewer.request.get(`${base}/approvals/${approvalId}`)
    ).json()) as ApprovalView;
    expect(saved.effective_status).toBe("consumed");
    expect(saved.execution_status).toBe("executed");
    expect(saved.dispatch_status).toBe("NOT_APPLICABLE");
    expect(saved.changed_ids).toEqual(result?.changed_ids);
    const taskId = saved.changed_ids?.[0];
    if (!taskId || saved.changed_ids?.length !== 1) throw new Error("Exactly one persisted approved task required");
    await expect(
      reviewer.getByRole("link", { name: "Open created task", exact: true }),
    ).toBeVisible({ timeout: 15_000 });
    await reviewer
      .getByRole("link", { name: "Open created task", exact: true })
      .click();
    await expect(reviewer).toHaveURL(
      new RegExp(`/tasks/${taskId}`),
    );
    await expect(
      reviewer.getByText(`Task reference: ${taskId}`, {
        exact: true,
      }),
    ).toBeVisible();
    await reviewer.reload();
    await expect(
      reviewer.getByText(`Task reference: ${taskId}`, {
        exact: true,
      }),
    ).toBeVisible();
    await page.reload();
    await expect(
      page.getByRole("link", {
        name: `Open task ${taskId}`,
        exact: true,
      }),
    ).toBeVisible();
    await page.setViewportSize({ width: 390, height: 844 });
    await expect
      .poll(() =>
        page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      )
      .toBe(true);
    const current = await (await page.request.get("/api/me")).json();
    expect(
      current.workspaces.find((item: { id: string }) => item.id === workspace),
    ).toMatchObject({ outbound_paused: true, automation_enabled: false });
    const communications = await page.request.get(`${base}/communications`);
    expect(communications.status()).toBe(200);
    expect((await communications.json()).total).toBe(0);
  } finally {
    await context.close();
  }
});
