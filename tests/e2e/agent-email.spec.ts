import { expect, test, type Page } from "@playwright/test";
import { createHash, randomUUID } from "node:crypto";
import type { RunView, ApprovalView } from "../../web/src/lib/generated-api";

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

test("approved replacement email passes actual worker guards and reloads its private original", async ({
  page,
  browser,
}) => {
  test.setTimeout(180_000);
  expect(process.env.ACO_TEST_PROFILE).toBe("synthetic-agent-email");
  const password = process.env.ACO_TEST_DEMO_PASSWORD;
  if (!password) throw Error("Explicit synthetic browser password required");
  await signIn(page, password, `email-review-${randomUUID()}@example.test`);
  const me = await (await page.request.get("/api/me")).json();
  const headers = {
    origin: "http://127.0.0.1:53000",
    "x-csrf-token": me.csrf_token,
  };
  const created = await page.request.post("/api/workspaces", {
    headers,
    data: { name: `Email review ${randomUUID()}`, timezone: "UTC" },
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
  expect(
    (
      await page.request.post(`${base}/email-ingress/setup`, {
        headers,
        data: {
          request_id: randomUUID(),
          evidence:
            "Owner prepares actual private storage for fixed offline email proof",
        },
      })
    ).status(),
  ).toBe(200);
  const invoices = await (
    await page.request.get(`${base}/receivables?page_size=100`)
  ).json();
  const invoice = invoices.items.find(
    (item: { number: string }) => item.number === "INV-101",
  );
  if (!invoice) throw Error("Actual seeded invoice required");
  const financial = {
    status: invoice.financial_status,
    amount: invoice.outstanding.minor_units,
  };
  const recipients = await (
    await page.request.get(`${base}/policy-recipients`)
  ).json();
  const recipient = recipients.find(
    (item: { customer_id: string }) => item.customer_id === invoice.customer.id,
  );
  if (!recipient) throw Error("Actual source contact required");
  const offset = 12 - new Date().getUTCHours();
  const zone =
    offset === 0
      ? "Etc/GMT"
      : `Etc/GMT${offset > 0 ? "-" : "+"}${Math.abs(offset)}`;
  expect(
    (
      await page.request.put(
        `${base}/policy-recipients/${recipient.contact_id}`,
        {
          headers,
          data: {
            expected_version: recipient.version,
            expected_contact_revision: recipient.source_revision,
            expected_destination: recipient.destination,
            timezone: zone,
            timezone_verified: true,
            owner_confirmed_tenant_timezone: false,
            destination_verified: true,
            allowed_channels: ["email"],
            opt_out: false,
            evidence: "Reviewed synthetic recipient local business hour",
          },
        },
      )
    ).status(),
  ).toBe(200);
  expect(
    (
      await page.request.post(`${base}/policies`, {
        headers,
        data: {
          name: "Reviewed agent email",
          expected_version: 0,
          activate: true,
          native_reminders_disabled: true,
          configuration_reviewed: true,
          definition: {
            rules: [
              {
                id: randomUUID(),
                offset_days: 120,
                local_time: "09:00",
                channel: "email",
                template_id: "routine",
              },
            ],
            autonomy: "draft_and_review",
            weekdays: [0, 1, 2, 3, 4],
            opens_at: "09:00",
            closes_at: "17:00",
          },
        },
      })
    ).status(),
  ).toBe(201);
  const invited = await page.request.post(`${base}/invitations`, {
    headers,
    data: { email: "owner@example.test", role: "finance_manager" },
  });
  expect(invited.status()).toBe(201);
  const code = (await invited.json()).invite_code as string;
  await page.goto(`/settings/email?workspace=${workspace}`);
  await expect(
    page.getByText("Email execution is enabled, subject to reviewed scope and current guards."),
  ).toBeVisible();
  await page
    .getByLabel("Sender email", { exact: true })
    .fill("collections@aco.example.test");
  await page
    .getByLabel("Permitted recipient emails", { exact: true })
    .fill(recipient.destination);
  await page.getByLabel("Maximum requests", { exact: true }).fill("4");
  await page
    .getByLabel("Review evidence", { exact: true })
    .fill(
      "Explicit fixed synthetic sender, recipient and private original only",
    );
  await page
    .getByLabel("I reviewed synthetic source capture.", { exact: true })
    .check();
  await page
    .getByLabel("I reviewed this sender and every destination.", {
      exact: true,
    })
    .check();
  await page
    .getByRole("button", { name: "Save synthetic activation", exact: true })
    .click();
  await expect(
    page.getByText("Synthetic email activation reviewed and saved."),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Request workspace resume", exact: true })
    .click();
  await expect(
    page.getByText(
      "Workspace outbound resume requested; current guards still apply.",
    ),
  ).toBeVisible();
  await page.goto(`/agents?workspace=${workspace}`);
  await page.getByLabel("Find invoices", { exact: true }).fill("INV-101");
  await page
    .getByRole("button", { name: "Search invoices", exact: true })
    .click();
  await page.getByLabel("Select INV-101", { exact: true }).check();
  await page
    .getByRole("button", {
      name: "Use synthetic email review objective",
      exact: true,
    })
    .click();
  const start = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      new URL(r.url()).pathname === `${base}/agent-runs`,
  );
  await page
    .getByRole("button", { name: "Start investigation", exact: true })
    .click();
  const started = await start;
  expect(started.status()).toBe(202);
  const runId = (await started.json()).id as string;
  const readRun = async () =>
    (await (
      await page.request.get(`${base}/agent-runs/${runId}`)
    ).json()) as RunView;
  await expect
    .poll(async () => (await readRun()).status, { timeout: 45000 })
    .toBe("awaiting_approval");
  const initial = await readRun();
  const proposed = initial.result?.proposals.find(
    (p) => p.kind === "request_email_send",
  );
  if (!proposed?.approval_id) throw Error("Actual email review required");
  const oldId = proposed.approval_id;
  const old = (await (
    await page.request.get(`${base}/approvals/${oldId}`)
  ).json()) as ApprovalView;
  expect(old.can_approve).toBe(false);
  expect(
    (
      await (
        await page.request.get(
          `${base}/communications?invoice_id=${invoice.id}`,
        )
      ).json()
    ).total,
  ).toBe(0);
  await page.goto(`/review?workspace=${workspace}&approval=${oldId}`);
  const record = page.getByRole("article", {
    name: `Approval ${oldId}`,
    exact: true,
  });
  await record
    .getByRole("button", { name: "Modify proposal", exact: true })
    .click();
  let editor = page.getByRole("region", {
    name: "Modify captured proposal",
    exact: true,
  });
  await editor
    .getByRole("textbox", { name: "Revised message content", exact: true })
    .fill("Cancelled unsaved email");
  await editor
    .getByRole("button", { name: "Cancel modification", exact: true })
    .click();
  expect(
    (await (await page.request.get(`${base}/approvals/${oldId}`)).json())
      .material_hash,
  ).toBe(old.material_hash);
  await record
    .getByRole("button", { name: "Modify proposal", exact: true })
    .click();
  editor = page.getByRole("region", {
    name: "Modify captured proposal",
    exact: true,
  });
  const exactText =
    "Human reviewed synthetic replacement: please contact our team to clarify the retained invoice. No payment statement is asserted.";
  await editor
    .getByRole("textbox", { name: "Revised message content", exact: true })
    .fill(exactText);
  await editor
    .getByLabel("Modification reason", { exact: true })
    .fill(
      "Owner corrects wording, retaining exact source, recipient and scope",
    );
  const modifiedResponse = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      new URL(r.url()).pathname === `${base}/approvals/${oldId}/modifications`,
  );
  await editor
    .getByRole("button", {
      name: "Save replacement for independent approval",
      exact: true,
    })
    .click();
  const modified = await modifiedResponse;
  expect(modified.status()).toBe(200);
  const replacement = (await modified.json()) as ApprovalView;
  const approvalId = replacement.id;
  expect(replacement.can_approve).toBe(false);
  await page
    .getByRole("link", { name: "Review saved replacement", exact: true })
    .click();
  await expect(page).toHaveURL(
    new RegExp(`/review\\?workspace=${workspace}&approval=${approvalId}`),
  );
  await page.reload();
  await expect(
    page.getByRole("article", { name: `Approval ${approvalId}`, exact: true }),
  ).toContainText(exactText);
  const context = await browser.newContext();
  try {
    const reviewer = await context.newPage();
    await signIn(reviewer, password);
    await reviewer.getByLabel("Join code", { exact: true }).fill(code);
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
    await expect(approval).toContainText(exactText);
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
        "Independent Finance reviewed exact replacement, recipient, source, policy and synthetic-only cost scope",
      );
    const decision = reviewer.waitForResponse(
      (r) =>
        r.request().method() === "POST" &&
        new URL(r.url()).pathname ===
          `${base}/approvals/${approvalId}/decisions`,
    );
    await captured
      .getByRole("button", { name: "Approve proposal", exact: true })
      .click();
    expect((await decision).status()).toBe(200);
    await expect
      .poll(async () => (await readRun()).status, { timeout: 60000 })
      .toBe("completed");
    const final = await readRun();
    const outcome = final.result?.proposals.find(
      (p) => p.kind === "request_email_send",
    );
    expect(outcome?.executed).toBe(true);
    expect(outcome?.changed_ids).toHaveLength(1);
    expect(outcome?.email_command).toEqual(proposed.email_command);
    const messages = await (
      await page.request.get(`${base}/communications?invoice_id=${invoice.id}`)
    ).json();
    expect(messages.total).toBe(1);
    const message = messages.items[0];
    expect(message.id).toBe(outcome?.changed_ids?.[0]);
    expect(message.delivery_state).toBe("accepted");
    const source = message.sources.find(
      (s: { available: boolean }) => s.available,
    );
    if (!source) throw Error("Actual private original required");
    const raw = await page.request.get(
      `${base}/artifacts/${source.artifact_id}/content`,
    );
    expect(raw.status()).toBe(200);
    const bytes = await raw.body();
    expect(createHash("sha256").update(bytes).digest("hex")).toBe(
      source.sha256,
    );
    expect(bytes.toString("utf8").replace(/=\r?\n/g, "")).toContain(exactText);
    await page.goto(`/communications/${message.id}?workspace=${workspace}`);
    await page.reload();
    await page.getByRole("button", { name: "Open message", exact: true }).click();
    await expect(page.getByText(exactText, { exact: true })).toBeVisible();
    const saved = (await (
      await page.request.get(`${base}/approvals/${approvalId}`)
    ).json()) as ApprovalView;
    expect(saved.dispatch_status).toBe("ACCEPTED");
    expect(saved.effective_status).toBe("consumed");
    const historic = (await (
      await page.request.get(`${base}/approvals/${oldId}`)
    ).json()) as ApprovalView;
    expect(historic.status).toBe("superseded");
    expect(historic.proposal).toEqual(old.proposal);
    const current = await (
      await page.request.get(`${base}/receivables/${invoice.id}`)
    ).json();
    expect(current.invoice.financial_status).toBe(financial.status);
    expect(current.invoice.outstanding.minor_units).toBe(financial.amount);
    expect(
      (
        await (
          await page.request.get(
            `${base}/communications?invoice_id=${invoice.id}`,
          )
        ).json()
      ).total,
    ).toBe(1);
  } finally {
    await context.close();
  }
});
