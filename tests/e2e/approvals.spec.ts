import { expect, test, type Page, type Route } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";

const origin = "http://127.0.0.1:53000";
const literalPattern = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const env = Object.fromEntries(
  (existsSync(".env") ? readFileSync(".env", "utf8") : "")
    .split("\n")
    .filter((line) => line.includes("="))
    .map((line) => [
      line.slice(0, line.indexOf("=")),
      line.slice(line.indexOf("=") + 1),
    ]),
);
const password = process.env.ACO_TEST_DEMO_PASSWORD || env.DEMO_PASSWORD;
type Workspace = {
  id: string;
  name: string;
  role: string;
  permissions: string[];
  automation_enabled: boolean;
  outbound_paused: boolean;
};
type Me = {
  id: string;
  email: string;
  csrf_token: string;
  workspaces: Workspace[];
};
type Ref = {
  id: string;
  invoice_id: string;
  action_id: string;
  content: string;
};
type View = {
  id: string;
  version: number;
  action_version: number;
  material_hash: string;
  current_binding_hash: string;
  effective_status: string;
  can_approve: boolean;
  can_deny: boolean;
  dispatch_status: string;
  proposal: { content: string; destination: string };
  invoice_scope: { synthetic: boolean; source_revision: string };
  decisions: { approver_id: string; decision: string; reason: string }[];
};
type Evidence = {
  synthetic: boolean;
  action_executions: number;
  dispatch_bindings: number;
  communications: number;
  dispatch_tickets: number;
  decisions: { request_id: string; approver_id: string; decision: string }[];
  actions: { id: string; status: string; kind: string }[];
};
function state<T>(
  command: "prepare" | "change-source" | "evidence",
  workspace: string,
  approval?: string,
): T {
  expect(process.env.ACO_TEST_PROFILE ?? "mock").toBe("mock");
  const args = [
    "tests/approval_browser_state.py",
    command,
    "--workspace",
    workspace,
  ];
  if (approval) args.push("--approval", approval);
  return JSON.parse(
    execFileSync(".venv/bin/python", args, {
      encoding: "utf8",
      timeout: 30_000,
      env: {
        ...process.env,
        ACO_TEST_PROFILE: "mock",
        PYTHONPATH: "src:tests:.",
      },
    }),
  ) as T;
}
async function login(page: Page): Promise<void> {
  await page.goto(`${origin}/login`);
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Username or email").fill("owner@example.test");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await expect(page).toHaveURL(/\/onboarding$/);
}
async function register(page: Page, email: string): Promise<void> {
  await page.goto(`${origin}/login`);
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  await page.getByRole("link", { name: "Register", exact: true }).click();
  await page
    .getByRole("textbox", { name: "Username", exact: true })
    .fill(email);
  await page.getByRole("textbox", { name: "Email", exact: true }).fill(email);
  await page
    .getByRole("textbox", { name: "First name", exact: true })
    .fill("Synthetic");
  await page
    .getByRole("textbox", { name: "Last name", exact: true })
    .fill("Approval reviewer");
  await page
    .getByRole("textbox", { name: "Password", exact: true })
    .fill(password);
  await page
    .getByRole("textbox", { name: "Confirm password", exact: true })
    .fill(password);
  await page.getByRole("button", { name: "Register", exact: true }).click();
  await expect(page).toHaveURL(/\/onboarding$/);
}

