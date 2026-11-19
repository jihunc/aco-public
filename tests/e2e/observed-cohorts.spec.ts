import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import type {
  CohortListView,
  ObservedCohortView,
  ObservedList,
  ObservedPreview,
  WorkspaceView,
} from "../../web/src/lib/generated-api.js";

test("Owner freezes observed source membership and explicitly restates it without changing prior evidence", async ({
  page,
}) => {
  test.setTimeout(120_000);
  expect(process.env.ACO_TEST_PROFILE ?? "mock").toBe("mock");
  const password = process.env.ACO_TEST_DEMO_PASSWORD;
  if (!password) throw new Error("Explicit mock browser password required");
  await page.goto("/login");
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Username or email").fill("owner@example.test");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await expect(page).toHaveURL(/\/onboarding$/);
  await page
    .getByLabel("Workspace name", { exact: true })
    .fill(`Observed cohort ${randomUUID()}`);
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
  const createdResponse = await created;
  expect(createdResponse.status()).toBe(201);
  const workspace = (await createdResponse.json()) as WorkspaceView;
  expect(workspace.role).toBe("owner");
  expect(workspace.automation_enabled).toBe(false);
  expect(workspace.outbound_paused).toBe(true);
  const base = `/api/workspaces/${workspace.id}`;
  const seeded = page.waitForResponse(
    (response) =>
      response.url().endsWith(`${base}/demo/accounting/seed`) &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Import synthetic demo data", exact: true })
    .click();
  expect((await seeded).status()).toBe(201);
  await expect(
    page.getByRole("region", {
      name: "Synthetic accounting setup",
      exact: true,
    }),
  ).toContainText("24 imported invoices · 6 imported customers");
  await page.goto(`/business-impact?workspace=${workspace.id}`);
  const observed = page.getByRole("region", {
    name: "Observed frozen cohorts",
    exact: true,
  });
  await expect(observed).toBeVisible();
  const initialResponse = await page.request.get(
    `${base}/impact/cohorts/observed`,
  );
  expect(initialResponse.status()).toBe(200);
  const initial = (await initialResponse.json()) as ObservedList;
  expect(initial.can_build).toBe(true);
  expect(initial.cohorts).toHaveLength(0);
  expect(initial.sources).toHaveLength(1);
  const selection = observed.getByRole("form", {
    name: "Observed cohort selection",
    exact: true,
  });
  await selection
    .getByRole("combobox", { name: "Observed source connection", exact: true })
    .selectOption(initial.sources[0].connection_id);
  await selection
    .getByLabel("Original due-date start", { exact: true })
    .fill(new Date(Date.now() - 86400000 * 100).toISOString().slice(0, 10));
  await selection
    .getByLabel("Original due-date end (excluded)", { exact: true })
    .fill(new Date(Date.now() + 86400000 * 100).toISOString().slice(0, 10));
  await selection
    .getByLabel("Original credit-term days", { exact: true })
    .fill("21");
  await selection
    .getByLabel("Observed cohort review evidence", { exact: true })
    .fill(
      "Owner reviewed actual retained source; unknown opening cannot be inferred from balance.",
    );
  const previewResponse = page.waitForResponse(
    (r) =>
      r.url().endsWith(`${base}/impact/cohorts/observed/preview`) &&
      r.request().method() === "POST",
  );
  await selection
    .getByRole("button", {
      name: "Preview observed source population",
      exact: true,
    })
    .click();
  const previewWire = await previewResponse;
  expect(previewWire.status()).toBe(200);
  const preview = (await previewWire.json()) as ObservedPreview;
  expect(preview.result.status).toBe("INCOMPLETE");
  expect(preview.result.recovery_percent).toBeNull();
  expect(preview.members.length).toBeGreaterThan(0);
  expect(preview.members.every((member) => member.opening_minor === null)).toBe(
    true,
  );
  await page.reload();
  const captured = observed.getByRole("region", {
    name: "Captured observed cohort review",
    exact: true,
  });
  await expect(captured).toContainText(preview.reviewed_hash);
  await expect(captured).toContainText(
    "Synthetic financial source remains synthetic",
  );
  const publish = captured.getByRole("button", {
    name: "Publish reviewed cohort version",
    exact: true,
  });
  await expect(publish).toBeDisabled();
  await captured.getByRole("checkbox").check();
  const freezing = page.waitForResponse(
    (r) =>
      r.url().endsWith(`${base}/impact/cohorts/observed`) &&
      r.request().method() === "POST",
  );
  await publish.click();
  const frozenWire = await freezing;
  expect(frozenWire.status()).toBe(201);
  const incomplete = (await frozenWire.json()) as ObservedCohortView;
  expect(incomplete.synthetic).toBe(true);
  expect(incomplete.execution_basis).toBe("OBSERVED");
  await expect(
    observed.getByRole("region", {
      name: `Observed cohort ${incomplete.id}`,
      exact: true,
    }),
  ).toContainText("Insufficient data");
  // Explicit public synthetic-source creation supplies complete history; no test DB source writes.
  const illustrative = page.getByRole("region", {
    name: "Frozen synthetic cohorts",
    exact: true,
  });
  await illustrative
    .getByLabel("Source action review note", { exact: true })
    .fill(
      "Owner explicitly publishes fixed synthetic history for separate observed cohort review.",
    );
  await illustrative
    .getByRole("button", {
      name: "Review synthetic cohort creation",
      exact: true,
    })
    .click();
  const sourceResponse = page.waitForResponse(
    (r) =>
      r.url().endsWith(`${base}/impact/cohorts/synthetic`) &&
      r.request().method() === "POST",
  );
  await illustrative
    .getByRole("button", { name: "Create illustrative cohorts", exact: true })
    .click();
  const sourceWire = await sourceResponse;
  expect(sourceWire.status()).toBe(200);
  const pair = ((await sourceWire.json()) as CohortListView).pairs[0];
  const baseline = pair.cohorts.find(
      (row) => row.side === "BASELINE_SYNTHETIC",
    ),
    after = pair.cohorts.find((row) => row.side === "AFTER_SYNTHETIC");
  if (!baseline || !after) throw Error("Actual public source history required");
  await selection
    .getByRole("combobox", { name: "Observed source connection", exact: true })
    .selectOption(initial.sources[0].connection_id);
  await selection
    .getByLabel("Original due-date start", { exact: true })
    .fill(baseline.period_start);
  await selection
    .getByLabel("Original due-date end (excluded)", { exact: true })
    .fill(baseline.period_end);
  await selection
    .getByLabel("Original credit-term days", { exact: true })
    .fill("30");
  await selection
    .getByLabel("Observed cohort review evidence", { exact: true })
    .fill(
      "Owner reviews the complete retained baseline source without relabeling synthetic finances.",
    );
  await selection
    .getByRole("button", {
      name: "Preview observed source population",
      exact: true,
    })
    .click();
  await expect(captured).toContainText("60%");
  await captured.getByRole("checkbox").check();
  const completeResponse = page.waitForResponse(
    (r) =>
      r.url().endsWith(`${base}/impact/cohorts/observed`) &&
      r.request().method() === "POST",
  );
  await publish.click();
  const completeWire = await completeResponse;
  expect(completeWire.status()).toBe(201);
  const complete = (await completeWire.json()) as ObservedCohortView;
  expect(complete.versions[0].results[0].recovery_percent).toBe("60");
  expect(complete.comparison_allowed).toBe(false);
  await observed
    .getByRole("button", {
      name: `Review membership restatement ${complete.id}`,
      exact: true,
    })
    .click();
  await selection
    .getByLabel("Original due-date start", { exact: true })
    .fill(after.period_start);
  await selection
    .getByLabel("Original due-date end (excluded)", { exact: true })
    .fill(after.period_end);
  await selection
    .getByLabel("Observed cohort review evidence", { exact: true })
    .fill(
      "Explicitly review changed original-date membership while retaining the previous definition.",
    );
  await selection
    .getByRole("button", {
      name: "Preview observed source population",
      exact: true,
    })
    .click();
  await expect(captured).toContainText("75%");
  await captured.getByRole("checkbox").check();
  const changedResponse = page.waitForResponse(
    (r) =>
      r
        .url()
        .endsWith(`${base}/impact/cohorts/observed/${complete.id}/restate`) &&
      r.request().method() === "POST",
  );
  await publish.click();
  const changedWire = await changedResponse;
  expect(changedWire.status()).toBe(200);
  const changed = (await changedWire.json()) as ObservedCohortView;
  expect(changed.versions).toHaveLength(2);
  expect(changed.versions[0]).toEqual(complete.versions[0]);
  expect(changed.versions[1].prior_definition_id).toBe(complete.versions[0].id);
  expect(changed.versions[1].results[0].recovery_percent).toBe("75");
  await page.reload();
  const record = observed.getByRole("region", {
    name: `Observed cohort ${complete.id}`,
    exact: true,
  });
  await expect(record).toContainText("Frozen membership version 2");
  await expect(record).toContainText("60%");
  await expect(record).toContainText("75%");
  await record
    .getByText("Inspect frozen members (4)", { exact: true })
    .first()
    .click();
  const invoice = complete.versions[0].members[0].invoice_id;
  await expect(
    record.getByRole("link", { name: `Invoice ${invoice}`, exact: true }),
  ).toHaveAttribute(
    "href",
    `/receivables/${invoice}?workspace=${workspace.id}`,
  );
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  const retained = (await (
    await page.request.get(`${base}/impact/cohorts/observed`)
  ).json()) as ObservedList;
  expect(retained.cohorts).toHaveLength(2);
  const original = (await (
    await page.request.get(`${base}/impact/cohorts`)
  ).json()) as CohortListView;
  expect(original.pairs[0]).toEqual(pair);
});
