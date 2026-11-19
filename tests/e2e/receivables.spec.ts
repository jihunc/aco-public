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
      name: `T05 browser ${Date.now()}`,
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

test("source backed overview, filters, detail, keyboard/mobile and persisted metric delta", async ({
  page,
}) => {
  const { tenant, headers, connection } = await setup(page);
  await page.goto(`/overview?workspace=${tenant}`);
  await expect(
    page.getByRole("heading", { name: "Overview", exact: true }),
  ).toBeVisible();
  await expect(page.getByTestId("outstanding-AUD")).toHaveText("AUD 1,900.00");
  const overdue = await page.getByTestId("overdue-AUD").innerText();
  await page.screenshot({
    path: test.info().outputPath("overview.png"),
    fullPage: true,
  });
  await expect(
    page.getByText("Synthetic — Accounting-sourced", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Business Impact", exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Receivables", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("heading", { name: "Receivables", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("24 invoices")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Previous page" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Next page" }).click();
  await expect(page.getByText("Page 2 of 2")).toBeVisible();
  await page.getByLabel("Search invoices or customers").fill("INV-101");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect(page.getByText("1 invoices", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "INV-101", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "INV-101", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Financial: open", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Collection: active", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Last source read", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /Mark Paid/i })).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "Communication history" }),
  ).toBeVisible();
  await expect(page.getByText("No communications yet.")).toBeVisible();
  await page.getByRole("link", { name: /Synthetic Example Trading/ }).click();
  await expect(
    page.getByRole("heading", {
      name: "Synthetic Example Trading 1",
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByText("4 invoices")).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("link", { name: "Overview", exact: true }).focus();
  await page.keyboard.press("Enter");
  const payment = await page.request.post(
    `/api/workspaces/${tenant}/demo/accounting/simulate-payment`,
    {
      headers,
      data: {
        provider_connection_id: connection,
        external_invoice_id: "INV-101",
        operation_id: crypto.randomUUID(),
        amount: { minor_units: 3000, currency: "AUD", exponent: 2 },
      },
    },
  );
  expect(payment.status()).toBe(200);
  await page.reload();
  await expect(page.getByTestId("outstanding-AUD")).toHaveText("AUD 1,870.00");
  expect(await page.getByTestId("overdue-AUD").innerText()).not.toBe(overdue);
  await expect(page.getByTestId("aging-total-AUD")).toContainText(
    "AUD 1,870.00",
  );
});

test("loading empty error and unavailable states remain truthful", async ({
  page,
}) => {
  const { tenant } = await setup(page);
  await page.route("**/api/workspaces/*/receivables?*", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 500));
    await route.continue();
  });
  await page.goto(`/receivables?workspace=${tenant}`);
  await expect(
    page.getByRole("status").filter({ hasText: "Loading receivables" }),
  ).toBeVisible();
  await expect(page.getByText("24 invoices")).toBeVisible();
  await page.getByLabel("Search invoices or customers").fill("does-not-exist");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect(
    page.getByText("No invoices match these filters."),
  ).toBeVisible();
  await page.unroute("**/api/workspaces/*/receivables?*");
  await page.route("**/api/workspaces/*/receivables?*", (route) =>
    route.fulfill({ status: 503, json: { detail: "TEMPORARILY_UNAVAILABLE" } }),
  );
  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "Receivables unavailable" }),
  ).toContainText("Receivables unavailable");
  await page.unroute("**/api/workspaces/*/receivables?*");
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByText("24 invoices")).toBeVisible();
  await page.goto(`/overview?workspace=${tenant}`);
  await expect(page.getByText("Needs Your Attention")).toBeVisible();
  await expect(
    page.getByText(
      "Review saved tasks and human approval requests in this workspace.",
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Open Review Queue", exact: true }),
  ).toHaveAttribute("href", `/review?workspace=${tenant}`);
  expect(
    (
      await page.request.get(
        `/api/workspaces/${tenant}/overview?as_of=2020-01-01`,
      )
    ).status(),
  ).toBe(422);
});

