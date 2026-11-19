import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";

type Impact = {
  since: string;
  until: string;
  cash: {
    currency: string;
    exponent: number;
    synthetic: boolean;
    net_cash: { minor_units: string } | null;
    allocation_count: number;
    completeness: string;
  }[];
  activity: { kind: string; count: number; statuses: Record<string, number> }[];
};

test("source-backed Impact reports known cash, explicit windows and real activity without outcome claims", async ({
  page,
}) => {
  test.setTimeout(120_000);
  expect(process.env.ACO_TEST_PROFILE ?? "mock").toBe("mock");
  const password = process.env.ACO_TEST_DEMO_PASSWORD;
  if (!password)
    throw new Error("Explicit mock browser test password required");
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
      name: `Impact browser ${randomUUID()}`,
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
  const initialResponse = page.waitForResponse(
    (response) =>
      response.url().includes(`${base}/impact?`) &&
      response.request().method() === "GET",
  );
  await page.goto(`/business-impact?workspace=${workspace}`);
  const response = await initialResponse;
  expect(response.status()).toBe(200);
  const initial = (await response.json()) as Impact;
  // Independent seed arithmetic: four partial cash receipts of 5,000 plus three full
  // receipts of 10,000 = 50,000 minor AUD. No comparative cohort is invented.
  const aud = initial.cash.find(
    (group) =>
      group.currency === "AUD" && group.exponent === 2 && group.synthetic,
  );
  expect(aud?.net_cash?.minor_units).toBe("50000");
  expect(aud?.allocation_count).toBe(7);
  expect(aud?.completeness).toBe("partial");
  await expect(page.getByTestId("impact-net-AUD-2-synthetic")).toHaveText(
    "AUD 500.00",
  );
  await expect(page.getByTestId("outstanding-AUD")).toHaveText("AUD 1,900.00");
  await expect(
    page.getByRole("region", { name: "Known imported cash", exact: true }),
  ).toContainText("Partial source history");
  const outcomeLimits = page.getByRole("region", {
    name: "Outcome metrics requiring more evidence",
    exact: true,
  });
  await expect(outcomeLimits).toContainText(
    "summary does not replace those scopes or establish causal uplift, measured labour savings or achieved ROI.",
  );
  const cohortLimits = outcomeLimits.getByRole("definition").filter({
    hasText: "Frozen observed-cohort results",
  });
  await expect(cohortLimits).toContainText(
    "Their source, maturity and comparability limits remain explicit; neither establishes causal impact.",
  );
  const humanTimeLimits = outcomeLimits.getByRole("definition").filter({
    hasText: "Case-bound decisions use exact immutable case/action links",
  });
  await expect(humanTimeLimits).toContainText(
    "Unbound historical approvals remain unattributed",
  );
  await expect(humanTimeLimits).toContainText(
    "event counts and manual reported minutes do not establish measured handling or labour savings.",
  );
  const economicLimits = outcomeLimits.getByRole("definition").filter({
    hasText: "Operating economics shows canonical usage allocations",
  });
  await expect(economicLimits).toContainText(
    "Owner-reviewed versioned assumptions",
  );
  await expect(economicLimits).toContainText(
    "missing provider billing, unallocated usage and mixed currencies",
  );
  await expect(economicLimits).toContainText(
    "Reported minutes are not measured handling; estimated ROI remains unavailable without required measured handling and complete same-currency costs.",
  );
  await page
    .getByText("Inspect cash allocation evidence", { exact: true })
    .click();
  await expect(
    page.getByRole("table", {
      name: "Immutable imported allocations for this reporting window",
    }),
  ).toBeVisible();
  const invoices = (await (
    await page.request.get(`${base}/receivables?page_size=100`)
  ).json()) as {
    items: {
      id: string;
      number: string;
      source: { connection_id: string; external_id: string };
    }[];
  };
  const invoice = invoices.items.find((row) => row.number === "INV-101");
  if (!invoice) throw new Error("Actual seeded invoice unavailable");
  const started = await page.request.post(`${base}/agent-runs`, {
    headers,
    data: {
      invoice_ids: [invoice.id],
      objective:
        "Synthetic reporting fixture: inspect source evidence without outreach.",
      request_id: randomUUID(),
    },
  });
  expect(started.status()).toBe(202);
  const run = (await started.json()) as { id: string; authenticity: string };
  expect(run.authenticity).toBe("FAKE");
  const paid = await page.request.post(
    `${base}/demo/accounting/simulate-payment`,
    {
      headers,
      data: {
        provider_connection_id: invoice.source.connection_id,
        external_invoice_id: invoice.source.external_id,
        operation_id: randomUUID(),
        amount: { minor_units: 2500, currency: "AUD", exponent: 2 },
      },
    },
  );
  expect(paid.status()).toBe(200);
  const refreshed = page.waitForResponse(
    (result) =>
      result.url().includes(`${base}/impact?`) &&
      result.request().method() === "GET",
  );
  await page
    .getByRole("button", { name: "Last 30 days through now", exact: true })
    .click();
  const refreshedResponse = await refreshed;
  expect(refreshedResponse.status()).toBe(200);
  const current = (await refreshedResponse.json()) as Impact;
  expect(
    current.cash.find((group) => group.currency === "AUD" && group.synthetic)
      ?.net_cash?.minor_units,
  ).toBe("52500");
  expect(
    current.activity.find((group) => group.kind === "agent_runs")?.count,
  ).toBe(1);
  await expect(page.getByTestId("impact-net-AUD-2-synthetic")).toHaveText(
    "AUD 525.00",
  );
  await expect(page.getByTestId("outstanding-AUD")).toHaveText("AUD 1,875.00");
  const bookmarked = page.url();
  expect(new URL(bookmarked).searchParams.get("since")).toBeTruthy();
  expect(new URL(bookmarked).searchParams.get("until")).toBeTruthy();
  await page.reload();
  await expect(page.getByTestId("impact-net-AUD-2-synthetic")).toHaveText(
    "AUD 525.00",
  );
  await page.getByText("Inspect recent saved records", { exact: true }).click();
  await expect(
    page.getByRole("link", {
      name: `Agent run records ${run.id}`,
      exact: true,
    }),
  ).toHaveAttribute("href", `/agent-runs/${run.id}?workspace=${workspace}`);
  await expect(
    page.getByRole("region", {
      name: "Provider evidence in reporting window",
      exact: true,
    }),
  ).toContainText("Unknown charges are not zero costs");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(
      () =>
        page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      { timeout: 5000 },
    )
    .toBe(true);
  await page
    .getByLabel("Window start (UTC, included)", { exact: true })
    .fill("2020-01-01T00:00:00");
  await page
    .getByLabel("Window end (UTC, excluded)", { exact: true })
    .fill("2020-02-01T00:00:00");
  await page
    .getByRole("button", { name: "Apply reporting window", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Known imported cash", exact: true }),
  ).toContainText("Missing history is not a verified zero");
  await expect(page.getByTestId("outstanding-AUD")).toHaveText("AUD 1,875.00");
  await expect(
    page.getByRole("region", { name: "Reporting basis", exact: true }),
  ).toContainText("not historical balances");
  await page.reload();
  await expect(
    page.getByRole("region", { name: "Known imported cash", exact: true }),
  ).toContainText("Missing history is not a verified zero");
  expect(new URL(page.url()).searchParams.get("since")).toBe(
    "2020-01-01T00:00:00.000Z",
  );
  const session = (await (await page.request.get("/api/me")).json()) as {
    workspaces: {
      id: string;
      outbound_paused: boolean;
      automation_enabled: boolean;
    }[];
  };
  const workspaceState = session.workspaces.find((row) => row.id === workspace);
  expect(workspaceState?.outbound_paused).toBe(true);
  expect(workspaceState?.automation_enabled).toBe(false);
});
