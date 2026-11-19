import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import type {
  InvoiceView,
  PromiseView,
  IngressReceiptView,
  IngressSetupView,
} from "../../web/src/lib/generated-api.js";

test("human multi-invoice promise and reviewed amendment preserve exact allocations and original source until confirmation", async ({
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
    data: { name: `Multi promise ${randomUUID()}`, timezone: "UTC" },
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
  const simulate = async (
    scenario: "multi_promise_reply" | "multi_promise_amendment",
  ) => {
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
  const source = await simulate("multi_promise_reply");
  const firstDate = /total by (\d{4}-\d{2}-\d{2}) in UTC/.exec(
    source.content.text_body,
  )?.[1];
  if (!firstDate)
    throw new Error("Stored exact initial commitment date required");
  for (const row of scope)
    expect(source.content.text_body).toContain(`AUD 100.00 to ${row.number}`);
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
  const fillTerms = async (
    panel: typeof capture,
    each: string,
    total: string,
    due: string,
  ) => {
    for (const row of scope)
      await panel
        .getByLabel(`Allocation for ${row.number} (AUD)`, { exact: true })
        .fill(each);
    await panel
      .getByLabel("Promised amount (AUD)", { exact: true })
      .fill(total);
    await panel.getByLabel("Promised payment date", { exact: true }).fill(due);
    await panel.getByLabel("Recipient timezone", { exact: true }).fill("UTC");
    await panel
      .getByRole("button", { name: "Review promise terms", exact: true })
      .click();
    await expect(
      panel.getByRole("region", {
        name: "Reviewed promise capture",
        exact: true,
      }),
    ).toContainText("not a payment or automatic confirmation");
  };
  await fillTerms(capture, "100.00", "200.00", firstDate);
  const firstResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `${base}/promises` &&
      response.request().method() === "POST",
  );
  await capture
    .getByRole("button", { name: "Record payment promise", exact: true })
    .click();
  const first = await firstResponse;
  expect(first.status(), await first.text()).toBe(201);
  const original = (await first.json()) as PromiseView;
  expect(original.status).toBe("NEEDS_CONFIRMATION");
  expect(original.invoice_links).toHaveLength(2);
  expect(
    original.invoice_links.every((row) => row.amount?.minor_units === "10000"),
  ).toBe(true);
  const review = page.getByRole("region", {
    name: "Human commitment review",
    exact: true,
  });
  const confirm = async () => {
    await review
      .getByLabel("Review reason", { exact: true })
      .fill(
        "I reviewed the stored synthetic statement, exact allocations, scope, date and authority.",
      );
    await review
      .getByLabel(
        "I reviewed the source and confirm these exact commitment terms.",
        { exact: true },
      )
      .check();
    const response = page.waitForResponse(
      (result) =>
        result.url().includes("/confirm") &&
        result.request().method() === "POST",
    );
    await review
      .getByRole("button", { name: "Confirm commitment", exact: true })
      .click();
    const saved = await response;
    expect(saved.status(), await saved.text()).toBe(200);
    return {
      record: (await saved.json()) as PromiseView,
      body: saved.request().postDataJSON() as {
        replacement_expected_version?: number;
      },
    };
  };
  const active = (await confirm()).record;
  expect(active.status).toBe("ACTIVE");
  const revisedSource = await simulate("multi_promise_amendment");
  const revisedDate = /total by (\d{4}-\d{2}-\d{2}) in UTC/.exec(
    revisedSource.content.text_body,
  )?.[1];
  if (!revisedDate) throw new Error("Stored exact amendment date required");
  await page.goto(`/promises/${active.id}?workspace=${workspace}`);
  const amendment = page.getByRole("region", {
    name: "Record source-backed amendment",
    exact: true,
  });
  await amendment
    .getByRole("combobox", { name: "Promise source message", exact: true })
    .selectOption(revisedSource.id);
  await fillTerms(amendment, "75.00", "150.00", revisedDate);
  const amendmentResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `${base}/promises` &&
      response.request().method() === "POST",
  );
  await amendment
    .getByRole("button", { name: "Record proposed amendment", exact: true })
    .click();
  const amendedResponse = await amendmentResponse;
  expect(amendedResponse.status(), await amendedResponse.text()).toBe(201);
  const proposed = (await amendedResponse.json()) as PromiseView;
  expect(proposed.status).toBe("NEEDS_CONFIRMATION");
  expect(proposed.replaces_promise_id).toBe(active.id);
  expect(proposed.source_communication_id).toBe(revisedSource.id);
  const read = async (id: string) =>
    (await (
      await page.request.get(`${base}/promises/${id}`)
    ).json()) as PromiseView;
  const unchanged = await read(active.id);
  expect(unchanged.status).toBe("ACTIVE");
  expect(unchanged.source_text).toBe(source.content.text_body);
  expect(unchanged.source_communication_id).toBe(source.id);
  // An actual stale reviewed predecessor version must not supersede anything.
  const refused = await page.request.post(
    `${base}/promises/${proposed.id}/confirm`,
    {
      headers,
      data: {
        promise_id: proposed.id,
        expected_version: proposed.version,
        replacement_expected_version: unchanged.version + 1,
        confirmation_evidence: {
          communication_id: revisedSource.id,
          excerpt: revisedSource.content.text_body,
          offset: 0,
        },
        reason:
          "Adversarial stale original-version probe in an explicitly synthetic browser workspace.",
      },
    },
  );
  expect(refused.status()).toBe(409);
  expect((await read(active.id)).status).toBe("ACTIVE");
  expect((await read(proposed.id)).status).toBe("NEEDS_CONFIRMATION");
  await expect(review).toContainText(
    `Reviewed original version ${unchanged.version}`,
  );
  const confirmed = await confirm();
  expect(confirmed.body.replacement_expected_version).toBe(unchanged.version);
  expect(confirmed.record.status).toBe("ACTIVE");
  expect(
    confirmed.record.invoice_links.every(
      (row) => row.amount?.minor_units === "7500",
    ),
  ).toBe(true);
  const superseded = await read(active.id);
  expect(superseded.status).toBe("SUPERSEDED");
  expect(superseded.superseded_by_id).toBe(proposed.id);
  expect(superseded.source_text).toBe(source.content.text_body);
  await page.reload();
  await expect(
    page.getByRole("link", { name: "Prior commitment", exact: true }),
  ).toHaveAttribute("href", `/promises/${active.id}?workspace=${workspace}`);
  await expect(page.locator(".agent-business-record")).toContainText(
    "AUD 150.00",
  );
  const afterInvoices = (await (
    await page.request.get(`${base}/receivables?page_size=100`)
  ).json()) as { items: InvoiceView[] };
  for (const invoice of scope) {
    const current = afterInvoices.items.find((row) => row.id === invoice.id);
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
