import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import type {
  EconomicsAssumptionView,
  EconomicsView,
  CostAllocationView,
  WorkspaceView,
} from "../../web/src/lib/generated-api.js";

test("Owner reviews persisted operating assumptions and original unknown charges without inventing ROI", async ({
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
    .fill(`Economics ${randomUUID()}`);
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
  const reportingWindow = {
    since: new Date(Date.now() - 86400000).toISOString(),
    until: new Date(Date.now() + 86400000).toISOString(),
  };
  const query = new URLSearchParams(reportingWindow);
  const read = async () => {
    const response = await page.request.get(
      `${base}/impact/economics?${query}`,
    );
    expect(response.status()).toBe(200);
    return (await response.json()) as EconomicsView;
  };
  await page.goto(
    `/business-impact?${new URLSearchParams({ workspace: workspace.id, ...reportingWindow })}`,
  );
  const panel = page.getByRole("region", {
    name: "Operating economics workflow",
    exact: true,
  });
  const editor = panel.getByRole("region", {
    name: "Owner economics assumptions",
    exact: true,
  });
  await expect(editor).toBeVisible();
  const initial = await read();
  expect(initial.assumptions).toHaveLength(0);
  expect(initial.events.length).toBeGreaterThan(0);
  expect(
    initial.events.every((event) => event.synthetic && event.amount === null),
  ).toBe(true);
  await editor
    .getByLabel("Fixed-cost allocation (blank means unknown)", { exact: true })
    .fill("0");
  await editor
    .getByLabel("Fixed-cost evidence, including explicit zero", { exact: true })
    .fill(
      "Owner explicitly allocates zero for this synthetic demonstration; not a measured bill.",
    );
  await editor
    .getByLabel("Assumption provenance", { exact: true })
    .fill(
      "Illustrative defaults reviewed for this exact synthetic window; no achieved savings claimed.",
    );
  const saving = page.waitForResponse(
    (response) =>
      response.url().endsWith(`${base}/impact/economics/assumptions`) &&
      response.request().method() === "POST",
  );
  await editor
    .getByRole("button", { name: "Save assumption draft", exact: true })
    .click();
  const saved = await saving;
  expect(saved.status()).toBe(201);
  const draft = (await saved.json()) as EconomicsAssumptionView;
  expect(draft.approval_id).toBeNull();
  expect(draft.command.wage_per_hour).toBe("60");
  expect(draft.command.fixed_cost).toBe("0");
  await editor
    .getByRole("button", { name: "Review saved assumptions", exact: true })
    .click();
  const review = editor.getByRole("region", {
    name: "Captured economics review",
    exact: true,
  });
  await expect(review).toContainText(draft.reviewed_hash);
  await expect(
    review.getByRole("button", {
      name: "Approve reviewed assumptions",
      exact: true,
    }),
  ).toBeDisabled();
  await review
    .getByLabel(
      "I reviewed these saved assumptions, exact window and synthetic/currency basis.",
      { exact: true },
    )
    .check();
  await review
    .getByLabel("Owner approval evidence", { exact: true })
    .fill(
      "Reviewed original saved values, explicit zero and exact synthetic reporting scope.",
    );
  const approving = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/assumptions/${draft.id}/approve`) &&
      response.request().method() === "POST",
  );
  await review
    .getByRole("button", { name: "Approve reviewed assumptions", exact: true })
    .click();
  const approvedResponse = await approving;
  expect(approvedResponse.status()).toBe(200);
  expect(approvedResponse.request().postDataJSON().reviewed_hash).toBe(
    draft.reviewed_hash,
  );
  const approved = (await approvedResponse.json()) as EconomicsAssumptionView;
  expect(approved.approval_id).not.toBeNull();
  expect((await read()).current_approval_id).toBe(approved.approval_id);
  const report = panel.getByRole("region", {
    name: "Recorded operating economics",
    exact: true,
  });
  await expect(report).toContainText("Insufficient data");
  const group = (await read()).groups.find(
    (row) => row.currency === "AUD" && row.synthetic,
  );
  expect(group?.eligible_case_count).toBe(0);
  expect(group?.resolved_case_count).toBe(0);
  expect(group?.capacity.estimated_roi_percent).toBeNull();
  expect(group?.costs.complete_cost_per_resolution).toBeNull();
  await panel
    .getByText("Owner review of canonical cost allocations", { exact: true })
    .click();
  const event = initial.events[0];
  await panel
    .getByRole("button", {
      name: `Review allocation ${event.event_id}`,
      exact: true,
    })
    .click();
  const allocation = panel.getByRole("region", {
    name: "Captured allocation review",
    exact: true,
  });
  await expect(allocation).toContainText("Unallocated");
  await allocation
    .getByLabel("Allocation review evidence", { exact: true })
    .fill(
      "Reviewed original accounting operation; no case linkage is inferred from missing source evidence.",
    );
  const allocating = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/usage/${event.event_id}/allocation`) &&
      response.request().method() === "POST",
  );
  await allocation
    .getByRole("button", { name: "Save reviewed allocation", exact: true })
    .click();
  const allocatedResponse = await allocating;
  expect(allocatedResponse.status()).toBe(201);
  const allocated = (await allocatedResponse.json()) as CostAllocationView;
  expect(allocated.basis).toBe("UNALLOCATED");
  expect(allocated.basis_revision_id).toBe(event.revision_id);
  expect(allocated.leaves).toEqual([
    { case_id: null, invoice_id: null, weight: "1.000000000000" },
  ]);
  await page.reload();
  await expect(editor).toBeVisible();
  const after = await read();
  expect(after.current_approval_id).toBe(approved.approval_id);
  expect(
    after.events.find((row) => row.event_id === event.event_id)?.allocation.id,
  ).toBe(allocated.id);
  expect(after.events.every((row) => row.amount === null)).toBe(true);
  expect(
    after.groups.every((row) => row.capacity.estimated_roi_percent === null),
  ).toBe(true);
  // A second tab commits a real new draft while the first tab holds a captured review.
  await editor.getByLabel("Assumed wage per hour", { exact: true }).fill("61");
  const savingSecond = page.waitForResponse(
    (response) =>
      response.url().endsWith(`${base}/impact/economics/assumptions`) &&
      response.request().method() === "POST",
  );
  await editor
    .getByLabel("Assumption provenance", { exact: true })
    .fill("Second draft for an actual stale-review browser check.");
  await editor
    .getByRole("button", { name: "Save assumption draft", exact: true })
    .click();
  const secondResponse = await savingSecond;
  expect(secondResponse.status()).toBe(201);
  const second = (await secondResponse.json()) as EconomicsAssumptionView;
  await editor
    .getByRole("button", { name: "Review saved assumptions", exact: true })
    .click();
  await review
    .getByLabel(
      "I reviewed these saved assumptions, exact window and synthetic/currency basis.",
      { exact: true },
    )
    .check();
  await review
    .getByLabel("Owner approval evidence", { exact: true })
    .fill(
      "Captured second version should be rejected after a concurrent newer draft.",
    );
  const me = (await (await page.request.get("/api/me")).json()) as {
    csrf_token: string;
  };
  const thirdResponse = await page.request.post(
    `${base}/impact/economics/assumptions`,
    {
      headers: {
        origin: "http://127.0.0.1:53000",
        "x-csrf-token": me.csrf_token,
      },
      data: {
        ...second.command,
        request_id: randomUUID(),
        expected_previous_version_id: second.id,
        wage_per_hour: "62",
        evidence:
          "Actual second-tab draft supersedes the captured browser review.",
      },
    },
  );
  expect(thirdResponse.status()).toBe(201);
  const third = (await thirdResponse.json()) as EconomicsAssumptionView;
  const rejected = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/assumptions/${second.id}/approve`) &&
      response.request().method() === "POST",
  );
  await review
    .getByRole("button", { name: "Approve reviewed assumptions", exact: true })
    .click();
  const rejectedResponse = await rejected;
  expect(rejectedResponse.status()).toBe(409);
  expect(rejectedResponse.request().postDataJSON().reviewed_hash).toBe(
    second.reviewed_hash,
  );
  await expect(review).toContainText("captured review is no longer current");
  await expect(
    review.getByRole("button", {
      name: "Approve reviewed assumptions",
      exact: true,
    }),
  ).toBeDisabled();
  const final = await read();
  expect(final.latest_assumption_id).toBe(third.id);
  expect(final.current_approval_id).toBe(approved.approval_id);
  expect(final.assumptions.filter((row) => row.approval_id)).toHaveLength(1);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    panel.getByRole("button", {
      name: "Refresh operating evidence",
      exact: true,
    }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBe(true);
});
