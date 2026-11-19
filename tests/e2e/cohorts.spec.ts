import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import type { CohortListView } from "../../web/src/lib/generated-api.js";

test("Owner publishes illustrative source cohorts and retains exact reviewed refresh bindings across reload and retry", async ({
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
      name: `Frozen cohort browser ${randomUUID()}`,
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
  await page.goto(`/business-impact?workspace=${workspace}`);
  const panel = page.getByRole("region", {
    name: "Frozen synthetic cohorts",
    exact: true,
  });
  await expect(
    panel.getByRole("button", {
      name: "Review synthetic cohort creation",
      exact: true,
    }),
  ).toBeVisible();
  await panel
    .getByLabel("Source action review note", { exact: true })
    .fill("Owner reviewed the fixed illustrative synthetic source history.");
  await panel
    .getByRole("button", {
      name: "Review synthetic cohort creation",
      exact: true,
    })
    .click();
  const captured = panel.getByRole("region", {
    name: "Captured cohort source review",
    exact: true,
  });
  await expect(captured).toContainText(
    "does not claim a real customer payment",
  );
  const seedResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith(`${base}/impact/cohorts/synthetic`) &&
      response.request().method() === "POST",
  );
  await captured
    .getByRole("button", { name: "Create illustrative cohorts", exact: true })
    .click();
  const seededResponse = await seedResponse;
  expect(seededResponse.status()).toBe(200);
  const seedCommand = seededResponse.request().postDataJSON() as {
    request_id: string;
    evidence: string;
  };
  expect(seedCommand.request_id).toMatch(/^[0-9a-f-]{36}$/);
  expect(seedCommand.evidence).toContain("Owner reviewed");
  const seeded = (await seededResponse.json()) as CohortListView;
  expect(seeded.pairs).toHaveLength(1);
  const pair = seeded.pairs[0];
  expect(pair.comparison_allowed).toBe(true);
  expect(pair.recovery_change_pp).toBe("15");
  expect(pair.recovery_relative_change_percent).toBe("25");
  expect(pair.paid_in_full_change_pp).toBe("25");
  const before = pair.cohorts.find(
    (cohort) => cohort.side === "BASELINE_SYNTHETIC",
  );
  const after = pair.cohorts.find(
    (cohort) => cohort.side === "AFTER_SYNTHETIC",
  );
  if (!before || !after)
    throw new Error("Actual baseline/after source cohorts required");
  const latest = (cohort: typeof before) =>
    cohort.results.reduce((a, b) => (a.version > b.version ? a : b));
  const results = [latest(before), latest(after)];
  expect(before.members).toHaveLength(4);
  expect(after.members).toHaveLength(4);
  for (const result of results) {
    expect(result.status).toBe("COMPLETE");
    expect(result.opening_minor).toBe("40000");
    expect(result.source_status).toBe("VERIFIED_AT_AS_OF");
    expect(result.freshness).toBe("FROZEN_SNAPSHOT");
  }
  await expect(
    panel.getByTestId(`cohort-recovery-${results[0].id}`),
  ).toHaveText("60%");
  await expect(
    panel.getByTestId(`cohort-paid-in-full-${results[0].id}`),
  ).toHaveText("50%");
  await expect(
    panel.getByTestId(`cohort-recovery-${results[1].id}`),
  ).toHaveText("75%");
  await expect(
    panel.getByTestId(`cohort-paid-in-full-${results[1].id}`),
  ).toHaveText("75%");
  await expect(panel).toContainText("+15 percentage points");
  await expect(panel).toContainText("+25% relative");
  await expect(panel).toContainText(
    "do not establish that ACO caused better payment outcomes",
  );
  await expect(panel).toContainText("Frozen source snapshot at");
  const beforeCard = panel.getByRole("article", {
    name: "Before ACO · synthetic baseline",
    exact: true,
  });
  await beforeCard
    .getByText("Inspect frozen membership and opening source", { exact: true })
    .click();
  const member = before.members[0];
  await expect(beforeCard).toContainText(
    `Original due ${member.original_due_date}`,
  );
  await expect(beforeCard).toContainText(member.source_watermark);
  const sourceLink = beforeCard.getByRole("link", {
    name: `Source invoice ${member.invoice_id}`,
    exact: true,
  });
  await expect(sourceLink).toHaveAttribute(
    "href",
    `/receivables/${member.invoice_id}?workspace=${workspace}`,
  );
  await sourceLink.click();
  await expect(page).toHaveURL(
    new RegExp(`/receivables/${member.invoice_id}\\?workspace=${workspace}`),
  );
  await expect(page.locator(".invoice-detail")).toContainText("Synthetic");
  await page.goto(`/business-impact?workspace=${workspace}`);
  await expect(
    panel.getByTestId(`cohort-recovery-${results[0].id}`),
  ).toHaveText("60%");
  await page.reload();
  await expect(
    panel.getByTestId(`cohort-recovery-${results[1].id}`),
  ).toHaveText("75%");
  await panel
    .getByLabel("Source action review note", { exact: true })
    .fill("Owner reviewed both frozen results before reading source again.");
  await panel
    .getByRole("button", { name: "Review source refresh", exact: true })
    .click();
  for (const result of results)
    await expect(captured).toContainText(`Reviewed result ${result.id}`);
  await captured
    .getByText("Inspect captured request references", { exact: true })
    .click();
  const capturedReferences = await captured.innerText();
  await page.reload();
  for (const result of results)
    await expect(captured).toContainText(`Reviewed result ${result.id}`);
  await captured
    .getByText("Inspect captured request references", { exact: true })
    .click();
  await expect(captured).toHaveText(capturedReferences, { useInnerText: true });
  // The first real source refresh commits, but its browser confirmation is lost.
  // Retry must use the captured UUID/IDs; it must not create an invented result version.
  const refreshPath = `${base}/impact/cohorts/${pair.id}/refresh`;
  let firstCommand:
    | { request_id: string; expected_result_ids: string[]; evidence: string }
    | undefined;
  await page.route(
    `**${refreshPath}`,
    async (route) => {
      firstCommand = route.request().postDataJSON() as typeof firstCommand;
      const response = await route.fetch();
      expect(response.status()).toBe(200);
      await route.abort("failed");
    },
    { times: 1 },
  );
  await captured
    .getByRole("button", {
      name: "Refresh reviewed cohort source",
      exact: true,
    })
    .click();
  await expect(captured).toContainText("unconfirmed outcome");
  expect(firstCommand?.expected_result_ids).toEqual(
    results.map((result) => result.id),
  );
  await page.reload();
  await expect(captured).toContainText("unconfirmed outcome");
  for (const result of results)
    await expect(captured).toContainText(`Reviewed result ${result.id}`);
  const retryResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith(refreshPath) &&
      response.request().method() === "POST",
  );
  await captured
    .getByRole("button", {
      name: "Retry same captured source request",
      exact: true,
    })
    .click();
  const retried = await retryResponse;
  expect(retried.status()).toBe(200);
  expect(retried.request().postDataJSON()).toEqual(firstCommand);
  const refreshed = (await retried.json()) as CohortListView;
  expect(refreshed.pairs[0].id).toBe(pair.id);
  expect(
    refreshed.pairs[0].cohorts.map((cohort) =>
      cohort.results.map((result) => result.id),
    ),
  ).toEqual(
    pair.cohorts.map((cohort) => cohort.results.map((result) => result.id)),
  );
  await expect(captured).toHaveCount(0);
  await page.reload();
  for (const result of results)
    await expect(
      panel.getByTestId(`cohort-recovery-${result.id}`),
    ).toBeVisible();
  const current = (await (await page.request.get("/api/me")).json()) as {
    workspaces: {
      id: string;
      role: string;
      automation_enabled: boolean;
      outbound_paused: boolean;
    }[];
  };
  const membership = current.workspaces.find((row) => row.id === workspace);
  expect(membership?.role).toBe("owner");
  expect(membership?.automation_enabled).toBe(false);
  expect(membership?.outbound_paused).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(
      () =>
        page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      { timeout: 5000 },
    )
    .toBe(true);
});
