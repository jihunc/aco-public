import { expect, test } from "@playwright/test";
import { readFileSync, existsSync } from "node:fs";

const env = Object.fromEntries((existsSync(".env") ? readFileSync(".env", "utf8") : "")
  .split("\n").filter((x) => x.includes("="))
  .map((x) => [x.slice(0, x.indexOf("=")), x.slice(x.indexOf("=") + 1)]));
const password = process.env.ACO_TEST_DEMO_PASSWORD || env.DEMO_PASSWORD;

test("human task CRUD, keyboard order and refresh persist through API", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Username or email").fill("owner@example.test");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await expect(page).toHaveURL(/\/onboarding$/);
  const session = await (await page.request.get("/api/me")).json();
  const workspace = await page.request.post("/api/workspaces", {
    headers: { origin: "http://127.0.0.1:53000", "x-csrf-token": session.csrf_token },
    data: { name: `Review browser ${Date.now()}`, timezone: "Australia/Melbourne" },
  });
  expect(workspace.status()).toBe(201);
  const id = (await workspace.json()).id;
  await page.goto(`/review?workspace=${id}`);
  await expect(page.getByRole("heading", { name: "Review Queue" })).toBeVisible();
  await page.getByRole("button", { name: "New task" }).click();
  await page.getByLabel("Task title").fill("Confirm purchase order");
  await page.getByRole("button", { name: "Create task" }).click();
  await expect(page.getByText("Confirm purchase order")).toBeVisible();
  await page.getByRole("button", { name: "New task" }).click();
  await page.getByLabel("Task title").fill("Check customer account");
  await page.getByRole("button", { name: "Create task" }).click();
  const reordered = page.waitForResponse(response => response.request().method() === "PATCH" &&
    response.url().endsWith(`/api/workspaces/${id}/review-queue`));
  await page.getByRole("button", { name: "Move Check customer account up" }).click();
  expect((await reordered).status()).toBe(200);
  await page.reload();
  await expect(page.locator("[data-testid='review-task-title']")).toHaveText(["Check customer account", "Confirm purchase order"]);
  await page.getByRole("button", { name: "Edit Check customer account" }).click();
  await page.getByLabel("Task title").fill("Check customer ledger evidence");
  await page.getByLabel("Status").selectOption("done");
  await page.getByRole("button", { name: "Save task" }).click();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Completed tasks" })).toBeVisible();
  await expect(page.getByTestId("completed-task").getByText("Check customer ledger evidence")).toBeVisible();
  await page.getByRole("button", { name: "Edit Check customer ledger evidence" }).click();
  await page.getByLabel("Status").selectOption("open");
  await page.getByRole("button", { name: "Save task" }).click();
  await page.reload();
  await expect(page.getByTestId("review-task").getByText("Check customer ledger evidence")).toBeVisible();
  await page.getByRole("button", { name: "Edit Confirm purchase order" }).click();
  await page.getByLabel("Status").selectOption("done");
  await page.getByRole("button", { name: "Save task" }).click();
  await page.reload();
  await page.getByRole("button", { name: "Delete Confirm purchase order" }).click();
  await expect(page.getByText("Confirm deletion")).toBeVisible();
  await page.getByRole("button", { name: "Confirm delete" }).click();
  await page.reload();
  await expect(page.getByText("Confirm purchase order")).toHaveCount(0);
  await expect(page.getByTestId("review-task").getByText("Check customer ledger evidence")).toBeVisible();
});
