import { expect, test, type Page } from "@playwright/test";
import { existsSync, readFileSync } from "node:fs";
import type {
  InvoiceWorkspace,
  RunView,
  TaskView,
} from "../../web/src/lib/generated-api";
const env = Object.fromEntries(
  (existsSync(".env") ? readFileSync(".env", "utf8") : "")
    .split("\n")
    .filter((x) => x.includes("="))
    .map((x) => [x.slice(0, x.indexOf("=")), x.slice(x.indexOf("=") + 1)]),
);
async function setup(page: Page) {
  await page.goto("/login");
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Username or email").fill("owner@example.test");
  await page
    .getByLabel("Password", { exact: true })
    .fill(process.env.ACO_TEST_DEMO_PASSWORD || env.DEMO_PASSWORD);
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await expect(page).toHaveURL(/\/onboarding$/);
  const me = await (await page.request.get("/api/me")).json();
  const headers = {
    origin: "http://127.0.0.1:53000",
    "x-csrf-token": me.csrf_token,
  };
  const created = await page.request.post("/api/workspaces", {
    headers,
    data: {
      name: `Agent browser ${Date.now()}`,
      timezone: "Australia/Melbourne",
    },
  });
  expect(created.status()).toBe(201);
  const workspace = (await created.json()).id;
  expect(
    (
      await page.request.post(
        `/api/workspaces/${workspace}/demo/accounting/seed`,
        { headers, data: {} },
      )
    ).status(),
  ).toBe(201);
  return { workspace, headers };
}
test("investigation starts from source invoices, survives navigation and reloads persisted evidence", async ({
  page,
}) => {
  // Includes a bounded 45s worker poll, real sign-in and persisted reloads.
  test.setTimeout(120_000);
  const { workspace } = await setup(page);
  await page.goto(`/agents?workspace=${workspace}`);
  await expect(
    page.getByRole("heading", { name: "AI investigations", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Start investigation", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("Find invoices", { exact: true }).fill("INV-10");
  await page
    .getByRole("button", { name: "Search invoices", exact: true })
    .click();
  await page.getByLabel("Select INV-101", { exact: true }).check();
  await page.getByLabel("Select INV-102", { exact: true }).check();
  await page.getByLabel("Select INV-103", { exact: true }).check();
  await expect(
    page.getByLabel("Select INV-104", { exact: true }),
  ).toBeDisabled();
  await page
    .getByLabel("Investigation objective")
    .fill(
      "Inspect the selected invoice, verified payment and communication evidence. Create an internal task to review missing remittance evidence.",
    );
  await page
    .getByRole("button", { name: "Start investigation", exact: true })
    .focus();
  const started = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      r.url().endsWith(`/api/workspaces/${workspace}/agent-runs`),
  );
  await page.keyboard.press("Enter");
  const response = await started;
  expect(response.status()).toBe(202);
  const run = await response.json();
  await expect(page).toHaveURL(new RegExp(`/agent-runs/${run.id}`));
  // Navigating away drops the browser observer; it must not cancel the durable job.
  await page.goto(`/receivables?workspace=${workspace}`);
  await expect
    .poll(
      async () =>
        (
          await (
            await page.request.get(
              `/api/workspaces/${workspace}/agent-runs/${run.id}`,
            )
          ).json()
        ).status,
      { timeout: 45000 },
    )
    .toBe("completed");
  const committed = await (
    await page.request.get(`/api/workspaces/${workspace}/agent-runs/${run.id}`)
  ).json();
  const taskOutcomes = committed.result.proposals.filter(
    (proposal: { kind: string; executed: boolean }) =>
      proposal.kind === "create_task" && proposal.executed,
  );
  expect(taskOutcomes.length).toBeGreaterThan(0);
  const taskId = taskOutcomes[0].changed_ids[0];
  const actualTask = await (
    await page.request.get(`/api/workspaces/${workspace}/tasks/${taskId}`)
  ).json();
  expect(actualTask.id).toBe(taskId);
  expect(actualTask.deleted_at).toBeNull();
  await page.goto(`/agent-runs/${run.id}?workspace=${workspace}`);
  await expect(
    page.getByText("Simulated model", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Findings & outcomes" }),
  ).toBeVisible();
  const history = await (
    await page.request.get(
      `/api/workspaces/${workspace}/agent-runs/${run.id}/steps`,
    )
  ).json();
  expect(
    history.items.filter(
      (s: { kind: string; status: string }) =>
        s.kind === "read" && s.status === "completed",
    ).length,
  ).toBeGreaterThanOrEqual(2);
  for (const step of history.items)
    await expect(page.locator(`#agent-step-${step.id}`)).toHaveCount(1);
  await page.getByText("Inspect saved input and output").first().click();
  await expect(
    page.getByRole("heading", { name: "Validated input" }).first(),
  ).toBeVisible();
  await page.reload();
  for (const step of history.items)
    await expect(page.locator(`#agent-step-${step.id}`)).toHaveCount(1);
  await expect(
    page.getByRole("button", { name: "Execute", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("link", { name: `Open task ${taskId}`, exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: actualTask.title, exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByText(`Task reference: ${taskId}`, { exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.goto(`/agents?workspace=${workspace}`);
  await expect(
    page.getByRole("link", { name: run.objective, exact: true }),
  ).toBeVisible();
});
test("membership errors hide stale investigation data and offer explicit retry", async ({
  page,
}) => {
  const { workspace } = await setup(page);
  await page.route(`**/api/workspaces/${workspace}/agent-runs`, (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({ detail: "FORBIDDEN" }),
    }),
  );
  await page.goto(`/agents?workspace=${workspace}`);
  await expect(
    page.getByRole("alert").filter({ hasText: "Investigations unavailable" }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "This investigation is unavailable to your current membership.",
      { exact: false },
    ),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
});

test("explicit simulated task update uses saved current evidence and refuses a stale objective", async ({
  page,
}) => {
  test.setTimeout(180_000);
  expect(process.env.ACO_TEST_PROFILE).toBe("synthetic-agents");
  const { workspace } = await setup(page);
  const base = `/api/workspaces/${workspace}`;
  async function start(objective: string): Promise<RunView> {
    await page.goto(`/agents?workspace=${workspace}`);
    await page.getByLabel("Find invoices", { exact: true }).fill("INV-101");
    await page
      .getByRole("button", { name: "Search invoices", exact: true })
      .click();
    await page.getByLabel("Select INV-101", { exact: true }).check();
    await page.getByLabel("Investigation objective").fill(objective);
    const pending = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname === `${base}/agent-runs`,
    );
    await page
      .getByRole("button", { name: "Start investigation", exact: true })
      .click();
    const response = await pending;
    expect(response.status()).toBe(202);
    const launched = (await response.json()) as RunView;
    await expect
      .poll(
        async () => {
          const value = (await (
            await page.request.get(`${base}/agent-runs/${launched.id}`)
          ).json()) as RunView;
          return value.status;
        },
        { timeout: 45_000 },
      )
      .toMatch(/^(completed|failed)$/);
    return (await (
      await page.request.get(`${base}/agent-runs/${launched.id}`)
    ).json()) as RunView;
  }
  // The initial linked task also comes from the real normal UI/worker; no task-row fixture.
  const initial = await start(
    "Create an internal task to review imported evidence",
  );
  expect(initial.status).toBe("completed");
  const taskId = initial.result?.proposals.find(
    (item) => item.kind === "create_task" && item.executed,
  )?.changed_ids?.[0];
  if (!taskId) throw Error("Actual created task outcome required");
  const readTask = async () =>
    (await (
      await page.request.get(`${base}/tasks/${taskId}`)
    ).json()) as TaskView;
  const original = await readTask();
  if (!original.invoice_id)
    throw Error("Actual selected-invoice task required");
  const source = (await (
    await page.request.get(`${base}/receivables/${original.invoice_id}`)
  ).json()) as InvoiceWorkspace;
  // Preserve a genuine human edit, including urgency, across the later agent status change.
  await page.goto(`/review?workspace=${workspace}`);
  await page
    .getByRole("button", { name: `Edit ${original.title}`, exact: true })
    .click();
  const form = page.getByRole("region", { name: "Edit task", exact: true });
  await form
    .getByRole("textbox", { name: "Notes", exact: true })
    .fill(
      "Human source-review note retained through simulated task completion.",
    );
  await form
    .getByRole("combobox", { name: "Priority", exact: true })
    .selectOption("3");
  const edited = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" &&
      new URL(response.url()).pathname === `${base}/tasks/${taskId}`,
  );
  await form.getByRole("button", { name: "Save task", exact: true }).click();
  expect((await edited).status()).toBe(200);
  const before = await readTask();
  expect(before.priority).toBe(3);
  const objective = `Update task ${taskId} version ${before.version} to done`;
  const updated = await start(objective);
  expect(updated.status).toBe("completed");
  expect(updated.authenticity).toBe("FAKE");
  expect(updated.provider_attempted).toBe(false);
  const outcome = updated.result?.proposals.find(
    (item) => item.kind === "update_collection_task",
  );
  expect(outcome?.executed).toBe(true);
  expect(outcome?.changed_ids).toEqual([taskId]);
  await expect(
    page.getByText("Simulated model", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Task fields updated.", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Task completion does not resolve a case or verify payment.",
      { exact: false },
    ),
  ).toBeVisible();
  const steps = await (
    await page.request.get(`${base}/agent-runs/${updated.id}/steps`)
  ).json();
  const taskRead = steps.items.find(
    (step: { tool_name: string }) => step.tool_name === "list_collection_tasks",
  );
  expect(taskRead?.status).toBe("completed");
  expect(
    taskRead.output.records.find(
      (fact: { task_id: string }) => fact.task_id === taskId,
    ).version,
  ).toBe(before.version);
  await page
    .getByRole("link", { name: `Open task ${taskId}`, exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: before.title, exact: true }),
  ).toBeVisible();
  await expect(page.getByText("done", { exact: true })).toBeVisible();
  await page.reload();
  await expect(
    page.getByText(`Task reference: ${taskId}`, { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(before.notes, { exact: true })).toBeVisible();
  const after = await readTask();
  expect(after.version).toBe(before.version + 1);
  expect(after.status).toBe("done");
  for (const key of [
    "title",
    "notes",
    "priority",
    "priority_rank",
    "assignee_id",
    "due_at",
    "source_action_id",
    "creator_id",
    "invoice_id",
  ] as const)
    expect(after[key]).toEqual(before[key]);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 1280, height: 720 });
  const stale = await start(objective);
  expect(stale.status).toBe("failed");
  expect(stale.provider_attempted).toBe(false);
  await expect(
    page.getByText("Investigation stopped:", { exact: false }),
  ).toBeVisible();
  expect(await readTask()).toEqual(after);
  expect(
    (await (await page.request.get(`${base}/tasks`)).json()).items,
  ).toHaveLength(1);
  const currentSource = (await (
    await page.request.get(`${base}/receivables/${original.invoice_id}`)
  ).json()) as InvoiceWorkspace;
  expect(currentSource.invoice.outstanding).toEqual(source.invoice.outstanding);
  expect(currentSource.invoice.financial_status).toBe(
    source.invoice.financial_status,
  );
  expect(currentSource.invoice.source).toEqual(source.invoice.source);
  expect(
    (await (await page.request.get(`${base}/communications`)).json()).items,
  ).toHaveLength(0);
});
