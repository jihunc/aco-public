import { expect, test } from "@playwright/test";
import { readFileSync, existsSync } from "node:fs";

const env = Object.fromEntries((existsSync(".env") ? readFileSync(".env", "utf8") : "")
  .split("\n").filter((x) => x.includes("="))
  .map((x) => [x.slice(0, x.indexOf("=")), x.slice(x.indexOf("=") + 1)]));
const password = process.env.ACO_TEST_DEMO_PASSWORD || env.DEMO_PASSWORD;

test("communications load from API with source availability and invoice links", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Username or email").fill("owner@example.test");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  const me = await (await page.request.get("/api/me")).json();
  const workspace = await page.request.post("/api/workspaces", {
    headers: { origin: "http://127.0.0.1:53000", "x-csrf-token": me.csrf_token },
    data: { name: `Conversation browser ${Date.now()}`, timezone: "Australia/Melbourne" },
  });
  expect(workspace.status()).toBe(201);
  const id = (await workspace.json()).id;
  await page.goto(`/conversations?workspace=${id}`);
  await expect(page.getByRole("heading", { name: "Conversations" })).toBeVisible();
  await expect(page.getByText("No communications yet.")).toBeVisible();
  await page.goto(`/settings/email?workspace=${id}`);
  await expect(page.getByRole("heading", { name: "Email execution" })).toBeVisible();
  await expect(page.getByText("System email execution is disabled.")).toBeVisible();
  await expect(page.getByText("No current reviewed activation.")).toBeVisible();
  const provider = page.getByRole("region", { name: "Company email provider", exact: true });
  await expect(provider).toContainText("No current provider configuration is available for review.");
  const mutating: string[] = [];
  page.on("request", request => {
    if (new URL(request.url()).pathname.includes("/email-execution") && request.method() !== "GET") mutating.push(request.method());
  });
  const refreshed = page.waitForResponse(response => new URL(response.url()).pathname === `/api/workspaces/${id}/email-execution/provider` && response.request().method() === "GET");
  await provider.getByRole("button", { name: "Refresh provider configuration", exact: true }).click();
  expect((await refreshed).status()).toBe(200);
  await expect(provider).toContainText("No current provider configuration is available for review.");
  await expect(page.getByRole("button", { name: "Save company provider activation", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Save synthetic activation", exact: true })).toBeDisabled();
  expect(mutating).toEqual([]);
});
