import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import type {
  InvoiceView,
  PromiseView,
  IngressReceiptView,
  IngressSetupView,
} from "../../web/src/lib/generated-api.js";

test("human source-stated installment plan retains explicit invoice portions, due checks and source facts after reload", async ({
  page,
}) => {
  test.setTimeout(150_000);
  expect(process.env.ACO_TEST_PROFILE).toBe("synthetic-inbound");
  const password = process.env.ACO_TEST_DEMO_PASSWORD;
  if (!password)
    throw new Error("Explicit synthetic browser password required");
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
    data: { name: `Installment promise ${randomUUID()}`, timezone: "UTC" },
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
  const rows = (await (
    await page.request.get(`${base}/receivables?page_size=100`)
  ).json()) as { items: InvoiceView[] };
  const invoices = ["INV-101", "INV-107"].map((number) =>
    rows.items.find((row) => row.number === number),
  );
  if (!invoices[0] || !invoices[1])
    throw new Error("Actual verified same-customer seed invoices required");
  const scope = [invoices[0], invoices[1]];
  expect(scope[0].customer.id).toBe(scope[1].customer.id);
  const prepared = await page.request.post(`${base}/email-ingress/setup`, {
    headers,
    data: {
      request_id: randomUUID(),
      evidence:
        "Owner explicitly prepares synthetic multi-invoice source capture without outreach.",
    },
  });
  expect(prepared.status()).toBe(200);
  const setup = (await prepared.json()) as IngressSetupView;
  const activated = await page.request.post(
    `${base}/email-ingress/activations`,
    {
      headers,
      data: {
        email_connection_id: setup.email_connections[0].id,
        storage_connection_id: setup.storage_connections[0].id,
        evidence:
          "Owner authorizes only fixed synthetic samples and private original preservation.",
        expires_in_seconds: 3600,
      },
    },
  );
  expect(activated.status()).toBe(200);
  const activation = (await activated.json()) as {
    id: string;
    version: number;
  };
  const simulate = async (scenario: "installment_promise_reply") => {
    const response = await page.request.post(
      `${base}/email-ingress/activations/${activation.id}/simulate`,
      {
        headers,
        data: {
          request_id: randomUUID(),
          expected_activation_version: activation.version,
          invoice_ids: scope.map((row) => row.id),
          scenario,
        },
      },
    );
    expect(response.status(), await response.text()).toBe(202);
    const receipt = (await response.json()) as IngressReceiptView;
    let stored: IngressReceiptView = receipt;
    await expect
      .poll(
        async () => {
          const result = await page.request.get(
            `${base}/email-ingress/receipts/${receipt.id}`,
          );
          expect(result.status()).toBe(200);
          stored = (await result.json()) as IngressReceiptView;
          return stored.state;
        },
        { timeout: 30_000 },
      )
      .toBe("processed");
    const source = (await (
      await page.request.get(
        `${base}/communications/${stored.communication_id}`,
      )
    ).json()) as {
      id: string;
      source_state: string;
      synthetic: boolean;
      content: { text_body: string };
      invoice_links: { invoice_id: string }[];
    };
    expect(source.source_state).toBe("stored");
    expect(source.synthetic).toBe(true);
    expect(source.invoice_links.map((row) => row.invoice_id).sort()).toEqual(
      scope.map((row) => row.id).sort(),
    );
    return source;
  };
  const source = await simulate("installment_promise_reply");
  const dates = [
    ...source.content.text_body.matchAll(
      /Installment [12]: AUD 100.00 total by (\d{4}-\d{2}-\d{2}) in UTC/g,
    ),
  ].map((match) => match[1]);
  expect(dates).toHaveLength(2);
  for (const row of scope) {
    expect(source.content.text_body).toContain(`AUD 100.00 to ${row.number}`);
    expect(
      source.content.text_body.match(
        new RegExp(`AUD 50\\.00 to ${row.number}`, "g"),
      ),
    ).toHaveLength(2);
  }
  await page.goto(`/receivables/${scope[0].id}?workspace=${workspace}`);
  const capture = page.getByRole("region", {
    name: "Record source-backed promise",
    exact: true,
  });
  await capture
    .getByRole("combobox", { name: "Promise source message", exact: true })
    .selectOption(source.id);
  await capture
    .getByLabel(`Include ${scope[1].number}`, { exact: true })
    .check();
  for (const row of scope)
    await capture
      .getByLabel(`Allocation for ${row.number} (AUD)`, { exact: true })
      .fill("100.00");
  await capture
    .getByLabel("Promised amount (AUD)", { exact: true })
    .fill("200.00");
  await capture
    .getByLabel("Promised payment date", { exact: true })
    .fill(dates[1]);
  await capture.getByLabel("Recipient timezone", { exact: true }).fill("UTC");
  for (let index = 0; index < 2; index++) {
    await capture
      .getByRole("button", { name: "Add source installment", exact: true })
      .click();
    await capture
      .getByLabel(`Installment ${index + 1} date`, { exact: true })
      .fill(dates[index]);
    await capture
      .getByLabel(`Installment ${index + 1} amount (AUD)`, { exact: true })
      .fill("100.00");
    for (const row of scope)
      await capture
        .getByLabel(
          `Installment ${index + 1} allocation for ${row.number} (AUD)`,
          { exact: true },
        )
        .fill("50.00");
  }
  await capture
    .getByRole("button", { name: "Review promise terms", exact: true })
    .click();
  const reviewed = capture.getByRole("region", {
    name: "Reviewed installment plan",
    exact: true,
  });
  for (const date of dates) await expect(reviewed).toContainText(date);
  await expect(reviewed).toContainText("AUD 50.00");
  const recordedResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `${base}/promises` &&
      response.request().method() === "POST",
  );
  await capture
    .getByRole("button", { name: "Record payment promise", exact: true })
    .click();
  const recorded = await recordedResponse;
  expect(recorded.status(), await recorded.text()).toBe(201);
  const proposed = (await recorded.json()) as PromiseView;
  expect(proposed.status).toBe("NEEDS_CONFIRMATION");
  expect(proposed.installments).toHaveLength(2);
  const human = page.getByRole("region", {
    name: "Human commitment review",
    exact: true,
  });
  await human
    .getByLabel("Review reason", { exact: true })
    .fill(
      "I independently reviewed the exact stored installment dates, amounts and each invoice portion.",
    );
  await human
    .getByLabel(
      "I reviewed the source and confirm these exact commitment terms.",
      { exact: true },
    )
    .check();
  const confirmationResponse = page.waitForResponse(
    (response) =>
      response.url().includes("/confirm") &&
      response.request().method() === "POST",
  );
  await human
    .getByRole("button", { name: "Confirm commitment", exact: true })
    .click();
  const confirmed = await confirmationResponse;
  expect(confirmed.status(), await confirmed.text()).toBe(200);
  const active = (await confirmed.json()) as PromiseView;
  expect(active.status).toBe("ACTIVE");
  expect(active.source_communication_id).toBe(source.id);
  expect(active.source_text).toBe(source.content.text_body);
  expect(active.installments).toHaveLength(2);
  for (const [index, row] of active.installments.entries()) {
    expect(row.due_date).toBe(dates[index]);
    expect(row.amount.minor_units).toBe("10000");
    expect(row.allocations.map((part) => part.invoice_id).sort()).toEqual(
      scope.map((invoice) => invoice.id).sort(),
    );
    expect(
      row.allocations.every((part) => part.amount.minor_units === "5000"),
    ).toBe(true);
  }
  const plans = (await (
    await page.request.get(`${base}/promises/${active.id}/check-plans`)
  ).json()) as {
    installment_id: string | null;
    due_at: string;
    action_ids: string[];
    invoice_ids: string[];
  }[];
  expect(plans).toHaveLength(2);
  expect(plans.every((plan) => plan.action_ids.length > 0)).toBe(true);
  expect(plans.map((plan) => plan.installment_id).sort()).toEqual(
    active.installments.map((row) => row.id).sort(),
  );
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Installments", exact: true }),
  ).toBeVisible();
  for (const date of dates)
    await expect(page.locator(".agent-business-record")).toContainText(date);
  const reread = (await (
    await page.request.get(`${base}/promises/${active.id}`)
  ).json()) as PromiseView;
  expect(reread.installments).toEqual(active.installments);
  const after = (await (
    await page.request.get(`${base}/receivables?page_size=100`)
  ).json()) as { items: InvoiceView[] };
  for (const invoice of scope) {
    const current = after.items.find((row) => row.id === invoice.id);
    expect(current?.outstanding).toEqual(invoice.outstanding);
    expect(current?.financial_status).toBe(invoice.financial_status);
  }
  const current = (await (await page.request.get("/api/me")).json()) as {
    workspaces: {
      id: string;
      outbound_paused: boolean;
      automation_enabled: boolean;
    }[];
  };
  const member = current.workspaces.find((row) => row.id === workspace);
  expect(member?.outbound_paused).toBe(true);
  expect(member?.automation_enabled).toBe(false);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(
      () =>
        page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      { timeout: 5000 },
    )
    .toBe(true);
});
