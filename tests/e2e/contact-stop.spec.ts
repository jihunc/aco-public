import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import type {
  ContactStopView,
  IngressReceiptView,
  IngressSetupView,
  ReleaseContactStop,
  RunView,
  PageInvoiceView,
} from "../../web/src/lib/generated-api";

test("fixed synthetic incoming stop reaches actual guarded customer holds and exact human disposition", async ({
  page,
}) => {
  test.setTimeout(180_000);
  page.setDefaultTimeout(15_000);
  expect(process.env.ACO_TEST_PROFILE).toBe("synthetic-inbound");
  const password = process.env.ACO_TEST_DEMO_PASSWORD;
  if (!password)
    throw new Error("Explicit synthetic browser password is required");
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
    data: { name: `Contact stop ${randomUUID()}`, timezone: "UTC" },
  });
  expect(created.status()).toBe(201);
  const workspace = ((await created.json()) as { id: string }).id,
    base = `/api/workspaces/${workspace}`;
  expect(
    (
      await page.request.post(`${base}/demo/accounting/seed`, {
        headers,
        data: {},
      })
    ).status(),
  ).toBe(201);
  const invoices = (await (
    await page.request.get(`${base}/receivables?page_size=100`)
  ).json()) as PageInvoiceView;
  const invoice = invoices.items.find((i) => i.number === "INV-101");
  if (!invoice) throw new Error("Canonical seed invoice missing");
  const before = invoices.items
    .map((i) => ({
      id: i.id,
      balance: i.outstanding,
      status: i.financial_status,
    }))
    .sort((a, b) => a.id.localeCompare(b.id));
  await page.goto(`/settings/email?workspace=${workspace}`);
  const inbox = page.getByRole("region", {
    name: "Synthetic inbound email",
    exact: true,
  });
  const prepared = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      r.url().endsWith(`${base}/email-ingress/setup`),
  );
  await inbox
    .getByRole("button", { name: "Prepare synthetic inbox", exact: true })
    .click();
  expect((await prepared).status()).toBe(200);
  const setup = (await (
    await page.request.get(`${base}/email-ingress/setup`)
  ).json()) as IngressSetupView;
  await inbox
    .getByRole("combobox", { name: "Demo email connection", exact: true })
    .selectOption(setup.email_connections[0].id);
  await inbox
    .getByRole("combobox", { name: "Private source storage", exact: true })
    .selectOption(setup.storage_connections[0].id);
  await inbox
    .getByRole("checkbox", {
      name: /I reviewed this synthetic private capture/,
    })
    .check();
  const enabled = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      r.url().endsWith(`${base}/email-ingress/activations`),
  );
  await inbox
    .getByRole("button", {
      name: "Enable synthetic inbound for one hour",
      exact: true,
    })
    .click();
  const activation = (await (await enabled).json()) as {
    id: string;
    version: number;
  };
  await inbox
    .getByRole("checkbox", { name: "Select inbound INV-101", exact: true })
    .check();
  await inbox
    .getByRole("combobox", { name: "Synthetic message scenario", exact: true })
    .selectOption("contact_stop_reply");
  const simulated = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      r.url().endsWith(`/activations/${activation.id}/simulate`),
  );
  await inbox
    .getByRole("button", { name: "Capture synthetic sample", exact: true })
    .click();
  const response = await simulated;
  expect(response.status()).toBe(202);
  const firstReceipt = (await response.json()) as IngressReceiptView;
  async function waitRun(id: string) {
    const path = `${base}/email-ingress/receipts/${id}`;
    await expect
      .poll(
        async () =>
          ((await (await page.request.get(path)).json()) as IngressReceiptView)
            .state,
        { timeout: 60_000 },
      )
      .toBe("processed");
    const receipt = (await (
      await page.request.get(path)
    ).json()) as IngressReceiptView;
    if (!receipt.agent_run_id || !receipt.communication_id)
      throw new Error("Actual persisted source/run required");
    const runPath = `${base}/agent-runs/${receipt.agent_run_id}`;
    await expect
      .poll(
        async () =>
          ((await (await page.request.get(runPath)).json()) as RunView).status,
        { timeout: 60_000 },
      )
      .toBe("completed");
    const run = (await (await page.request.get(runPath)).json()) as RunView;
    expect(run.authenticity).toBe("FAKE");
    expect(run.provider_attempted).toBe(false);
    const proposal = run.result?.proposals.find(
      (p) => p.kind === "request_collection_hold",
    );
    expect(proposal?.executed).toBe(true);
    expect(proposal?.changed_ids).toHaveLength(4);
    return { receipt, run };
  }
  const first = await waitRun(firstReceipt.id);
  const detail = `/receivables/${invoice.id}?workspace=${workspace}`,
    reviewPath = `${base}/invoices/${invoice.id}/contact-stops`;
  await page.goto(detail);
  const panel = page.getByRole("region", {
    name: "Customer contact restrictions",
    exact: true,
  });
  await expect(panel).toContainText("4 of 4 original holds remain pending");
  await expect(
    panel.getByRole("link", {
      name: "Read original incoming message",
      exact: true,
    }),
  ).toHaveAttribute(
    "href",
    `/communications/${first.receipt.communication_id}?workspace=${workspace}`,
  );
  await panel
    .getByRole("link", { name: "Read original incoming message", exact: true })
    .click();
  await page.getByRole("button", { name: "Open message", exact: true }).click();
  await expect(
    page.getByRole("link", {
      name: "Download authenticated source",
      exact: true,
    }),
  ).toBeVisible();
  await page.goto(detail);

  await panel
    .getByLabel("Human disposition reason", { exact: true })
    .fill(
      "Owner explicitly reviews the exact fixed synthetic customer request",
    );
  await panel
    .getByRole("button", {
      name: "Review release of this customer restriction",
      exact: true,
    })
    .click();
  const captured = panel.getByRole("region", {
    name: "Captured contact restriction review",
    exact: true,
  });
  // Compare every material child (including collapsed source/hold references),
  // separately from the legitimate mutation-state buttons.
  const capturedMaterial = captured.locator(":scope > :not(button)");
  await expect(captured).toBeVisible();
  const firstCapture = await capturedMaterial.allTextContents();
  const storageKey = `aco-contact-stop:${workspace}:${invoice.id}`;
  const saved = await page.evaluate(
    (key) =>
      JSON.parse(localStorage.getItem(key) ?? "null") as {
        command: ReleaseContactStop;
        review: ContactStopView;
      },
    storageKey,
  );
  // A second genuine current-human disposition makes the original capture stale.
  expect(
    (
      await page.request.post(
        `${base}/contact-stops/${saved.review.action_id}/release`,
        { headers, data: { ...saved.command, request_id: randomUUID() } },
      )
    ).status(),
  ).toBe(200);
  const staleReply = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      r.url().endsWith(`${base}/contact-stops/${saved.review.action_id}/release`),
  );
  await panel
    .getByRole("button", { name: "Confirm reviewed release", exact: true })
    .click();
  const rejected = await staleReply;
  expect(rejected.status()).toBe(409);
  expect(rejected.request().postDataJSON()).toEqual(saved.command);
  await expect(panel.getByRole("alert")).toContainText(
    "Explicitly refresh and review",
  );
  await expect(capturedMaterial).toHaveText(firstCapture);
  expect(
    await page.evaluate(
      (key) => JSON.parse(localStorage.getItem(key) ?? "null"),
      storageKey,
    ),
  ).toEqual(saved);
  await expect(
    captured.getByRole("button", {
      name: "Retry same captured release",
      exact: true,
    }),
  ).toBeDisabled();
  await panel
    .getByRole("button", {
      name: "Refresh and review current restrictions",
      exact: true,
    })
    .click();
  await expect(captured).toHaveCount(0);
  // A new fixed incoming original produces a new guarded stop, not a fabricated hold fixture.
  const second = await page.request.post(
    `${base}/email-ingress/activations/${activation.id}/simulate`,
    {
      headers,
      data: {
        request_id: randomUUID(),
        expected_activation_version: activation.version,
        invoice_ids: [invoice.id],
        scenario: "contact_stop_reply",
      },
    },
  );
  expect(second.status()).toBe(202);
  await waitRun(((await second.json()) as IngressReceiptView).id);
  await page.reload();
  await expect(panel).toContainText("4 of 4 original holds remain pending");
  await panel
    .getByLabel("Human disposition reason", { exact: true })
    .fill(
      "Owner reviews new source; independent holds and recipient controls stay unchanged",
    );
  await panel
    .getByRole("button", {
      name: "Review release of this customer restriction",
      exact: true,
    })
    .click();
  await expect(captured).toBeVisible();
  const exact = await capturedMaterial.allTextContents();
  const stored = await page.evaluate(
    (key) =>
      JSON.parse(localStorage.getItem(key) ?? "null") as {
        command: ReleaseContactStop;
        review: ContactStopView;
      },
    storageKey,
  );
  const releasePath = `${base}/contact-stops/${stored.review.action_id}/release`;
  await page.route(`**${releasePath}`, async (route) => {
    const result = await route.fetch();
    expect(result.status()).toBe(200);
    await route.abort("failed");
  });
  await panel
    .getByRole("button", { name: "Confirm reviewed release", exact: true })
    .click();
  await expect(panel.getByRole("alert")).toContainText(
    "could not be confirmed",
  );
  await expect(capturedMaterial).toHaveText(exact);
  expect(
    await page.evaluate(
      (key) => JSON.parse(localStorage.getItem(key) ?? "null"),
      storageKey,
    ),
  ).toEqual(stored);
  await expect(
    captured.getByRole("button", {
      name: "Retry same captured release",
      exact: true,
    }),
  ).toBeEnabled();
  await page.unroute(`**${releasePath}`);
  await page.reload();
  await expect(capturedMaterial).toHaveText(exact);
  expect(
    await page.evaluate(
      (key) => JSON.parse(localStorage.getItem(key) ?? "null"),
      storageKey,
    ),
  ).toEqual(stored);
  const retried = page.waitForResponse(
    (r) => r.request().method() === "POST" && r.url().endsWith(releasePath),
  );
  await panel
    .getByRole("button", { name: "Confirm reviewed release", exact: true })
    .click();
  const retry = await retried;
  expect(retry.status()).toBe(200);
  expect(retry.request().postDataJSON()).toEqual(stored.command);
  await expect(panel.getByRole("status")).toContainText("was released");
  await page.reload();
  const reviews = (await (await page.request.get(reviewPath)).json()) as {
    items: ContactStopView[];
  };
  expect(reviews.items).toHaveLength(2);
  expect(reviews.items.every((r) => !r.pending)).toBe(true);
  const after = (await (
    await page.request.get(`${base}/receivables?page_size=100`)
  ).json()) as PageInvoiceView;
  expect(
    after.items
      .map((i) => ({
        id: i.id,
        balance: i.outstanding,
        status: i.financial_status,
      }))
      .sort((a, b) => a.id.localeCompare(b.id)),
  ).toEqual(before);
  const session = (await (await page.request.get("/api/me")).json()) as {
    workspaces: {
      id: string;
      outbound_paused: boolean;
      automation_enabled: boolean;
    }[];
  };
  expect(session.workspaces.find((w) => w.id === workspace)).toMatchObject({
    outbound_paused: true,
    automation_enabled: false,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
});
