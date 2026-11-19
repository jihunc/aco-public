import { expect, test, type Page } from "@playwright/test";
import { randomUUID } from "node:crypto";
import type {
  InvoiceWorkspace,
  PageInvoiceView,
  ResetRequest,
  ResetResult,
  RunView,
  WorkspaceView,
} from "../../web/src/lib/generated-api";

async function createTask(page: Page, workspace: string, objective: string) {
  const base = `/api/workspaces/${workspace}`;
  await page.goto(`/agents?workspace=${workspace}`);
  await page.getByLabel("Find invoices", { exact: true }).fill("INV-101");
  await page
    .getByRole("button", { name: "Search invoices", exact: true })
    .click();
  await page.getByLabel("Select INV-101", { exact: true }).check();
  await page.getByLabel("Investigation objective").fill(objective);
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
  const run = (await response.json()) as RunView;
  expect(run.authenticity).toBe("FAKE");
  await expect
    .poll(
      async () =>
        (
          (await (
            await page.request.get(`${base}/agent-runs/${run.id}`)
          ).json()) as RunView
        ).status,
      { timeout: 45_000 },
    )
    .toBe("completed");
  const saved = (await (
    await page.request.get(`${base}/agent-runs/${run.id}`)
  ).json()) as RunView;
  const taskId = saved.result?.proposals.find(
    (proposal) => proposal.kind === "create_task" && proposal.executed,
  )?.changed_ids?.[0];
  if (!taskId)
    throw new Error("Maintained fake worker did not persist a canonical task");
  expect((await page.request.get(`${base}/tasks/${taskId}`)).status()).toBe(
    200,
  );
  return { runId: run.id, taskId };
}

