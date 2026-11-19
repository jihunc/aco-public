import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import type {
  IngressReceiptView,
  IngressSetupView,
  RunView,
  PageInvoiceView,
  PromiseView,
  CaseView,
} from "../../web/src/lib/generated-api";

test("one fixed incoming original produces separate guarded promise, dispute and payment-review outcomes", async ({
  page,
}) => {
  test.setTimeout(180_000);
  page.setDefaultTimeout(15_000);
  expect(process.env.ACO_TEST_PROFILE).toBe("synthetic-inbound");
  const password = process.env.ACO_TEST_DEMO_PASSWORD;
  if (!password)
    throw new Error("Explicit synthetic browser password is required");
  await page.goto("/login");
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Username or email").fill("owner@example.test");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await expect(page).toHaveURL(/\/onboarding$/);
  const me = (await (await page.request.get("/api/me")).json()) as {
    csrf_token: string;
  };
  const headers = {
    origin: "http://127.0.0.1:53000",
    "x-csrf-token": me.csrf_token,
  };
  const created = await page.request.post("/api/workspaces", {
    headers,
    data: { name: `Three intents ${randomUUID()}`, timezone: "UTC" },
  });
  expect(created.status()).toBe(201);
  const workspace = ((await created.json()) as { id: string }).id,
    base = `/api/workspaces/${workspace}`;
  expect(
    (
      await page.request.post(`${base}/demo/accounting/seed`, {
        headers,
        data: {},
      })
    ).status(),
  ).toBe(201);
  const invoices = (await (
    await page.request.get(`${base}/receivables?page_size=100`)
  ).json()) as PageInvoiceView;
  const selected = ["INV-101", "INV-107", "INV-113"].map((number) => {
    const row = invoices.items.find((i) => i.number === number);
    if (!row) throw new Error("Canonical seed invoice missing");
    return row;
  });
  const before = invoices.items
    .map((i) => ({
      id: i.id,
      balance: i.outstanding,
      status: i.financial_status,
    }))
    .sort((a, b) => a.id.localeCompare(b.id));
  await page.goto(`/settings/email?workspace=${workspace}`);
  const inbox = page.getByRole("region", {
    name: "Synthetic inbound email",
    exact: true,
  });
  const prepared = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      r.url().endsWith(`${base}/email-ingress/setup`),
  );
  await inbox
    .getByRole("button", { name: "Prepare synthetic inbox", exact: true })
    .click();
  expect((await prepared).status()).toBe(200);
  const setup = (await (
    await page.request.get(`${base}/email-ingress/setup`)
  ).json()) as IngressSetupView;
  await inbox
    .getByRole("combobox", { name: "Demo email connection", exact: true })
    .selectOption(setup.email_connections[0].id);
  await inbox
    .getByRole("combobox", { name: "Private source storage", exact: true })
    .selectOption(setup.storage_connections[0].id);
  await inbox
    .getByRole("checkbox", {
      name: /I reviewed this synthetic private capture/,
    })
    .check();
  const enabled = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      r.url().endsWith(`${base}/email-ingress/activations`),
  );
  await inbox
    .getByRole("button", {
      name: "Enable synthetic inbound for one hour",
      exact: true,
    })
    .click();
  const activation = (await (await enabled).json()) as {
    id: string;
    version: number;
  };
  for (const row of selected) {
    await inbox
      .getByRole("checkbox", {
        name: `Select inbound ${row.number}`,
        exact: true,
      })
      .check();
  }
  await inbox
    .getByRole("combobox", { name: "Synthetic message scenario", exact: true })
    .selectOption("three_intent_reply");
  const simulated = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      r.url().endsWith(`/activations/${activation.id}/simulate`),
  );
  await inbox
    .getByRole("button", { name: "Capture synthetic sample", exact: true })
    .click();
  const response = await simulated;
  expect(response.status()).toBe(202);
  const firstReceipt = (await response.json()) as IngressReceiptView;
  const receiptPath = `${base}/email-ingress/receipts/${firstReceipt.id}`;
  await expect
    .poll(
      async () =>
        (
          (await (
            await page.request.get(receiptPath)
          ).json()) as IngressReceiptView
        ).state,
      { timeout: 60_000 },
    )
    .toBe("processed");
  const receipt = (await (
    await page.request.get(receiptPath)
  ).json()) as IngressReceiptView;
  if (!receipt.agent_run_id || !receipt.communication_id)
    throw new Error("Persisted source/run required");
  const runPath = `${base}/agent-runs/${receipt.agent_run_id}`;
  await expect
    .poll(
      async () =>
        ((await (await page.request.get(runPath)).json()) as RunView).status,
      { timeout: 60_000 },
    )
    .toBe("completed");
  const run = (await (await page.request.get(runPath)).json()) as RunView;
  expect(run.authenticity).toBe("FAKE");
  expect(run.provider_attempted).toBe(false);
  const proposals = run.result?.proposals;
  expect(proposals?.map((p) => p.kind)).toEqual([
    "record_promise",
    "upsert_case",
    "create_task",
  ]);
  expect(proposals?.every((p) => p.executed)).toBe(true);
  const promiseId = proposals?.[0].changed_ids?.[0],
    caseId = proposals?.[1].changed_ids?.[0],
    taskId = proposals?.[2].changed_ids?.[0];
  if (!promiseId || !caseId || !taskId)
    throw new Error("Actual committed result IDs required");
  await page.goto(`/agent-runs/${run.id}?workspace=${workspace}`);
  const results = page.getByRole("region", {
    name: "Investigation results",
    exact: true,
  });
  await expect(
    results.getByRole("link", {
      name: `Open promise ${promiseId}`,
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    results.getByRole("link", { name: `Open case ${caseId}`, exact: true }),
  ).toBeVisible();
  await expect(
    results.getByRole("link", { name: `Open task ${taskId}`, exact: true }),
  ).toBeVisible();
  await results
    .getByRole("link", { name: `Open promise ${promiseId}`, exact: true })
    .click();
  const review = page.getByRole("region", {
    name: "Human commitment review",
    exact: true,
  });
  await expect(review).toBeVisible();
  await review
    .getByLabel("Review reason", { exact: true })
    .fill(
      "Owner independently reviewed the exact stored synthetic commitment, date, amount and invoice scope.",
    );
  await review
    .getByLabel(
      "I reviewed the source and confirm these exact commitment terms.",
      { exact: true },
    )
    .check();
  const confirmed = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      r.url().endsWith(`/promises/${promiseId}/confirm`),
  );
  await review
    .getByRole("button", { name: "Confirm commitment", exact: true })
    .click();
  const confirmation = await confirmed;
  expect(confirmation.status()).toBe(200);
  expect(((await confirmation.json()) as PromiseView).status).toBe("ACTIVE");
  await page.reload();
  const savedPromise = (await (
    await page.request.get(`${base}/promises/${promiseId}`)
  ).json()) as PromiseView;
  expect(savedPromise.status).toBe("ACTIVE");
  expect(savedPromise.amount?.minor_units).toBe("10000");
  const savedCase = (await (
    await page.request.get(`${base}/cases/${caseId}`)
  ).json()) as CaseView;
  expect(savedCase.status).toBe("OPEN");
  expect(savedCase.issue_type).toBe("wrong_amount");
  await page.goto(`/cases/${caseId}?workspace=${workspace}`);
  await expect(
    page.getByRole("heading", { name: "Source evidence", exact: true }),
  ).toBeVisible();
  await page.goto(
    `/communications/${receipt.communication_id}?workspace=${workspace}`,
  );
  await page.getByRole("button", { name: "Open message", exact: true }).click();
  await expect(
    page.getByRole("link", {
      name: "Download authenticated source",
      exact: true,
    }),
  ).toBeVisible();
  const after = (
    (await (
      await page.request.get(`${base}/receivables?page_size=100`)
    ).json()) as PageInvoiceView
  ).items
    .map((i) => ({
      id: i.id,
      balance: i.outstanding,
      status: i.financial_status,
    }))
    .sort((a, b) => a.id.localeCompare(b.id));
  expect(after).toEqual(before);
  const session = (await (await page.request.get("/api/me")).json()) as {
    workspaces: {
      id: string;
      outbound_paused: boolean;
      automation_enabled: boolean;
    }[];
  };
  expect(session.workspaces.find((w) => w.id === workspace)).toMatchObject({
    outbound_paused: true,
    automation_enabled: false,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
});
