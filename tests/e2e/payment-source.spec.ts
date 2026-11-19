import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";

type Money = { minor_units: string; currency: string; exponent: number };
type Invoice = {
  id: string;
  number: string;
  financial_status: string;
  collection_lifecycle: string | null;
  verified: boolean;
  accounting_provider: string;
  outstanding: Money;
  source: {
    connection_id: string;
    external_id: string;
    revision: string | null;
    synthetic: boolean;
  };
};
type PaymentResult = {
  event_id: string;
  invoice_id: string;
  financial_status: string;
  collection_lifecycle: string;
  source_revision: string;
  synthetic: boolean;
};
type PaymentCommand = {
  provider_connection_id: string;
  external_invoice_id: string;
  amount: { minor_units: number; currency: string; exponent: number };
  operation_id: string;
};
const decimal = (minor: string, exponent: number) => {
  const digits = minor.padStart(exponent + 1, "0");
  return exponent
    ? `${digits.slice(0, -exponent)}.${digits.slice(-exponent)}`
    : digits;
};
const display = (money: Money) => {
  const minor = BigInt(money.minor_units),
    scale = 10n ** BigInt(money.exponent);
  return `${money.currency} ${new Intl.NumberFormat("en-AU", { maximumFractionDigits: 0 }).format(minor / scale)}${money.exponent ? `.${(minor % scale).toString().padStart(money.exponent, "0")}` : ""}`;
};