test("Owner restores source through captured reset, preserves history/retry, and repeats a maintained fake investigation", async ({
  page,
}) => {
  test.setTimeout(180_000);
  expect(process.env.ACO_TEST_PROFILE).toBe("synthetic-agents");
  const password = process.env.ACO_TEST_DEMO_PASSWORD;
  if (!password)
    throw new Error("Explicit synthetic browser password required");
  await page.goto("/login");
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Username or email").fill("owner@example.test");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await expect(page).toHaveURL(/\/onboarding$/);
  const name = `Reset source ${randomUUID()}`;
  await page.getByLabel("Workspace name", { exact: true }).fill(name);
  await page.getByLabel("Timezone", { exact: true }).fill("UTC");
  const created = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === "/api/workspaces",
  );
  await page
    .getByRole("button", { name: "Create demo workspace", exact: true })
    .click();
  const response = await created;
  expect(response.status()).toBe(201);
  const workspace = (await response.json()) as WorkspaceView;
  const base = `/api/workspaces/${workspace.id}`;
  const source = page.getByRole("region", {
    name: "Synthetic accounting setup",
    exact: true,
  });
  await expect(source).toContainText(
    "0 imported invoices · 0 imported customers",
  );
  const importedSource = page.waitForResponse(
    (result) =>
      result.request().method() === "POST" &&
      new URL(result.url()).pathname === `${base}/demo/accounting/seed`,
  );
  await source
    .getByRole("button", { name: "Import synthetic demo data", exact: true })
    .click();
  expect((await importedSource).status()).toBe(201);
  await expect(source).toContainText(
    "24 imported invoices · 6 imported customers",
  );
  const initial = (await (
    await page.request.get(`${base}/receivables?page_size=100`)
  ).json()) as PageInvoiceView;
  const invoice = initial.items.find((row) => row.number === "INV-101");
  if (!invoice)
    throw new Error("Canonical invoice missing after actual UI import");
  expect(BigInt(invoice.outstanding.minor_units)).toBe(10000n);
  const first = await createTask(
    page,
    workspace.id,
    "Inspect INV-101 evidence and create an internal task for the first synthetic story",
  );
  await page.goto(`/receivables/${invoice.id}?workspace=${workspace.id}`);
  const payment = page.getByRole("region", {
    name: "Synthetic source payment",
    exact: true,
  });
  await payment
    .getByLabel("Demo payment amount (AUD)", { exact: true })
    .fill("25.00");
  const paid = page.waitForResponse(
    (result) =>
      result.request().method() === "POST" &&
      new URL(result.url()).pathname ===
        `${base}/demo/accounting/simulate-payment`,
  );
  await payment
    .getByRole("button", {
      name: "Record synthetic source payment",
      exact: true,
    })
    .click();
  expect((await paid).status()).toBe(200);
  await expect(
    page.getByText("Financial: partially_paid", { exact: true }),
  ).toBeVisible();
  const me = (await (await page.request.get("/api/me")).json()) as {
    csrf_token: string;
  };
  const headers = {
    origin: "http://127.0.0.1:53000",
    "x-csrf-token": me.csrf_token,
  };
  async function onboarding() {
    await page.goto("/onboarding");
    await page
      .getByRole("combobox", { name: "Workspace", exact: true })
      .selectOption(workspace.id);
    await expect(
      page.getByRole("heading", { name, exact: true }),
    ).toBeVisible();
  }
  await onboarding();
  const reset = page.getByRole("region", {
    name: "Synthetic demo reset",
    exact: true,
  });
  const evidence =
    "Owner reviewed preserved tasks, holds, uncertainty and synthetic source restoration";
  async function capture() {
    await reset
      .getByLabel("Reset review evidence", { exact: true })
      .fill(evidence);
    await reset
      .getByLabel(
        "I reviewed that business history, holds and uncertain effects remain, and outbound will be paused.",
        { exact: true },
      )
      .check();
    await reset
      .getByRole("button", { name: "Capture reset review", exact: true })
      .click();
  }
  await capture();
  const captured = reset.getByLabel("Captured reset review", { exact: true });
  const originalReview = await captured.locator("p").allTextContents();
  // A real concurrent Owner settings write changes generation; it does not change this browser's captured command.
  const changed = await page.request.patch(base, {
    headers,
    data: { name, timezone: "UTC" },
  });
  expect(changed.status()).toBe(200);
  const stale = page.waitForResponse(
    (result) =>
      result.request().method() === "POST" &&
      new URL(result.url()).pathname === `${base}/demo/reset`,
  );
  await reset
    .getByRole("button", { name: "Reset synthetic source", exact: true })
    .click();
  expect((await stale).status()).toBe(409);
  await expect(reset.getByRole("alert")).toContainText(
    "Refresh your reset review",
  );
  expect(
    (await captured.locator("p").allTextContents()).slice(
      0,
      originalReview.length,
    ),
  ).toEqual(originalReview);
  await reset
    .getByRole("button", { name: "Refresh reset review", exact: true })
    .click();
  await expect(captured).toHaveCount(0);
  await expect(
    reset.getByLabel("Reset review evidence", { exact: true }),
  ).toHaveValue("");
  await capture();
  let committed: ResetResult | null = null,
    command: ResetRequest | null = null;
  await page.route(
    `**${base}/demo/reset`,
    async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      command = route.request().postDataJSON() as ResetRequest;
      const saved = await route.fetch();
      expect(saved.status()).toBe(200);
      committed = (await saved.json()) as ResetResult;
      await route.abort("failed");
    },
    { times: 1 },
  );
  await reset
    .getByRole("button", { name: "Reset synthetic source", exact: true })
    .click();
  await expect(
    reset.getByRole("button", { name: "Retry captured reset", exact: true }),
  ).toBeEnabled();
  await expect(reset.getByRole("alert")).toContainText("unconfirmed");
  if (!committed || !command)
    throw new Error(
      "Actual reset must commit before browser confirmation is lost",
    );
  const savedResult = committed as ResetResult,
    savedCommand = command as ResetRequest;
  expect(savedResult.source_restoration).toBe("RESTORED");
  expect(savedResult.restored_invoice_ids?.slice().sort()).toEqual(
    initial.items.map((row) => row.id).sort(),
  );
  await page.reload();
  await page
    .getByRole("combobox", { name: "Workspace", exact: true })
    .selectOption(workspace.id);
  await expect(captured).toContainText(savedCommand.request_id);
  await expect(captured).toContainText(
    `Reviewed generation ${savedCommand.expected_generation}`,
  );
  const retry = page.waitForResponse(
    (result) =>
      result.request().method() === "POST" &&
      new URL(result.url()).pathname === `${base}/demo/reset`,
  );
  await reset
    .getByRole("button", { name: "Retry captured reset", exact: true })
    .click();
  const retried = await retry;
  expect(retried.status()).toBe(200);
  expect(retried.request().postDataJSON()).toEqual(savedCommand);
  expect(await retried.json()).toEqual(savedResult);
  await expect(
    reset.getByText("Synthetic accounting seed restored", { exact: true }),
  ).toBeVisible();
  await expect(reset).toContainText(
    "24 seed invoices · 6 seed customers restored",
  );
  await page.reload();
  await page
    .getByRole("combobox", { name: "Workspace", exact: true })
    .selectOption(workspace.id);
  await expect(reset).toContainText(savedCommand.request_id);
  await expect(reset).toContainText("collection holds");
  const restored = (await (
    await page.request.get(`${base}/receivables/${invoice.id}`)
  ).json()) as InvoiceWorkspace;
  expect(restored.invoice.id).toBe(invoice.id);
  expect(BigInt(restored.invoice.outstanding.minor_units)).toBe(10000n);
  expect(restored.invoice.financial_status).toBe("open");
  expect(
    (await page.request.get(`${base}/tasks/${first.taskId}`)).status(),
  ).toBe(200);
  const execution = (await (
    await page.request.get(`${base}/email-execution`)
  ).json()) as { system_enabled: boolean };
  expect(execution.system_enabled).toBe(false);
  const currentMe = (await (await page.request.get("/api/me")).json()) as {
    workspaces: WorkspaceView[];
  };
  const current = currentMe.workspaces.find((row) => row.id === workspace.id);
  expect(current?.outbound_paused).toBe(true);
  expect(current?.automation_enabled).toBe(false);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() =>
      page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    )
    .toBe(true);
  const second = await createTask(
    page,
    workspace.id,
    "Inspect restored INV-101 evidence and create an internal task for the second synthetic story",
  );
  expect(second.taskId).not.toBe(first.taskId);
  expect(
    (await page.request.get(`${base}/tasks/${first.taskId}`)).status(),
  ).toBe(200);
  await page.goto(`/agent-runs/${second.runId}?workspace=${workspace.id}`);
  await expect(
    page.getByText("Simulated model", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("link", { name: `Open task ${second.taskId}`, exact: true }),
  ).toBeVisible();
});