test("exact PostgreSQL source amount beyond JavaScript safe integer survives API client and display", async ({
  page,
}) => {
  const { tenant, connection, user } = await setup(page);
  execFileSync(
    ".venv/bin/python",
    ["tests/receivables_browser_source.py", tenant, connection, user],
    { stdio: "pipe" },
  );
  const result = await (
    await page.request.get(
      `/api/workspaces/${tenant}/receivables?search=INV-101`,
    )
  ).json();
  expect(result.items[0].outstanding.minor_units).toBe("9007199254740993");
  await page.goto(`/receivables/${result.items[0].id}?workspace=${tenant}`);
  await expect(
    page.getByText("AUD 90,071,992,547,409.93", { exact: true }),
  ).toHaveCount(2);
  await expect(
    page.getByText("AUD 90,071,992,547,409.93", { exact: true }).first(),
  ).toBeVisible();
  await expect(
    page.getByText("Collection: unavailable", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Collection holds unavailable", { exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("all receivables filters use actual API results and customer search", async ({
  page,
}) => {
  const { tenant } = await setup(page);
  await page.goto(`/receivables?workspace=${tenant}`);
  await expect(page.getByText("24 invoices", { exact: true })).toBeVisible();
  async function clear() {
    await page.getByRole("button", { name: "Clear filters" }).click();
    await expect(page.getByText("24 invoices", { exact: true })).toBeVisible();
  }
  await page
    .getByLabel("Financial status", { exact: true })
    .selectOption("paid");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect(page.getByText("3 invoices", { exact: true })).toBeVisible();
  await clear();
  await page
    .getByLabel("Collection state", { exact: true })
    .selectOption("closed");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect(page.getByText("3 invoices", { exact: true })).toBeVisible();
  await clear();
  await page
    .getByLabel("Source freshness", { exact: true })
    .selectOption("true");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect(
    page.getByText("No invoices match these filters."),
  ).toBeVisible();
  await clear();
  await page.getByLabel("Currency", { exact: true }).fill("AUD");
  await page
    .getByLabel("Minimum balance (minor units)", { exact: true })
    .fill("10000");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect(page.getByText("17 invoices", { exact: true })).toBeVisible();
  await clear();
  const due = await (
    await page.request.get(`/api/workspaces/${tenant}/receivables?overdue=true`)
  ).json();
  await page.getByLabel("Overdue", { exact: true }).selectOption("true");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect(
    page.getByText(`${due.total} invoices`, { exact: true }),
  ).toBeVisible();
  await clear();
  await page.getByLabel("Find customer", { exact: true }).fill("Trading 1");
  await expect(
    page.getByRole("option", {
      name: "Synthetic Example Trading 1",
      exact: true,
    }),
  ).toHaveCount(1);
  const selected = await page
    .getByRole("option", { name: "Synthetic Example Trading 1", exact: true })
    .getAttribute("value");
  await page.getByLabel("Customer", { exact: true }).selectOption(selected!);
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect(page.getByText("4 invoices", { exact: true })).toBeVisible();
});

test("company workload follows real task edits and persisted reload", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const { tenant } = await setup(page);
  await page.goto(`/overview?workspace=${tenant}`);
  const workload = page.getByRole("region", {
    name: "Company workload",
    exact: true,
  });
  const openTasks = () =>
    workload
      .locator("div.metric-card")
      .filter({ has: page.locator("dt", { hasText: /^Open tasks$/ }) })
      .locator("dd");
  await expect(openTasks()).toHaveText("0");
  await expect(
    workload.getByText("Outbound is paused", { exact: false }),
  ).toBeVisible();
  await workload
    .getByRole("link", { name: "Review tasks and approvals", exact: true })
    .click();
  await page.getByRole("button", { name: "New task", exact: true }).click();
  await page
    .getByLabel("Task title", { exact: true })
    .fill("Dashboard persisted workload");
  await page.getByRole("button", { name: "Create task", exact: true }).click();
  await expect(
    page.getByText("Dashboard persisted workload", { exact: true }),
  ).toBeVisible();
  await page.goto(`/overview?workspace=${tenant}`);
  await expect(openTasks()).toHaveText("1");
  await page.reload();
  await expect(openTasks()).toHaveText("1");
  await workload
    .getByRole("button", { name: "Refresh workload", exact: true })
    .click();
  await expect(openTasks()).toHaveText("1");
  await workload
    .getByRole("link", { name: "Review tasks and approvals", exact: true })
    .click();
  await page
    .getByRole("button", {
      name: "Edit Dashboard persisted workload",
      exact: true,
    })
    .click();
  await page
    .getByRole("combobox", { name: "Status", exact: true })
    .selectOption("done");
  await page.getByRole("button", { name: "Save task", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Completed tasks", exact: true }),
  ).toBeVisible();
  await page.goto(`/overview?workspace=${tenant}`);
  await expect(openTasks()).toHaveText("0");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    workload.getByRole("heading", {
      name: "Accounting source state",
      exact: true,
    }),
  ).toBeVisible();
  await page.screenshot({
    path: test.info().outputPath("company-workload.png"),
    fullPage: true,
  });
  const current = await page.request.get(
    `/api/workspaces/${tenant}/overview/workload`,
  );
  expect(current.status()).toBe(200);
  expect((await current.json()).open_tasks).toBe(0);
  await workload
    .getByRole("link", {
      name: "Connections and stopped action evidence",
      exact: true,
    })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Stopped background actions",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Refresh failed actions", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: "Refresh exhausted retries",
      exact: true,
    }),
  ).toBeVisible();
});

