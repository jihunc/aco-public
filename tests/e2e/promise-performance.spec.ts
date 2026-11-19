import { expect, test } from "@playwright/test";
import { createHash, randomUUID } from "node:crypto";
import type { InvoiceView as Invoice } from "../../web/src/lib/generated-api";

type PromiseView = {
  id: string;
  version: number;
  status: string;
  scheduling_status: string;
  verification_status: string;
  synthetic: boolean;
  review_task_id: string | null;
  history: { id: string; event: string; details: Record<string, unknown> }[];
};

test("synthetic promise detail retains actual queued checks and source-backed partial-invoice performance after reload", async ({
  page,
}) => {
  test.setTimeout(120_000);
  expect(process.env.ACO_TEST_PROFILE).toBe("synthetic-inbound");
  const password = process.env.ACO_TEST_DEMO_PASSWORD;
  if (!password)
    throw new Error("Explicit synthetic browser password required");
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
    data: { name: `Promise browser ${randomUUID()}`, timezone: "UTC" },
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
  const rows = (await (
    await page.request.get(`${base}/receivables?page_size=100`)
  ).json()) as { items: Invoice[] };
  const invoice = rows.items.find((row) => row.number === "INV-101");
  if (
    !invoice ||
    !invoice.verified ||
    !invoice.source.synthetic ||
    BigInt(invoice.outstanding.minor_units) <= 4000n
  )
    throw new Error("Verified synthetic open invoice required");

  // Real public preparation probes private storage; no READY/source/receipt fixture rows.
  const prepared = await page.request.post(`${base}/email-ingress/setup`, {
    headers,
    data: {
      request_id: randomUUID(),
      evidence:
        "Owner prepares an explicitly synthetic fixed promise sample for current source review.",
    },
  });
  expect(prepared.status()).toBe(200);
  const setup = (await prepared.json()) as {
    email_connections: { id: string }[];
    storage_connections: { id: string }[];
  };
  const activated = await page.request.post(
    `${base}/email-ingress/activations`,
    {
      headers,
      data: {
        email_connection_id: setup.email_connections[0].id,
        storage_connection_id: setup.storage_connections[0].id,
        evidence:
          "Owner reviewed synthetic-only private capture; no real customer or outreach authority.",
        expires_in_seconds: 3600,
      },
    },
  );
  expect(activated.status()).toBe(200);
  const activation = (await activated.json()) as {
    id: string;
    version: number;
  };
  const sample = await page.request.post(
    `${base}/email-ingress/activations/${activation.id}/simulate`,
    {
      headers,
      data: {
        request_id: randomUUID(),
        expected_activation_version: activation.version,
        invoice_ids: [invoice.id],
        scenario: "promise_reply",
      },
    },
  );
  expect(sample.status()).toBe(202);
  const receiptId = ((await sample.json()) as { id: string }).id;
  const readReceipt = async () =>
    (await (
      await page.request.get(`${base}/email-ingress/receipts/${receiptId}`)
    ).json()) as {
      state: string;
      communication_id: string | null;
      sensitive_action_allowed: boolean;
    };
  await expect
    .poll(async () => (await readReceipt()).state, { timeout: 60_000 })
    .toBe("processed");
  const receipt = await readReceipt();
  expect(receipt.sensitive_action_allowed).toBe(false);
  if (!receipt.communication_id)
    throw new Error("Actual stored promise source required");
  const source = (await (
    await page.request.get(`${base}/communications/${receipt.communication_id}`)
  ).json()) as {
    id: string;
    source_state: string;
    synthetic: boolean;
    content: { text_body: string };
    sources: {
      artifact_id: string;
      representation: string;
      available: boolean;
      sha256: string;
    }[];
  };
  expect(source.source_state).toBe("stored");
  expect(source.synthetic).toBe(true);
  expect(
    source.sources.some(
      (item) => item.representation === "provider_original" && item.available,
    ),
  ).toBe(true);
  const original = source.sources.find(
    (item) => item.representation === "provider_original" && item.available,
  );
  if (!original) throw new Error("Actual private original required");
  const bytes = await page.request.get(
    `${base}/artifacts/${original.artifact_id}/content`,
  );
  expect(bytes.status()).toBe(200);
  expect(
    createHash("sha256")
      .update(await bytes.body())
      .digest("hex"),
  ).toBe(original.sha256);
  const terms = source.content.text_body.match(
    /I commit to pay AUD 40\.00 for INV-101 by (\d{4}-\d{2}-\d{2}) in UTC\./,
  );
  if (!terms)
    throw new Error("Exact fixed synthetic commitment terms required");
  const evidence = {
    communication_id: source.id,
    excerpt: source.content.text_body,
    offset: 0,
  };
  await page.goto(`/receivables/${invoice.id}?workspace=${workspace}`);
  await expect(
    page.getByRole("button", { name: "Review promise terms", exact: true }),
  ).toBeVisible({ timeout: 5000 });
  await page
    .getByLabel("Promise source message", { exact: true })
    .selectOption(source.id);
  await page.getByLabel("Promised amount (AUD)", { exact: true }).fill("40.00");
  await page
    .getByLabel("Promised payment date", { exact: true })
    .fill(terms[1]);
  await page.getByLabel("Recipient timezone", { exact: true }).fill("UTC");
  await page
    .getByRole("button", { name: "Review promise terms", exact: true })
    .click();
  const capturedResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `${base}/promises`,
  );
  await page
    .getByRole("button", { name: "Record payment promise", exact: true })
    .click();
  const captured = await capturedResponse;
  expect(captured.status(), await captured.text()).toBe(201);
  const pending = (await captured.json()) as PromiseView;
  expect(pending.status).toBe("NEEDS_CONFIRMATION");
  await expect(page).toHaveURL(
    new RegExp(`/promises/${pending.id}\\?workspace=`),
  );
  await page
    .getByLabel("Review reason", { exact: true })
    .fill(
      "Authorized Owner confirms this exact synthetic AUD40 commitment, date, timezone and invoice scope from the original.",
    );
  await page
    .getByLabel(
      "I reviewed the source and confirm these exact commitment terms.",
      { exact: true },
    )
    .check();
  const confirmationResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname ===
        `${base}/promises/${pending.id}/confirm`,
  );
  await page
    .getByRole("button", { name: "Confirm commitment", exact: true })
    .click();
  const confirmed = await confirmationResponse;
  expect(confirmed.status(), await confirmed.text()).toBe(200);
  const active = (await confirmed.json()) as PromiseView;
  expect(active.status).toBe("ACTIVE");
  expect(active.scheduling_status).toBe("QUEUED");
  expect(active.verification_status).toBe("UNVERIFIED");
  const plans = (await (
    await page.request.get(`${base}/promises/${active.id}/check-plans`)
  ).json()) as { action_ids: string[]; event_id: string }[];
  expect(plans).toHaveLength(1);
  expect(plans[0].action_ids.length).toBeGreaterThan(0);
  await page.goto(`/promises/${active.id}?workspace=${workspace}`);
  await expect(page.getByText("Check scheduling:").locator("..")).toHaveText(
    "Check scheduling: Queued",
  );
  await expect(page.getByText("Source verification:").locator("..")).toHaveText(
    "Source verification: Not yet verified",
  );
  const checks = page.getByRole("region", {
    name: "Saved promise checks",
    exact: true,
  });
  await expect(checks).toContainText(plans[0].event_id);
  await checks.getByText(/Inspect saved action references/).click();
  await expect(
    checks.getByRole("link", {
      name: `Saved action data ${plans[0].action_ids[0]}`,
      exact: true,
    }),
  ).toHaveAttribute(
    "href",
    `${base}/scheduled-actions/${plans[0].action_ids[0]}`,
  );
  await expect(
    page.getByRole("link", { name: "Open source message", exact: true }),
  ).toHaveAttribute(
    "href",
    `/communications/${source.id}?workspace=${workspace}`,
  );

  // Genuine accounting adapter posts synthetic cash after confirmation; invoice remains open.
  const paid = await page.request.post(
    `${base}/demo/accounting/simulate-payment`,
    {
      headers,
      data: {
        provider_connection_id: invoice.source.connection_id,
        external_invoice_id: invoice.source.external_id,
        operation_id: randomUUID(),
        amount: { minor_units: 4000, currency: "AUD", exponent: 2 },
      },
    },
  );
  expect(paid.status()).toBe(200);
  const readPromise = async () =>
    (await (
      await page.request.get(`${base}/promises/${active.id}`)
    ).json()) as PromiseView;
  await expect
    .poll(async () => (await readPromise()).verification_status, {
      timeout: 30_000,
    })
    .toBe("VERIFIED");
  const performed = await readPromise();
  expect(performed.status).toBe("FULFILLED");
  const verified = performed.history.find(
    (event) => event.event === "promise.performance_verified",
  );
  if (!verified)
    throw new Error("Persisted verified performance history required");
  expect(verified.details.allocation_ids).not.toEqual([]);
  const afterInvoice = (await (
    await page.request.get(`${base}/receivables/${invoice.id}`)
  ).json()) as { invoice: Invoice };
  expect(BigInt(afterInvoice.invoice.outstanding.minor_units)).toBe(
    BigInt(invoice.outstanding.minor_units) - 4000n,
  );
  expect(afterInvoice.invoice.financial_status).toBe("partially_paid");
  await page.reload();
  await expect(
    page.getByText("FULFILLED", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.getByText("Source verification:").locator("..")).toHaveText(
    "Source verification: Source verification recorded",
  );
  await expect(
    page.getByText(`Immutable event ${verified.id}`, { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Recorded performance: ON TIME", { exact: true }),
  ).toBeVisible();
  const current = (await (await page.request.get("/api/me")).json()) as {
    workspaces: {
      id: string;
      outbound_paused: boolean;
      automation_enabled: boolean;
    }[];
  };
  expect(current.workspaces.find((row) => row.id === workspace)).toMatchObject({
    outbound_paused: true,
    automation_enabled: false,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(async () =>
      page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    )
    .toBe(true);
});

test("pending promise clarification uses new retained source and separate reviewed confirmation", async ({
  page,
}) => {
  test.setTimeout(150_000);
  expect(process.env.ACO_TEST_PROFILE).toBe("synthetic-inbound");
  const password = process.env.ACO_TEST_DEMO_PASSWORD;
  if (!password)
    throw new Error("Explicit synthetic browser password required");
  await page.goto("/login");
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Username or email").fill("owner@example.test");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await expect(page).toHaveURL(/\/onboarding$/);
  const me = await (await page.request.get("/api/me")).json();
  const headers = {
    origin: "http://127.0.0.1:53000",
    "x-csrf-token": me.csrf_token,
  };
  const created = await page.request.post("/api/workspaces", {
    headers,
    data: { name: `Clarify promise ${randomUUID()}`, timezone: "UTC" },
  });
  expect(created.status()).toBe(201);
  const workspace = (await created.json()).id as string,
    base = `/api/workspaces/${workspace}`;
  expect(
    (
      await page.request.post(`${base}/demo/accounting/seed`, {
        headers,
        data: {},
      })
    ).status(),
  ).toBe(201);
  const rows = (await (
    await page.request.get(`${base}/receivables?page_size=100`)
  ).json()) as { items: Invoice[] };
  const invoice = rows.items.find((row) => row.number === "INV-101");
  if (!invoice) throw new Error("Actual synthetic invoice required");
  const financial = {
    amount: invoice.outstanding,
    status: invoice.financial_status,
    revision: invoice.source.revision,
  };
  const prepared = await page.request.post(`${base}/email-ingress/setup`, {
    headers,
    data: {
      request_id: randomUUID(),
      evidence:
        "Owner prepares private fixed synthetic promise sources, no outreach.",
    },
  });
  expect(prepared.status()).toBe(200);
  const setup = await prepared.json();
  const activated = await page.request.post(
    `${base}/email-ingress/activations`,
    {
      headers,
      data: {
        email_connection_id: setup.email_connections[0].id,
        storage_connection_id: setup.storage_connections[0].id,
        evidence: "Owner reviewed fixed synthetic original capture only.",
        expires_in_seconds: 3600,
      },
    },
  );
  expect(activated.status()).toBe(200);
  const activation = await activated.json();
  async function source(scenario: "ambiguous_promise_reply" | "promise_reply") {
    const response = await page.request.post(
      `${base}/email-ingress/activations/${activation.id}/simulate`,
      {
        headers,
        data: {
          request_id: randomUUID(),
          expected_activation_version: activation.version,
          invoice_ids: [invoice!.id],
          scenario,
        },
      },
    );
    expect(response.status(), await response.text()).toBe(202);
    const receipt = await response.json();
    let communicationId: string | null = null;
    await expect
      .poll(
        async () => {
          const current = await (
            await page.request.get(
              `${base}/email-ingress/receipts/${receipt.id}`,
            )
          ).json();
          communicationId = current.communication_id;
          return current.state;
        },
        { timeout: 30_000 },
      )
      .toBe("processed");
    const message = (await (
      await page.request.get(`${base}/communications/${communicationId}`)
    ).json()) as {
      id: string;
      source_state: string;
      content: { text_body: string };
      event_at: string;
    };
    expect(message.source_state).toBe("stored");
    return message;
  }
  const ambiguous = await source("ambiguous_promise_reply");
  expect(ambiguous.content.text_body).toContain(
    "date and timezone are not yet agreed",
  );
  const originalResponse = await page.request.post(`${base}/promises`, {
    headers,
    data: {
      customer_id: invoice.customer.id,
      invoice_ids: [invoice.id],
      evidence: {
        communication_id: ambiguous.id,
        excerpt: ambiguous.content.text_body,
        offset: 0,
      },
      amount: { minor_units: 4000, currency: "AUD", exponent: 2 },
      due_date: null,
      timezone: null,
    },
  });
  expect(originalResponse.status()).toBe(201);
  const original = await originalResponse.json();
  const clarified = await source("promise_reply");
  const due = /by (\d{4}-\d{2}-\d{2}) in UTC/.exec(
    clarified.content.text_body,
  )?.[1];
  if (!due) throw new Error("Actual stored explicit source date required");
  await page.goto(`/promises/${original.id}?workspace=${workspace}`);
  await expect(
    page.getByRole("button", { name: "Confirm commitment", exact: true }),
  ).toBeDisabled();
  const panel = page.getByRole("region", {
    name: "Clarify pending promise",
    exact: true,
  });
  await expect(panel).toContainText("known customer");
  await panel
    .getByRole("combobox", { name: "Promise source message", exact: true })
    .selectOption(clarified.id);
  await panel.getByLabel("Promised amount (AUD)", { exact: true }).fill("41");
  await panel.getByLabel("Promised payment date", { exact: true }).fill(due);
  await panel.getByLabel("Recipient timezone", { exact: true }).fill("UTC");
  await panel
    .getByRole("button", { name: "Review promise terms", exact: true })
    .click();
  await expect(panel.getByRole("alert")).toContainText("preserve known amount");
  await panel.getByLabel("Promised amount (AUD)", { exact: true }).fill("40");
  await panel
    .getByRole("button", { name: "Review promise terms", exact: true })
    .click();
  await expect(
    panel.getByRole("region", { name: "Reviewed promise capture" }),
  ).toContainText("original remains pending");
  await panel
    .getByRole("button", { name: "Discard reviewed capture", exact: true })
    .click();
  await panel
    .getByRole("button", { name: "Review promise terms", exact: true })
    .click();
  const savedResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `${base}/promises` &&
      response.request().method() === "POST",
  );
  await panel
    .getByRole("button", { name: "Record proposed clarification", exact: true })
    .click();
  const saved = await savedResponse;
  expect(saved.status(), await saved.text()).toBe(201);
  const replacement = await saved.json();
  expect(replacement.status).toBe("NEEDS_CONFIRMATION");
  expect(replacement.replaces_promise_id).toBe(original.id);
  await expect(page).toHaveURL(new RegExp(`/promises/${replacement.id}`));
  let releasePredecessor: () => void = () => {};
  const predecessorReady = new Promise<void>((resolve) => {
    releasePredecessor = resolve;
  });
  const priorRoute = `**${base}/promises/${original.id}`;
  await page.route(priorRoute, async (route) => {
    await predecessorReady;
    await route.continue();
  });
  await page.reload();
  const review = page.getByRole("region", {
    name: "Human commitment review",
    exact: true,
  });
  await expect(
    review.getByRole("region", {
      name: "Captured predecessor lineage",
      exact: true,
    }),
  ).toContainText(original.id);
  const reviewedTerms = review.getByRole("checkbox", {
    name: "I reviewed the source and confirm these exact commitment terms.",
    exact: true,
  });
  // A delayed real predecessor read must not discard already typed input.
  await expect(reviewedTerms).toBeDisabled();
  const reason =
    "I reviewed both exact retained source statements and the unchanged original amount/scope.";
  await review
    .getByRole("textbox", { name: "Review reason", exact: true })
    .fill(reason);
  releasePredecessor();
  await expect(reviewedTerms).toBeEnabled();
  await page.unroute(priorRoute);
  await reviewedTerms.check();
  await expect(
    review.getByRole("textbox", { name: "Review reason", exact: true }),
  ).toHaveValue(reason);
  await expect(
    review.getByRole("button", { name: "Confirm commitment", exact: true }),
  ).toBeEnabled();
  const confirmedResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `${base}/promises/${replacement.id}/confirm` &&
      response.request().method() === "POST",
  );
  await review
    .getByRole("button", { name: "Confirm commitment", exact: true })
    .click();
  const confirmed = await confirmedResponse;
  expect(confirmed.status(), await confirmed.text()).toBe(200);
  expect((await confirmed.json()).status).toBe("ACTIVE");
  const prior = await (
    await page.request.get(`${base}/promises/${original.id}`)
  ).json();
  expect(prior.status).toBe("SUPERSEDED");
  expect(prior.source_text).toBe(ambiguous.content.text_body);
  expect(prior.superseded_by_id).toBe(replacement.id);
  expect(
    (
      await (
        await page.request.get(`${base}/communications/${ambiguous.id}`)
      ).json()
    ).content,
  ).toEqual(ambiguous.content);
  const current = (
    await (await page.request.get(`${base}/receivables/${invoice.id}`)).json()
  ).invoice as Invoice;
  expect({
    amount: current.outstanding,
    status: current.financial_status,
    revision: current.source.revision,
  }).toEqual(financial);
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Saved source checks", exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