test("demo source payment survives lost confirmation and reload, then updates persisted invoice lifecycle and Overview", async ({
  page,
}) => {
  test.setTimeout(120_000);
  expect(process.env.ACO_TEST_PROFILE ?? "mock").toBe("mock");
  const password = process.env.ACO_TEST_DEMO_PASSWORD;
  if (!password)
    throw new Error("Explicit mock browser test password is required");
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
    data: {
      name: `Source payment browser ${randomUUID()}`,
      timezone: "Australia/Melbourne",
    },
  });
  expect(created.status()).toBe(201);
  const workspace = ((await created.json()) as { id: string }).id;
  const base = `/api/workspaces/${workspace}`;
  expect(
    (
      await page.request.post(`${base}/demo/accounting/seed`, {
        headers,
        data: {},
      })
    ).status(),
  ).toBe(201);
  const all = (await (
    await page.request.get(`${base}/receivables?page_size=100`)
  ).json()) as { items: Invoice[] };
  const invoice = all.items.find((row) => row.number === "INV-101");
  if (!invoice) throw new Error("Actual seeded invoice unavailable");
  expect(invoice.verified).toBe(true);
  expect(invoice.source.synthetic).toBe(true);
  expect(invoice.accounting_provider).toBe("demo");
  expect(invoice.financial_status).toBe("open");
  const initial = (await (
    await page.request.get(`${base}/overview`)
  ).json()) as { currencies: { currency: string; outstanding: Money }[] };
  const group = initial.currencies.find(
    (row) => row.currency === invoice.outstanding.currency,
  );
  if (!group)
    throw new Error("Actual source-backed currency total unavailable");
  const reminders = (await (
    await page.request.get(`${base}/scheduled-actions?kind=policy_due`)
  ).json()) as { id: string; invoice_id: string; status: string }[];
  expect(reminders.filter((row) => row.invoice_id === invoice.id)).toHaveLength(
    0,
  );

  // Prime Overview through the product before the payment so its cache must refresh.
  await page.goto(`/overview?workspace=${workspace}`);
  await expect(
    page.getByTestId(`outstanding-${invoice.outstanding.currency}`),
  ).toHaveText(display(group.outstanding));
  await page
    .getByRole("link", {
      name: "Choose an invoice for a demo source payment",
      exact: true,
    })
    .click();
  await page.getByRole("link", { name: "INV-101", exact: true }).click();
  const payment = page.getByRole("region", {
    name: "Synthetic source payment",
    exact: true,
  });
  await expect(
    payment.getByRole("button", {
      name: "Record synthetic source payment",
      exact: true,
    }),
  ).toBeDisabled();
  await payment
    .getByLabel(`Demo payment amount (${invoice.outstanding.currency})`, {
      exact: true,
    })
    .fill("25.00");
  const endpoint = `${base}/demo/accounting/simulate-payment`;
  let originalResult: PaymentResult | null = null;
  let originalCommand: PaymentCommand | null = null;
  await page.route(
    `**${endpoint}`,
    async (route) => {
      originalCommand = route.request().postDataJSON() as PaymentCommand;
      // The real route commits; only the browser's first confirmation is lost.
      const committed = await route.fetch();
      expect(committed.status()).toBe(200);
      originalResult = (await committed.json()) as PaymentResult;
      await route.abort("failed");
    },
    { times: 1 },
  );
  await payment
    .getByRole("button", {
      name: "Record synthetic source payment",
      exact: true,
    })
    .click();
  await expect(
    payment.getByRole("button", {
      name: "Retry captured source payment",
      exact: true,
    }),
  ).toBeEnabled();
  await expect(payment.getByRole("alert")).toBeVisible();
  if (!originalResult || !originalCommand)
    throw new Error(
      "Actual source operation must commit before the confirmation is dropped",
    );
  const firstResult = originalResult as PaymentResult;
  const firstCommand = originalCommand as PaymentCommand;
  expect(firstCommand.provider_connection_id).toBe(
    invoice.source.connection_id,
  );
  expect(firstCommand.external_invoice_id).toBe(invoice.source.external_id);
  expect(firstCommand.amount).toEqual({
    minor_units: 2500,
    currency: invoice.outstanding.currency,
    exponent: invoice.outstanding.exponent,
  });
  expect(firstResult.synthetic).toBe(true);
  expect(firstResult.financial_status).toBe("partially_paid");
  await page.reload();
  const retained = page.getByRole("region", {
    name: "Synthetic source payment",
    exact: true,
  });
  await expect(retained.getByRole("status")).toContainText(
    firstCommand.operation_id,
  );
  await expect(
    retained.getByLabel(
      `Demo payment amount (${invoice.outstanding.currency})`,
      { exact: true },
    ),
  ).toBeDisabled();
  const retried = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(endpoint),
  );
  await retained
    .getByRole("button", { name: "Retry captured source payment", exact: true })
    .click();
  const retriedResponse = await retried;
  expect(retriedResponse.status()).toBe(200);
  expect(retriedResponse.request().postDataJSON()).toEqual(firstCommand);
  expect(await retriedResponse.json()).toEqual(firstResult);
  await expect(
    page.getByText("Financial: partially_paid", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Collection: active", { exact: true }),
  ).toBeVisible();
  const partial = (await (
    await page.request.get(`${base}/receivables/${invoice.id}`)
  ).json()) as { invoice: Invoice };
  expect(BigInt(partial.invoice.outstanding.minor_units)).toBe(
    BigInt(invoice.outstanding.minor_units) - 2500n,
  );
  expect(partial.invoice.source.revision).toBe(firstResult.source_revision);
  await expect(
    retained.getByRole("region", { name: "Source payment result" }),
  ).toContainText(firstResult.event_id);

  await retained
    .getByRole("button", {
      name: "Review another synthetic payment",
      exact: true,
    })
    .click();
  await retained
    .getByLabel(`Demo payment amount (${invoice.outstanding.currency})`, {
      exact: true,
    })
    .fill(
      decimal(
        partial.invoice.outstanding.minor_units,
        partial.invoice.outstanding.exponent,
      ),
    );
  const paid = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(endpoint),
  );
  await retained
    .getByRole("button", {
      name: "Record synthetic source payment",
      exact: true,
    })
    .focus();
  await page.keyboard.press("Enter");
  const paidResponse = await paid;
  expect(paidResponse.status()).toBe(200);
  const secondCommand = paidResponse.request().postDataJSON() as PaymentCommand;
  expect(secondCommand.operation_id).not.toBe(firstCommand.operation_id);
  const paidResult = (await paidResponse.json()) as PaymentResult;
  expect(paidResult.invoice_id).toBe(invoice.id);
  expect(paidResult.financial_status).toBe("paid");
  expect(paidResult.collection_lifecycle).toBe("closed");
  await expect(
    page.getByText("Financial: paid", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Collection: closed", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByText("Financial: paid", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Collection: closed", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Invoice reminder history", exact: true }),
  ).toContainText("No recent reminder action is listed for this invoice.");
  // This fresh workspace has no queued reminders. Nonempty cancellation is a backend integration assertion.
  const final = (await (
    await page.request.get(`${base}/receivables/${invoice.id}`)
  ).json()) as { invoice: Invoice };
  expect(final.invoice.outstanding.minor_units).toBe("0");
  expect(final.invoice.source.revision).toBe(paidResult.source_revision);
  await page
    .getByRole("link", { name: "View refreshed Overview", exact: true })
    .click();
  const expected = {
    ...group.outstanding,
    minor_units: (
      BigInt(group.outstanding.minor_units) -
      BigInt(invoice.outstanding.minor_units)
    ).toString(),
  };
  await expect(
    page.getByTestId(`outstanding-${invoice.outstanding.currency}`),
  ).toHaveText(display(expected));
  await page.reload();
  await expect(
    page.getByTestId(`outstanding-${invoice.outstanding.currency}`),
  ).toHaveText(display(expected));
  await expect(
    page.getByRole("link", { name: "Open Review Queue", exact: true }),
  ).toHaveAttribute("href", `/review?workspace=${workspace}`);
  await expect(
    page.getByRole("link", { name: "View investigation history", exact: true }),
  ).toHaveAttribute("href", `/agents?workspace=${workspace}`);
  const session = (await (await page.request.get("/api/me")).json()) as {
    workspaces: {
      id: string;
      outbound_paused: boolean;
      automation_enabled: boolean;
    }[];
  };
  const current = session.workspaces.find((row) => row.id === workspace);
  expect(current?.outbound_paused).toBe(true);
  expect(current?.automation_enabled).toBe(false);
  const outbound = (await (
    await page.request.get(`${base}/email-execution`)
  ).json()) as { system_enabled: boolean; activation: unknown };
  expect(outbound.system_enabled).toBe(false);
  expect(outbound.activation).toBeNull();
  await page.setViewportSize({ width: 390, height: 844 });
  const measureMobile = () =>
    page.evaluate(() => ({
      viewport: innerWidth,
      width: document.documentElement.scrollWidth,
      overflow: [...document.querySelectorAll("body *")]
        .map((element) => {
          const rect = element.getBoundingClientRect();
          return {
            tag: element.tagName,
            class: element.getAttribute("class"),
            x: rect.x,
            right: rect.right,
            width: rect.width,
          };
        })
        .filter((element) => element.right > innerWidth + 1 || element.x < -1)
        .slice(0, 30),
    }));
  const initialLayout = await measureMobile();
  try {
    await expect
      .poll(
        () =>
          page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        { timeout: 5000 },
      )
      .toBe(true);
  } catch (error) {
    await test
      .info()
      .attach("mobile-layout-diagnostic", {
        body: JSON.stringify(
          { initial: initialLayout, final: await measureMobile() },
          null,
          2,
        ),
        contentType: "application/json",
      });
    throw error;
  }
});
