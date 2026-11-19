import { expect, test, type Page } from "@playwright/test";
import { readFileSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
const env = Object.fromEntries(
  (existsSync(".env") ? readFileSync(".env", "utf8") : "")
    .split("\n")
    .filter((x) => x.includes("="))
    .map((x) => [x.slice(0, x.indexOf("=")), x.slice(x.indexOf("=") + 1)]),
);
const password = process.env.ACO_TEST_DEMO_PASSWORD || env.DEMO_PASSWORD;
const base = "http://127.0.0.1:53000";
async function setup(page: Page) {
  await page.goto("/login");
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Username or email").fill("owner@example.test");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await expect(page).toHaveURL(/\/onboarding$/);
  const me = await (await page.request.get("/api/me")).json();
  const headers = { origin: base, "x-csrf-token": me.csrf_token };
  const response = await page.request.post("/api/workspaces", {
    headers,
    data: {
      name: `T06 browser ${Date.now()}`,
      timezone: "Australia/Melbourne",
    },
  });
  expect(response.status()).toBe(201);
  const tenant = (await response.json()).id;
  const seed = await page.request.post(
    `/api/workspaces/${tenant}/demo/accounting/seed`,
    { headers, data: {} },
  );
  expect(seed.status()).toBe(201);
  return {
    tenant,
    headers,
    user: me.id,
    connection: (await seed.json()).provider_connection_id,
  };
}

test("persisted provider usage, exact fees and blocked budget notices", async ({
  page,
}) => {
  const { tenant, headers, user } = await setup(page);
  await page.goto(`/overview?workspace=${tenant}`);
  await expect(
    page.getByRole("heading", { name: "Usage & budgets" }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Real external execution is BLOCKED. Draft limits do not authorize spending.",
    ),
  ).toBeVisible();
  await page.getByLabel("Draft currency").fill("USD");
  await page.getByLabel("Draft monetary cap").fill("0.10");
  const savedResponse = page.waitForResponse((response) =>
    response.request().method() === "PUT" && response.url().endsWith(`/api/workspaces/${tenant}/usage/draft-limit`),
  );
  await page.getByRole("button", { name: "Save draft limit" }).click();
  const saved = await savedResponse;
  expect(saved.status(), await saved.text()).toBe(200);
  const immediate = (await saved.json()).limits;
  const persisted = await page.request.get(`/api/workspaces/${tenant}/usage`);
  expect(persisted.status()).toBe(200);
  expect((await persisted.json()).limits).toEqual(immediate);
  console.log("T06 persisted draft", JSON.stringify({ tenant, limits: immediate }));
  await expect(
    page.getByText("DRAFT · tenant · USD 0.100000000000", { exact: true }),
  ).toBeVisible();
  const transition = execFileSync(
    ".venv/bin/python",
    ["tests/usage_browser_source.py", tenant, user],
    { stdio: "pipe", encoding: "utf8" },
  );
  console.log("T06 production transitions", transition.trim());
  await page.reload();
  await expect(page.getByTestId("usage-known-USD-synthetic")).toHaveText(
    "USD 0.000000000005",
  );
  await expect(page.getByTestId("usage-reported-USD-synthetic")).toHaveText(
    "USD 0.000000000003",
  );
  await expect(page.getByTestId("usage-estimated-USD-synthetic")).toHaveText(
    "USD 0.000000000002",
  );
  await expect(page.getByTestId("usage-known-AUD-synthetic")).toHaveText(
    "AUD 99999999999999999999999999.123456789012",
  );
  await expect(page.getByTestId("usage-reserved-USD-synthetic")).toHaveText(
    "USD 0.060000000000",
  );
  await expect(
    page.getByText("partial · 1 unknown charge", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(/Budget request blocked: CAP_REACHED:/),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Currencies are separate; no FX conversion. Simulated fees are not actual vendor charges.",
    ),
  ).toBeVisible();
  await expect(page.getByTestId("usage-known-EUR-synthetic")).toHaveText("EUR 0.100000000000");
  await expect(page.getByText("Total unavailable: unresolved bundle overlap. Top-level known subtotal only.", { exact: true })).toBeVisible();
  await expect(page.getByText("carrier: EUR 0.080000000000", { exact: true })).toBeVisible();
  await expect(page.getByText("provider_total: EUR 0.100000000000", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("tts: EUR 0.080000000000", { exact: true })).toBeVisible();
  const api = await page.request.get(`/api/workspaces/${tenant}/usage`);
  expect(api.status()).toBe(200);
  const summary = await api.json();
  expect(summary.bundle_issues).toHaveLength(2);
  expect(summary.bundle_issues[0]).toMatchObject({ currency: "EUR", component_amount: "0.080000000000", parent_amount: "0.100000000000", reason: "BUNDLE_COMPONENTS_EXCEED_PARENT" });
  expect(summary.currencies.find((row: { currency: string }) => row.currency === "EUR")).toMatchObject({ completeness: "partial", bundle_issue_count: 2, unknown_count: 0 });
  expect(summary.bundle_issues.map((issue: { component: string }) => issue.component).sort()).toEqual(["carrier", "tts"]);
  expect(summary.providers.find((row: { currency: string }) => row.currency === "EUR")).toMatchObject({ completeness: "partial", bundle_issue_count: 2, unknown_count: 0 });
  expect(summary.external_execution).toBe("BLOCKED");
  console.log("T06 rendered persisted usage", JSON.stringify({ tenant, currencies: summary.currencies, notices: summary.notices }));
  const forged = await page.request.put(
    `/api/workspaces/${tenant}/usage/draft-limit`,
    {
      headers,
      data: {
        currency: "USD",
        amount: "1",
        requests: 1,
        external_authorization: "approved",
      },
    },
  );
  expect(forged.status()).toBe(422);
});