test("independent human review persists exact decisions and refuses stale captured source without dispatch", async ({
  page,
  browser,
}) => {
  test.setTimeout(180_000);
  expect(process.env.ACO_TEST_PROFILE ?? "mock").toBe("mock");
  const proposerEmail = `approval-proposer-${randomUUID()}@example.test`;
  await register(page, proposerEmail);
  const name = `Approval browser ${randomUUID()}`;
  await page.getByLabel("Workspace name", { exact: true }).fill(name);
  await page
    .getByRole("button", { name: "Create demo workspace", exact: true })
    .click();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  await expect(
    page.getByText("Outbound paused · Automation off", { exact: true }),
  ).toBeVisible();
  const owner = (await (await page.request.get("/api/me")).json()) as Me;
  expect(owner.email).toBe(proposerEmail);
  const workspace = owner.workspaces.find((item) => item.name === name);
  expect(workspace?.role).toBe("owner");
  expect(workspace?.outbound_paused).toBe(true);
  expect(workspace?.automation_enabled).toBe(false);
  if (!workspace) throw new Error("Fresh actual Owner workspace missing");
  const base = `/api/workspaces/${workspace.id}`;
  const seeded = await page.request.post(`${base}/demo/accounting/seed`, {
    headers: { origin, "x-csrf-token": owner.csrf_token },
    data: {},
  });
  expect(seeded.status()).toBe(201);
  const email = "owner@example.test";
  await page.getByLabel("Teammate email", { exact: true }).fill(email);
  await page
    .getByLabel("Role", { exact: true })
    .selectOption("finance_manager");
  await page
    .getByRole("button", { name: "Create invitation", exact: true })
    .click();
  const code = await page
    .getByLabel("Invitation code", { exact: true })
    .inputValue();
  const context = await browser.newContext();
  try {
    const reviewerPage = await context.newPage();
    await login(reviewerPage);
    expect((await reviewerPage.request.get(`${origin}${base}`)).status()).toBe(
      403,
    );
    await reviewerPage.getByLabel("Join code", { exact: true }).fill(code);
    await reviewerPage
      .getByRole("button", { name: "Accept invitation", exact: true })
      .click();
    await expect(
      reviewerPage.getByRole("heading", { name, exact: true }),
    ).toBeVisible();
    const reviewer = (await (
      await reviewerPage.request.get(`${origin}/api/me`)
    ).json()) as Me;
    expect(reviewer.email).toBe(email);
    expect(reviewer.id).not.toBe(owner.id);
    const membership = reviewer.workspaces.find(
      (item) => item.id === workspace.id,
    );
    expect(membership?.role).toBe("finance_manager");
    expect(membership?.permissions).toContain("approve_external");
    const prepared = state<{ approvals: Ref[]; synthetic: boolean }>(
      "prepare",
      workspace.id,
    );
    expect(prepared.synthetic).toBe(true);
    expect(prepared.approvals).toHaveLength(2);
    const [first, second] = prepared.approvals;
    const get = async (id: string): Promise<View> => {
      const response = await reviewerPage.request.get(
        `${origin}${base}/approvals/${id}`,
      );
      expect(response.status()).toBe(200);
      return (await response.json()) as View;
    };
    const original = await get(first.id);
    expect(original.can_approve).toBe(true);
    expect(original.invoice_scope.synthetic).toBe(true);
    const self = (await (
      await page.request.get(`${base}/approvals/${first.id}`)
    ).json()) as View;
    expect(self.can_approve).toBe(false);
    expect(self.can_deny).toBe(true);
    await reviewerPage.goto(`${origin}/review?workspace=${workspace.id}`);
    await expect(
      reviewerPage.getByRole("heading", {
        name: "Human approvals",
        exact: true,
      }),
    ).toBeVisible();
    const firstRecord = reviewerPage.getByRole("article", {
      name: `Approval ${first.id}`,
      exact: true,
    });
    await expect(
      firstRecord.getByText(first.content, { exact: true }),
    ).toBeVisible();
    await firstRecord
      .getByRole("button", { name: "Review proposal", exact: true })
      .click();
    let captured = reviewerPage.getByRole("region", {
      name: "Review captured proposal",
      exact: true,
    });
    await expect(
      captured.getByText(original.proposal.destination, { exact: false }),
    ).toBeVisible();
    await captured
      .getByText(/Inspect accounting source/)
      .first()
      .click();
    await expect(captured.locator("pre").first()).toContainText("provenance");
    await captured
      .getByLabel("Decision reason", { exact: true })
      .fill(
        "Independently inspected exact synthetic content, recipient and accounting evidence",
      );
    await captured
      .getByLabel("Approval valid for (hours)", { exact: true })
      .fill("1");
    const saved = reviewerPage.waitForResponse(
      (r) =>
        r.request().method() === "POST" &&
        r.url().endsWith(`/approvals/${first.id}/decisions`),
    );
    await captured
      .getByRole("button", { name: "Approve proposal", exact: true })
      .focus();
    await reviewerPage.keyboard.press("Enter");
    const approved = await saved;
    expect(approved.status()).toBe(200);
    const submitted = approved.request().postDataJSON() as Record<
      string,
      unknown
    >;
    expect(submitted).toMatchObject({
      expected_version: original.version,
      reviewed_action_version: original.action_version,
      reviewed_material_hash: original.material_hash,
      reviewed_binding_hash: original.current_binding_hash,
      decision: "approved",
      expires_in_seconds: 3600,
    });
    await expect(
      reviewerPage.getByRole("status").filter({ hasText: "Approval recorded" }),
    ).toBeVisible();
    await reviewerPage.reload();
    const persisted = await get(first.id);
    expect(persisted.effective_status).toBe("approved");
    expect(persisted.dispatch_status).toBe("NOT_ENABLED");
    expect(persisted.decisions).toHaveLength(1);
    expect(persisted.decisions[0].approver_id).toBe(reviewer.id);
    await reviewerPage
      .getByRole("article", { name: `Approval ${first.id}`, exact: true })
      .getByText("Review history and reference", { exact: true })
      .click();
    await expect(
      reviewerPage
        .getByRole("article", { name: `Approval ${first.id}`, exact: true })
        .getByText(persisted.decisions[0].reason, { exact: true }),
    ).toBeVisible();

    const before = await get(second.id);
    await reviewerPage
      .getByRole("article", { name: `Approval ${second.id}`, exact: true })
      .getByRole("button", { name: "Review proposal", exact: true })
      .click();
    captured = reviewerPage.getByRole("region", {
      name: "Review captured proposal",
      exact: true,
    });
    await captured
      .getByLabel("Decision reason", { exact: true })
      .fill("Review captured before synthetic source revision changed");
    const approvalsUrl = `${origin}${base}/approvals`;
    const decisionUrl = `${approvalsUrl}/${second.id}/decisions`;
    // Genuine pre-mutation response, retained verbatim for the bounded stale-browser race below.
    const priorListResponse = await reviewerPage.request.get(approvalsUrl);
    expect(priorListResponse.status()).toBe(200);
    const priorListBody = await priorListResponse.text();
    expect((JSON.parse(priorListBody) as { items: View[] }).items.find(
      (record) => record.id === second.id,
    )?.can_approve).toBe(true);
    let secondDecisionRequests = 0;
    let releasedAtPost = false;
    reviewerPage.on("request", (request) => {
      if (request.method() === "POST" && request.url() === decisionUrl) {
        secondDecisionRequests += 1;
        releasedAtPost = true;
      }
    });
    const revision = state<{
      previous_revision: string;
      current_revision: string;
    }>("change-source", workspace.id, second.id);
    expect(revision.previous_revision).toBe(
      before.invoice_scope.source_revision,
    );
    expect(revision.current_revision).not.toBe(revision.previous_revision);
    const financialSource = await reviewerPage.request.get(
      `${origin}${base}/receivables/${second.invoice_id}`,
    );
    expect(financialSource.status()).toBe(200);
    const financialInvoice = (await financialSource.json()).invoice;
    const retainedFinancialSource = {
      original: financialInvoice.original,
      outstanding: financialInvoice.outstanding,
      financial_status: financialInvoice.financial_status,
      source: financialInvoice.source,
    };
    // Current-query branch: genuine refresh sees the new source and refuses locally.
    const currentList = reviewerPage.waitForResponse(
      (response) => response.request().method() === "GET" && response.url() === approvalsUrl,
    );
    await reviewerPage.getByRole("button", { name: "Refresh approvals", exact: true }).click();
    expect((await currentList).status()).toBe(200);
    await expect(captured.getByRole("button", { name: "Approve proposal", exact: true })).toBeDisabled();
    await expect(captured.getByRole("alert")).toContainText("Your captured review has been kept");
    expect(secondDecisionRequests).toBe(0);
    expect((await get(second.id)).decisions).toHaveLength(0);
    console.log("APPROVAL_CURRENT_QUERY_REFUSAL", JSON.stringify({ disabled: true, decision_posts: secondDecisionRequests }));

    // Explicit stale-browser snapshot fixture: replay only this exact real prior GET body.
    // Polling/current guards stay enabled. POST and every business outcome remain real.
    let replayedLists = 0;
    let replayExceeded = false;
    const replayLimit = 4;
    const replayPriorList = async (route: Route) => {
      expect(route.request().method()).toBe("GET");
      if (releasedAtPost) {
        await route.continue();
        return;
      }
      if (replayedLists >= replayLimit) {
        replayExceeded = true;
        await route.continue();
        return;
      }
      replayedLists += 1;
      await route.fulfill({ status: 200, contentType: "application/json", body: priorListBody });
    };
    // The real POST is never intercepted. Its request event immediately restores real GET reads.
    await reviewerPage.route(approvalsUrl, replayPriorList);
    try {
      const replayed = reviewerPage.waitForResponse(
        (response) => response.request().method() === "GET" && response.url() === approvalsUrl,
      );
      await reviewerPage.getByRole("button", { name: "Refresh approvals", exact: true }).click();
      expect((await replayed).status()).toBe(200);
      await expect(captured.getByRole("button", { name: "Approve proposal", exact: true })).toBeEnabled();
      expect(releasedAtPost).toBe(false);
      const deniedStale = reviewerPage.waitForResponse(
        (response) => response.request().method() === "POST" && response.url() === decisionUrl,
        { timeout: 15_000 },
      );
      await captured.getByRole("button", { name: "Approve proposal", exact: true }).click({ timeout: 5_000 });
      const stale = await deniedStale;
      expect(stale.status()).toBe(409);
      expect(stale.request().postDataJSON()).toMatchObject({
        expected_version: before.version,
        reviewed_action_version: before.action_version,
        reviewed_material_hash: before.material_hash,
        reviewed_binding_hash: before.current_binding_hash,
      });
      expect(replayedLists).toBeGreaterThan(0);
      expect(replayedLists).toBeLessThanOrEqual(replayLimit);
      expect(replayExceeded).toBe(false);
      expect(releasedAtPost).toBe(true);
      expect(secondDecisionRequests).toBe(1);
      console.log("APPROVAL_STALE_BROWSER_SNAPSHOT", JSON.stringify({ replayed_lists: replayedLists, released_at_real_post: releasedAtPost, actual_status: stale.status() }));
    } finally {
      if (!reviewerPage.isClosed()) await reviewerPage.unroute(approvalsUrl, replayPriorList);
    }
    await expect(captured.getByRole("alert")).toContainText(
      "Your captured review has been kept",
    );
    await expect(
      captured.getByRole("button", { name: "Approve proposal", exact: true }),
    ).toBeDisabled();
    expect((await get(second.id)).decisions).toHaveLength(0);
    await reviewerPage
      .getByRole("button", { name: "Refresh approvals", exact: true })
      .click();
    await expect(
      captured.getByRole("button", { name: "Approve proposal", exact: true }),
    ).toBeDisabled();
    await expect(
      captured.getByRole("region", { name: "Invoice scope", exact: true }).getByText(/^Observed /),
    ).toHaveText(new RegExp(` · Revision ${literalPattern(revision.previous_revision)}$`));
    await captured
      .getByRole("button", { name: "Close review", exact: true })
      .click();
    await reviewerPage
      .getByRole("article", { name: `Approval ${second.id}`, exact: true })
      .getByRole("button", { name: "Review proposal", exact: true })
      .click();
    captured = reviewerPage.getByRole("region", {
      name: "Review captured proposal",
      exact: true,
    });
    await expect(
      captured.getByRole("region", { name: "Invoice scope", exact: true }).getByText(/^Observed /),
    ).toHaveText(new RegExp(` · Revision ${literalPattern(revision.current_revision)}$`));
    await expect(
      captured.getByRole("button", { name: "Approve proposal", exact: true }),
    ).toHaveCount(0);
    await captured
      .getByLabel("Decision reason", { exact: true })
      .fill("Deny after explicitly reviewing changed synthetic source");
    const denial = reviewerPage.waitForResponse(
      (r) =>
        r.request().method() === "POST" &&
        r.url().endsWith(`/approvals/${second.id}/decisions`),
    );
    await captured
      .getByRole("button", { name: "Deny proposal", exact: true })
      .click();
    expect((await denial).status()).toBe(200);
    await reviewerPage.reload();
    const rejected = await get(second.id);
    expect(rejected.effective_status).toBe("denied");
    expect(rejected.decisions).toHaveLength(1);
    expect(rejected.decisions[0].approver_id).toBe(reviewer.id);
    await reviewerPage.setViewportSize({ width: 390, height: 844 });
    expect(
      await reviewerPage.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const final = state<Evidence>("evidence", workspace.id);
    expect(final.action_executions).toBe(0);
    expect(final.dispatch_bindings).toBe(0);
    expect(final.communications).toBe(0);
    expect(final.dispatch_tickets).toBe(0);
    expect(final.decisions).toHaveLength(2);
    expect(final.actions).toHaveLength(2);
    expect(
      final.actions.every(
        (action) =>
          action.kind === "accounting_recheck" && action.status === "queued",
      ),
    ).toBe(true);
    const current = (await (
      await page.request.get(`${base}`)
    ).json()) as Workspace;
    expect(current.outbound_paused).toBe(true);
    expect(current.automation_enabled).toBe(false);
    const finalFinancialSource = await reviewerPage.request.get(
      `${origin}${base}/receivables/${second.invoice_id}`,
    );
    expect(finalFinancialSource.status()).toBe(200);
    expect((await finalFinancialSource.json()).invoice).toMatchObject(retainedFinancialSource);
  } finally {
    await context.close();
  }
});
