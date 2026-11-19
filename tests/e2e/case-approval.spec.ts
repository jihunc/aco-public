import { expect, test, type Page } from "@playwright/test";
import { randomUUID } from "node:crypto";
import type {
  RunView,
  ApprovalView,
  InvoiceView,
  InvoiceWorkspace,
  IngressSetupView,
  IngressActivationView,
  IngressReceiptView,
  CaseOutcomesView,
} from "../../web/src/lib/generated-api";

async function signIn(page: Page, password: string, email?: string) {
  await page.goto("/login");
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  if (email) {
    await page.getByRole("link", { name: "Register", exact: true }).click();
    for (const [name, value] of [
      ["Username", email],
      ["Email", email],
      ["First name", "Synthetic"],
      ["Last name", "Approval initiator"],
      ["Password", password],
      ["Confirm password", password],
    ])
      await page.getByRole("textbox", { name, exact: true }).fill(value);
    await page.getByRole("button", { name: "Register", exact: true }).click();
  } else {
    await page.getByLabel("Username or email").fill("owner@example.test");
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Sign In", exact: true }).click();
  }
  await expect(page).toHaveURL(/\/onboarding$/);
}

for (const modifyProposal of [false, true]) {
  test(`case task review uses exact source and independent approval with persisted business-impact attribution${modifyProposal ? " — modified proposal" : ""}`, async ({
    page,
    browser,
  }) => {
    test.setTimeout(180_000);
    expect(process.env.ACO_TEST_PROFILE).toBe("synthetic-inbound");
    const password = process.env.ACO_TEST_DEMO_PASSWORD;
    if (!password) throw Error("Explicit synthetic browser password required");
    await signIn(
      page,
      password,
      `internal-approval-${randomUUID()}@example.test`,
    );
    const me = await (await page.request.get("/api/me")).json();
    const headers = {
      origin: "http://127.0.0.1:53000",
      "x-csrf-token": me.csrf_token,
    };
    const created = await page.request.post("/api/workspaces", {
      headers,
      data: { name: `Independent approval ${randomUUID()}`, timezone: "UTC" },
    });
    expect(created.status()).toBe(201);
    const workspace = (await created.json()).id as string;
    const base = `/api/workspaces/${workspace}`;
    expect(
      (
        await page.request.post(`${base}/demo/accounting/seed`, {
          headers,
          data: {},
        })
      ).status(),
    ).toBe(201);
    const invited = await page.request.post(`${base}/invitations`, {
      headers,
      data: { email: "owner@example.test", role: "finance_manager" },
    });
    expect(invited.status()).toBe(201);
    const inviteCode = (await invited.json()).invite_code as string;
    const invoices = (await (
      await page.request.get(`${base}/receivables?page_size=100`)
    ).json()) as { items: InvoiceView[] };
    const invoice = invoices.items.find((row) => row.number === "INV-101");
    if (!invoice) throw Error("Actual synthetic source invoice required");
    const beforeResponse = await page.request.get(
      `${base}/receivables/${invoice.id}`,
    );
    expect(beforeResponse.status()).toBe(200);
    const before = (await beforeResponse.json()) as InvoiceWorkspace;
    const prep = await page.request.post(`${base}/email-ingress/setup`, {
      headers,
      data: {
        request_id: randomUUID(),
        evidence: "Owner prepares fixed synthetic case review source",
      },
    });
    expect(prep.status()).toBe(200);
    const setup = (await prep.json()) as IngressSetupView;
    const activated = await page.request.post(
      `${base}/email-ingress/activations`,
      {
        headers,
        data: {
          email_connection_id: setup.email_connections[0].id,
          storage_connection_id: setup.storage_connections[0].id,
          evidence: "Synthetic private original for case review only",
          expires_in_seconds: 3600,
        },
      },
    );
    expect(activated.status()).toBe(200);
    const activation = (await activated.json()) as IngressActivationView;
    const sample = await page.request.post(
      `${base}/email-ingress/activations/${activation.id}/simulate`,
      {
        headers,
        data: {
          request_id: randomUUID(),
          expected_activation_version: activation.version,
          invoice_ids: [invoice.id],
          scenario: "case_reply",
        },
      },
    );
    expect(sample.status()).toBe(202);
    const receiptId = ((await sample.json()) as IngressReceiptView).id;
    const readReceipt = async () =>
      (await (
        await page.request.get(`${base}/email-ingress/receipts/${receiptId}`)
      ).json()) as IngressReceiptView;
    await expect
      .poll(async () => (await readReceipt()).state, { timeout: 60_000 })
      .toBe("processed");
    const receipt = await readReceipt();
    if (!receipt.communication_id)
      throw Error("Original captured source required");
    await page.goto(`/receivables/${invoice.id}?workspace=${workspace}`);
    const capture = page.getByRole("region", {
      name: "Record source-backed case",
      exact: true,
    });
    await capture
      .getByLabel("Case source message", { exact: true })
      .selectOption(receipt.communication_id);
    await capture
      .getByLabel("Claim identifier", { exact: true })
      .fill(`review-${randomUUID()}`);
    await capture
      .getByLabel("Issue description", { exact: true })
      .fill("Review the original synthetic purchase order dispute");
    const caseResponse = page.waitForResponse(
      (r) =>
        r.request().method() === "POST" &&
        new URL(r.url()).pathname === `${base}/cases`,
    );
    await capture
      .getByRole("button", { name: "Record collection case", exact: true })
      .click();
    const savedCase = await caseResponse;
    expect(savedCase.status()).toBe(201);
    const caseId = (await savedCase.json()).case.id as string;
    await expect(page).toHaveURL(new RegExp(`/cases/${caseId}`));
    const started = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname === `${base}/agent-runs`,
    );
    await page
      .getByRole("button", {
        name: "Request independent task review",
        exact: true,
      })
      .click();
    const response = await started;
    expect(response.status()).toBe(202);
    const runId = (await response.json()).id as string;
    const readRun = async () =>
      (await (
        await page.request.get(`${base}/agent-runs/${runId}`)
      ).json()) as RunView;
    await expect
      .poll(async () => (await readRun()).status, { timeout: 45_000 })
      .toBe("awaiting_approval");
    const pending = await readRun();
    const proposal = pending.result?.proposals.find(
      (item) => item.kind === "request_task_approval",
    );
    expect(pending.authenticity).toBe("FAKE");
    expect(proposal?.executed).toBe(false);
    expect(proposal?.changed_ids).toEqual([]);
    if (!proposal?.approval_id)
      throw Error("Persisted independent approval required");
    let approvalId = proposal.approval_id;
    const originalApprovalId = approvalId;
    const self = (await (
      await page.request.get(`${base}/approvals/${approvalId}`)
    ).json()) as ApprovalView;
    expect(self.can_approve).toBe(false);
    expect(self.execution_status).toBe("pending_approval");
    expect(self.changed_ids).toEqual([]);
    await expect(
      page.getByRole("link", {
        name: "Open approval and decision history",
        exact: true,
      }),
    ).toHaveAttribute(
      "href",
      `/review?workspace=${workspace}&approval=${approvalId}`,
    );
    if (modifyProposal) {
      await page.goto(`/review?workspace=${workspace}&approval=${approvalId}`);
      const original = page.getByRole("article", {
        name: `Approval ${approvalId}`,
        exact: true,
      });
      await original
        .getByRole("button", { name: "Modify proposal", exact: true })
        .click();
      let editor = page.getByRole("region", {
        name: "Modify captured proposal",
        exact: true,
      });
      await editor
        .getByLabel("Revised task title", { exact: true })
        .fill("Cancelled unsaved edit");
      await editor
        .getByRole("button", { name: "Cancel modification", exact: true })
        .click();
      expect(
        (
          await (
            await page.request.get(`${base}/approvals/${approvalId}`)
          ).json()
        ).material_hash,
      ).toBe(self.material_hash);
      await original
        .getByRole("button", { name: "Modify proposal", exact: true })
        .click();
      editor = page.getByRole("region", {
        name: "Modify captured proposal",
        exact: true,
      });
      await editor
        .getByLabel("Revised task title", { exact: true })
        .fill("Human corrected documentary review");
      await editor
        .getByRole("textbox", { name: "Revised task notes", exact: true })
        .fill(
          "Review the retained source and record findings; no financial amendment.",
        );
      await editor
        .getByLabel("Modification reason", { exact: true })
        .fill(
          "Owner clarifies internal work while retaining exact case and invoice scope.",
        );
      const savedResponse = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          new URL(response.url()).pathname ===
            `${base}/approvals/${approvalId}/modifications`,
      );
      await editor
        .getByRole("button", {
          name: "Save replacement for independent approval",
          exact: true,
        })
        .click();
      const modified = await savedResponse;
      expect(modified.status()).toBe(200);
      const replacement = (await modified.json()) as ApprovalView;
      approvalId = replacement.id;
      expect(replacement.supersedes_id).toBe(originalApprovalId);
      expect(replacement.can_approve).toBe(false);
      expect(replacement.status).toBe("pending");
      await page
        .getByRole("link", { name: "Review saved replacement", exact: true })
        .click();
      await expect(page).toHaveURL(
        new RegExp(`/review\\?workspace=${workspace}&approval=${approvalId}`),
      );
      await page.reload();
      const savedRecord = page.getByRole("article", {
        name: `Approval ${approvalId}`,
        exact: true,
      });
      await expect(savedRecord).toContainText(
        "Human corrected documentary review",
      );
      await expect(
        savedRecord.getByRole("link", {
          name: "Original proposal and decisions",
          exact: true,
        }),
      ).toHaveAttribute(
        "href",
        `/review?workspace=${workspace}&approval=${originalApprovalId}`,
      );
      const previous = (await (
        await page.request.get(`${base}/approvals/${originalApprovalId}`)
      ).json()) as ApprovalView;
      expect(previous.status).toBe("superseded");
      expect(previous.proposal).toEqual(self.proposal);
      expect(previous.can_approve).toBe(false);
      expect(previous.replacement_id).toBe(approvalId);
      await page.goto(`/agent-runs/${runId}?workspace=${workspace}`);
    }
    const context = await browser.newContext();
    try {
      const reviewer = await context.newPage();
      await signIn(reviewer, password);
      await reviewer.getByLabel("Join code", { exact: true }).fill(inviteCode);
      await reviewer
        .getByRole("button", { name: "Accept invitation", exact: true })
        .click();
      await reviewer.goto(
        `/review?workspace=${workspace}&approval=${approvalId}`,
      );
      const approval = reviewer.getByRole("article", {
        name: `Approval ${approvalId}`,
        exact: true,
      });
      await expect(approval).toContainText("Internal task proposal");
      await expect(approval).toContainText("Reviewed case version 1");
      await expect(
        approval.getByRole("link", { name: "Open bound case", exact: true }),
      ).toHaveAttribute("href", `/cases/${caseId}?workspace=${workspace}`);
      await expect(approval).toContainText("Awaiting independent approval");
      await approval
        .getByRole("button", { name: "Review proposal", exact: true })
        .click();
      const captured = reviewer.getByRole("region", {
        name: "Review captured proposal",
        exact: true,
      });
      await captured
        .getByLabel("Decision reason", { exact: true })
        .fill(
          "Independent Finance Manager reviewed exact internal task, invoice and genuine saved source reads. No outreach authorized.",
        );
      const decision = reviewer.waitForResponse(
        (result) =>
          result.request().method() === "POST" &&
          new URL(result.url()).pathname ===
            `${base}/approvals/${approvalId}/decisions`,
      );
      await captured
        .getByRole("button", { name: "Approve proposal", exact: true })
        .click();
      expect((await decision).status()).toBe(200);
      await expect
        .poll(async () => (await readRun()).status, { timeout: 45_000 })
        .toBe("completed");
      const final = await readRun();
      const result = final.result?.proposals.find(
        (item) => item.approval_id === approvalId,
      );
      expect(result?.executed).toBe(true);
      expect(result?.approval_status).toBe("executed");
      expect(result?.changed_ids).toHaveLength(1);
      const saved = (await (
        await reviewer.request.get(`${base}/approvals/${approvalId}`)
      ).json()) as ApprovalView;
      expect(saved.effective_status).toBe("consumed");
      expect(saved.execution_status).toBe("executed");
      expect(saved.dispatch_status).toBe("NOT_APPLICABLE");
      expect(saved.changed_ids).toEqual(result?.changed_ids);
      const taskId = saved.changed_ids?.[0];
      if (!taskId || saved.changed_ids?.length !== 1)
        throw new Error("Exactly one persisted approved task required");
      await expect(
        reviewer.getByRole("link", { name: "Open created task", exact: true }),
      ).toBeVisible({ timeout: 15_000 });
      await reviewer
        .getByRole("link", { name: "Open created task", exact: true })
        .click();
      await expect(reviewer).toHaveURL(new RegExp(`/tasks/${taskId}`));
      await expect(
        reviewer.getByText(`Task reference: ${taskId}`, {
          exact: true,
        }),
      ).toBeVisible();
      if (modifyProposal) {
        const taskResponse = await reviewer.request.get(
          `${base}/tasks/${taskId}`,
        );
        expect(taskResponse.status()).toBe(200);
        expect((await taskResponse.json()).title).toBe(
          "Human corrected documentary review",
        );
      }
      await reviewer.reload();
      await expect(
        reviewer.getByText(`Task reference: ${taskId}`, {
          exact: true,
        }),
      ).toBeVisible();
      await page.reload();
      await expect(
        page.getByRole("link", {
          name: `Open task ${taskId}`,
          exact: true,
        }),
      ).toBeVisible();
      await page.setViewportSize({ width: 390, height: 844 });
      await expect
        .poll(() =>
          page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        )
        .toBe(true);
      const current = await (await page.request.get("/api/me")).json();
      expect(
        current.workspaces.find(
          (item: { id: string }) => item.id === workspace,
        ),
      ).toMatchObject({ outbound_paused: true, automation_enabled: false });
      const outgoing = await page.request.get(
        `${base}/communications?direction=outgoing`,
      );
      expect(outgoing.status()).toBe(200);
      expect((await outgoing.json()).total).toBe(0);
      const afterResponse = await page.request.get(
        `${base}/receivables/${invoice.id}`,
      );
      expect(afterResponse.status()).toBe(200);
      const after = (await afterResponse.json()) as InvoiceWorkspace;
      expect(after.invoice.source.revision).toBe(
        before.invoice.source.revision,
      );
      expect(after.invoice.outstanding).toEqual(before.invoice.outstanding);
      const until = new Date(Date.now() + 60_000).toISOString();
      const since = new Date(Date.now() - 86_400_000).toISOString();
      const outcomes = (await (
        await page.request.get(
          `${base}/impact/case-outcomes?${new URLSearchParams({ since, until })}`,
        )
      ).json()) as CaseOutcomesView;
      expect(outcomes.case_approval_count).toBe(1);
      expect(outcomes.resolved_count).toBe(0);
      expect(
        outcomes.cases.find((c) => c.case_id === caseId)?.approval_request_ids,
      ).toEqual([approvalId]);
      await page.goto(`/business-impact?workspace=${workspace}`);
      await page.getByText(/Inspect recorded case outcomes/).click();
      await expect(
        page.getByRole("link", {
          name: `Open case approval ${approvalId}`,
          exact: true,
        }),
      ).toBeVisible();
      await page.reload();
      await page.getByText(/Inspect recorded case outcomes/).click();
      await page
        .getByRole("link", {
          name: `Open case approval ${approvalId}`,
          exact: true,
        })
        .click();
      await expect(page).toHaveURL(new RegExp(`approval=${approvalId}`));
    } finally {
      await context.close();
    }
  });
}
