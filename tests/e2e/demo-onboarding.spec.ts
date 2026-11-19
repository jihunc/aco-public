import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import type {
  PageInvoiceView,
  PageCustomerView,
  WorkspaceView,
} from "../../web/src/lib/generated-api.js";

test("Owner explicitly imports synthetic accounting from onboarding and explores persisted receivables without resetting source records", async ({
  page,
}) => {
  test.setTimeout(90_000);
  expect(process.env.ACO_TEST_PROFILE ?? "mock").toBe("mock");
  const password = process.env.ACO_TEST_DEMO_PASSWORD;
  if (!password) throw new Error("Explicit mock browser password required");
  await page.goto("/login");
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Username or email").fill("owner@example.test");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await expect(page).toHaveURL(/\/onboarding$/);
  const name = `Onboarding source ${randomUUID()}`;
  await page.getByLabel("Workspace name", { exact: true }).fill(name);
  await page
    .getByLabel("Timezone", { exact: true })
    .fill("Australia/Melbourne");
  const created = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/workspaces" &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Create demo workspace", exact: true })
    .click();
  const createResponse = await created;
  expect(createResponse.status()).toBe(201);
  const workspace = (await createResponse.json()) as WorkspaceView;
  expect(workspace.is_demo).toBe(true);
  expect(workspace.role).toBe("owner");
  expect(workspace.automation_enabled).toBe(false);
  expect(workspace.outbound_paused).toBe(true);
  const base = `/api/workspaces/${workspace.id}`;
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  const panel = page.getByRole("region", {
    name: "Synthetic accounting setup",
    exact: true,
  });
  await expect(panel).toContainText(
    "0 imported invoices · 0 imported customers",
  );
  // Workspace creation alone must not invoke the source command or fabricate rows.
  const before = (await (
    await page.request.get(`${base}/receivables?page_size=100`)
  ).json()) as PageInvoiceView;
  expect(before.total).toBe(0);
  const seeded = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `${base}/demo/accounting/seed` &&
      response.request().method() === "POST",
  );
  await panel
    .getByRole("button", { name: "Import synthetic demo data", exact: true })
    .click();
  const seedResponse = await seeded;
  expect(seedResponse.status()).toBe(201);
  const receipt = (await seedResponse.json()) as {
    provider_connection_id: string;
    synthetic: boolean;
  };
  expect(receipt.synthetic).toBe(true);
  expect(seedResponse.request().headers()["x-csrf-token"]).toBeTruthy();
  await expect(panel).toContainText(
    "24 imported invoices · 6 imported customers",
  );
  const readInvoices = async () =>
    (await (
      await page.request.get(`${base}/receivables?page_size=100`)
    ).json()) as PageInvoiceView;
  const imported = await readInvoices();
  const customers = (await (
    await page.request.get(`${base}/customers?page_size=100`)
  ).json()) as PageCustomerView;
  expect(imported.total).toBe(24);
  expect(customers.total).toBe(6);
  for (const invoice of imported.items) {
    expect(invoice.source.connection_id).toBe(receipt.provider_connection_id);
    expect(invoice.source.synthetic).toBe(true);
    expect(invoice.verified).toBe(true);
  }
  expect(customers.items.every((customer) => customer.synthetic)).toBe(true);
  await panel
    .getByRole("link", { name: "Explore imported receivables →", exact: true })
    .click();
  await expect(page).toHaveURL(
    new RegExp(`/receivables\\?workspace=${workspace.id}`),
  );
  await expect(page.getByText("INV-101", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("INV-101", { exact: true })).toBeVisible();
  expect(
    (await readInvoices()).items.map((invoice) => invoice.id).sort(),
  ).toEqual(imported.items.map((invoice) => invoice.id).sort());
  await page.goto("/onboarding");
  await page
    .getByRole("combobox", { name: "Workspace", exact: true })
    .selectOption(workspace.id);
  await expect(panel).toContainText(
    "24 imported invoices · 6 imported customers",
  );
  await expect(panel).toContainText("never resets the workspace");
  const repeated = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `${base}/demo/accounting/seed` &&
      response.request().method() === "POST",
  );
  await panel
    .getByRole("button", { name: "Import synthetic demo data", exact: true })
    .click();
  expect((await repeated).status()).toBe(201);
  await expect(panel).toContainText(
    "24 imported invoices · 6 imported customers",
  );
  const current = await readInvoices();
  const snapshot = (rows: PageInvoiceView) =>
    rows.items
      .map((invoice) => ({
        id: invoice.id,
        revision: invoice.source.revision,
        outstanding: invoice.outstanding,
        financial_status: invoice.financial_status,
      }))
      .sort((a, b) => a.id.localeCompare(b.id));
  expect(snapshot(current)).toEqual(snapshot(imported));
  await page.reload();
  await page
    .getByRole("combobox", { name: "Workspace", exact: true })
    .selectOption(workspace.id);
  await expect(panel).toContainText(
    "24 imported invoices · 6 imported customers",
  );
  const session = (await (await page.request.get("/api/me")).json()) as {
    workspaces: WorkspaceView[];
  };
  const saved = session.workspaces.find((row) => row.id === workspace.id);
  expect(saved?.automation_enabled).toBe(false);
  expect(saved?.outbound_paused).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(
      () =>
        panel.evaluate((element) => element.scrollWidth <= element.clientWidth),
      { timeout: 5000 },
    )
    .toBe(true);
});