test("invoice related tasks and bounded audit persist through real review and reload", async ({ page }) => {
  test.setTimeout(120_000);
  const { tenant, headers } = await setup(page);
  const root = `/api/workspaces/${tenant}`;
  const list = await page.request.get(`${root}/receivables?search=INV-101`);
  expect(list.status()).toBe(200);
  const imported = (await list.json()).items;
  const invoice = imported.find((row: {number: string}) => row.number === "INV-101");
  expect(invoice).toBeTruthy();
  const path = `/receivables/${invoice.id}?workspace=${tenant}`;
  const before = (await (await page.request.get(`${root}/receivables/${invoice.id}`)).json()).invoice;
  const title = `Invoice activity ${Date.now()}`;
  const saved = await page.request.post(`${root}/tasks`, {headers, data: {title, invoice_id: invoice.id, notes: "Synthetic private task notes"}});
  expect(saved.status()).toBe(201);
  const task = await saved.json();
  await page.goto(path);
  const tasks = page.getByRole("region", {name:"Related invoice tasks",exact:true});
  const audit = page.getByRole("region", {name:"Invoice business audit",exact:true});
  await expect(tasks.getByRole("link", {name:title,exact:true})).toBeVisible();
  await expect(audit.getByText("task.created", {exact:true})).toBeVisible();
  await expect(audit.getByText("Synthetic private task notes")).toHaveCount(0);
  await tasks.getByRole("link", {name:title,exact:true}).click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${task.id}`));
  await expect(page.getByRole("heading",{name:title,exact:true})).toBeVisible();
  await page.getByRole("link",{name:"← Review Queue",exact:true}).click();
  await page.getByRole("button", {name:`Edit ${title}`,exact:true}).click();
  await page.getByRole("combobox",{name:"Status",exact:true}).selectOption("done");
  await page.getByRole("button",{name:"Save task",exact:true}).click();
  await expect(page.getByRole("status").filter({hasText:"Task saved."})).toBeVisible();
  await page.goto(path);
  await expect(tasks.getByText(/done · Human task/)).toBeVisible();
  await expect(audit.getByText("task.updated",{exact:true})).toBeVisible();
  await page.reload();
  await expect(tasks.getByText(/done · Human task/)).toBeVisible();
  await expect(audit.getByText("task.updated",{exact:true})).toBeVisible();
  const events = await page.request.get(`${root}/receivables/${invoice.id}/audit`);
  expect(events.status()).toBe(200);
  const actual = (await events.json()).items.find((row:{action:string;target_id:string}) => row.action === "task.updated" && row.target_id === task.id);
  expect(actual).toBeTruthy();
  const update = audit.locator("li").filter({hasText:"task.updated"});
  await update.getByText("Audit provenance",{exact:true}).click();
  await expect(update.getByText(`Event ${actual.id}`,{exact:true})).toBeVisible();
  const after = (await (await page.request.get(`${root}/receivables/${invoice.id}`)).json()).invoice;
  expect(after.outstanding).toEqual(before.outstanding);
  expect(after.financial_status).toEqual(before.financial_status);
  expect(after.source.revision).toEqual(before.source.revision);
  await page.route(`**${root}/receivables/${invoice.id}/audit*`, route => route.abort());
  await audit.getByRole("button",{name:"Refresh audit",exact:true}).click();
  await expect(audit.getByText("Invoice audit unavailable.",{exact:true})).toBeVisible();
  await page.unroute(`**${root}/receivables/${invoice.id}/audit*`);
  await audit.getByRole("button",{name:"Try again",exact:true}).click();
  await expect(audit.getByText("task.updated",{exact:true})).toBeVisible();
  await page.setViewportSize({width:390,height:844});
  await expect(tasks.getByRole("link",{name:title,exact:true})).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path:test.info().outputPath("invoice-activity-mobile.png"),fullPage:true});
});
