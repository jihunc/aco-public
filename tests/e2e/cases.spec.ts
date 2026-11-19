import { expect, test } from "@playwright/test";
import { createHash, randomUUID } from "node:crypto";
import type { InvoiceView as Invoice, CommunicationView as Source } from "../../web/src/lib/generated-api";

test("human multi-invoice case controls preserve source evidence, independent invoice disposition and recorded outcomes", async ({
  page,
}) => {
  test.setTimeout(120_000);
  page.setDefaultTimeout(15_000);
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
    data: { name: `Case browser ${randomUUID()}`, timezone: "UTC" },
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
  const invoice = rows.items.find((row) => row.number === "INV-102");
  if (
    !invoice ||
    !invoice.verified ||
    !invoice.source.synthetic ||
    BigInt(invoice.outstanding.minor_units) <= 4000n
  )
    throw new Error("Verified synthetic open invoice required");

  const secondary = rows.items.find(
    (row) =>
      row.id !== invoice.id &&
      row.customer.id === invoice.customer.id &&
      BigInt(row.outstanding.minor_units) > 0n,
  );
  if (!secondary)
    throw new Error(
      "Second actual open invoice for the same customer required",
    );

  // Real public preparation probes private storage; no READY/source/receipt fixture rows.
  const prepared = await page.request.post(`${base}/email-ingress/setup`, {
    headers,
    data: {
      request_id: randomUUID(),
      evidence:
        "Owner prepares an explicitly synthetic fixed case samples for current source review.",
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

  const simulate = async (
    scenario: "case_reply" | "case_resolution" | "contact_stop_reply",
  ) => {
    const sample = await page.request.post(
      `${base}/email-ingress/activations/${activation.id}/simulate`,
      {
        headers,
        data: {
          request_id: randomUUID(),
          expected_activation_version: activation.version,
          invoice_ids: [invoice.id, secondary.id],
          scenario,
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
      await page.request.get(
        `${base}/communications/${receipt.communication_id}`,
      )
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
    return source;
  };
  const source = await simulate("case_reply");
  expect(source.content.text_body).toContain(
    "purchase order reference is wrong",
  );
  await page.goto(`/receivables/${invoice.id}?workspace=${workspace}`);
  const capture = page.getByRole("region", {
    name: "Record source-backed case",
    exact: true,
  });
  await capture
    .getByLabel("Case source message", { exact: true })
    .selectOption(source.id);
  await capture
    .getByRole("checkbox", {
      name: `Include invoice ${secondary.id}`,
      exact: true,
    })
    .check();
  await capture
    .getByLabel("Claim identifier", { exact: true })
    .fill(`po-${randomUUID()}`);
  await capture
    .getByLabel("Issue description", { exact: true })
    .fill(
      "Synthetic customer disputes the PO reference on both explicitly selected invoices.",
    );
  const createdCase = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `${base}/cases` &&
      response.request().method() === "POST",
  );
  await capture
    .getByRole("button", { name: "Record collection case", exact: true })
    .click();
  const createdCaseResponse = await createdCase;
  expect(createdCaseResponse.status(), await createdCaseResponse.text()).toBe(
    201,
  );
  type Case = {
    id: string;
    version: number;
    status: string;
    invoice_links: {
      invoice_id: string;
      issue_state: string;
      hold_id: string | null;
    }[];
    evidence: { id: string; communication_id: string }[];
    history: { id: string; event: string; substantive_human: boolean }[];
  };
  const caseId = ((await createdCaseResponse.json()) as { case: Case }).case.id;
  await expect(page).toHaveURL(
    new RegExp(`/cases/${caseId}\\?workspace=${workspace}`),
  );
  const controls = page.getByRole("region", {
    name: "Case handling controls",
    exact: true,
  });
  const readCase = async () =>
    (await (await page.request.get(`${base}/cases/${caseId}`)).json()) as Case;
  const changeStatus = async (status: string, reason: string) => {
    await controls
      .getByRole("combobox", { name: "Next case status", exact: true })
      .selectOption(status);
    await controls
      .getByLabel("Case status reason", { exact: true })
      .fill(reason);
    const response = page.waitForResponse(
      (result) =>
        new URL(result.url()).pathname === `${base}/cases/${caseId}` &&
        result.request().method() === "PATCH",
    );
    await controls
      .getByRole("button", { name: "Save case status", exact: true })
      .click();
    const result = await response;
    expect(result.status(), await result.text()).toBe(200);
    await expect(controls).toContainText(
      `current version ${((await result.json()) as Case).version}`,
    );
  };
  expect((await readCase()).invoice_links).toHaveLength(2);
  await changeStatus(
    "INVESTIGATING",
    "Human reviews the original synthetic PO discrepancy.",
  );
  const resolutionSource = await simulate("case_resolution");
  const attach = controls.getByRole("form", {
    name: "Attach new case evidence",
    exact: true,
  });
  // Reload source choices after the actual worker has captured the second original.
  await page.reload();
  await attach
    .getByLabel("Case source message", { exact: true })
    .selectOption(resolutionSource.id);
  const attached = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `${base}/cases/${caseId}/evidence` &&
      response.request().method() === "POST",
  );
  await attach
    .getByRole("button", { name: "Attach source evidence", exact: true })
    .click();
  const attachedResponse = await attached;
  expect(attachedResponse.status(), await attachedResponse.text()).toBe(200);
  const current = await readCase();
  const evidence = current.evidence.find(
    (item) => item.communication_id === resolutionSource.id,
  );
  if (!evidence)
    throw new Error("Actual attached resolution evidence required");
  await expect(controls).toContainText(`current version ${current.version}`);
  for (const [index, row] of [invoice, secondary].entries()) {
    await controls
      .getByRole("combobox", { name: "Recorded case evidence", exact: true })
      .selectOption(evidence.id);
    await controls
      .getByRole("combobox", { name: "Case invoice to resolve", exact: true })
      .selectOption(row.id);
    await controls
      .getByLabel("Documented resolution action", { exact: true })
      .fill(
        "Human verified the PO reference against the exact stored documentary confirmation.",
      );
    const resolving = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname ===
          `${base}/cases/${caseId}/invoice-resolution` &&
        response.request().method() === "POST",
    );
    await controls
      .getByRole("button", {
        name: "Resolve selected invoice issue",
        exact: true,
      })
      .click();
    const result = await resolving;
    expect(result.status(), await result.text()).toBe(200);
    const saved = (await result.json()) as Case;
    expect(
      saved.invoice_links.filter((link) => link.issue_state === "RESOLVED"),
    ).toHaveLength(index + 1);
    expect(saved.status).toBe("INVESTIGATING");
    await expect(controls).toContainText(`current version ${saved.version}`);
  }
  await changeStatus(
    "RESOLVED",
    "Both invoice PO issues have actual documentary dispositions.",
  );
  await page.reload();
  expect((await readCase()).status).toBe("RESOLVED");
  const snapshots = await Promise.all(
    [invoice, secondary].map(
      async (row) =>
        (
          (await (
            await page.request.get(`${base}/receivables/${row.id}`)
          ).json()) as { invoice: Invoice }
        ).invoice,
    ),
  );
  for (const [index, row] of [invoice, secondary].entries())
    expect(snapshots[index].outstanding).toEqual(row.outstanding);
  const handling = page.getByRole("region", {
    name: "Reported case handling time",
    exact: true,
  });
  const eligible = (
    (await (
      await page.request.get(`${base}/cases/${caseId}/handling-time`)
    ).json()) as { can_record_history_ids: string[] }
  ).can_record_history_ids;
  expect(eligible.length).toBeGreaterThan(0);
  await handling
    .getByRole("combobox", { name: "Recorded work event", exact: true })
    .selectOption(eligible[0]);
  await handling
    .getByLabel("Reported active minutes", { exact: true })
    .fill("2.50");
  await handling
    .getByLabel("Handling report evidence", { exact: true })
    .fill(
      "I report two and a half active minutes for this synthetic documentary review event.",
    );
  const reported = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `${base}/cases/${caseId}/handling-time` &&
      response.request().method() === "POST",
  );
  await handling
    .getByRole("button", { name: "Record reported handling time", exact: true })
    .click();
  const recorded = await reported;
  expect(recorded.status(), await recorded.text()).toBe(201);
  const entry = (await recorded.json()) as {
    id: string;
    minutes: string;
    basis: string;
  };
  expect(entry.minutes).toBe("2.50");
  expect(entry.basis).toBe("MANUAL_REPORTED");
  await expect(handling).toContainText(entry.id);
  await page.reload();
  await expect(handling).toContainText(entry.id);
  await page.goto(`/business-impact?workspace=${workspace}`);
  const outcomes = page.getByRole("region", {
    name: "Recorded case outcomes",
    exact: true,
  });
  await expect(outcomes).toContainText("Synthetic case history");
  await outcomes
    .getByText("Inspect recorded case outcomes (1)", { exact: true })
    .click();
  await expect(
    outcomes.getByRole("link", { name: `Case ${caseId}`, exact: true }),
  ).toBeVisible();
  await expect(outcomes).toContainText("MANUAL_REPORTED");
  // The fixed incoming stop request creates its real guarded customer restriction.
  const stopSource = await simulate("contact_stop_reply");
  const stopsPath = `${base}/invoices/${invoice.id}/contact-stops`;
  await expect
    .poll(
      async () => {
        const value = await (await page.request.get(stopsPath)).json();
        return value.items.some(
          (item: { pending: boolean; evidence_complete: boolean }) =>
            item.pending && item.evidence_complete,
        );
      },
      { timeout: 60_000 },
    )
    .toBe(true);
  await page.goto(`/receivables/${invoice.id}?workspace=${workspace}`);
  await capture
    .getByLabel("Case source message", { exact: true })
    .selectOption(stopSource.id);
  await capture
    .getByRole("checkbox", {
      name: `Include invoice ${secondary.id}`,
      exact: true,
    })
    .check();
  await capture
    .getByLabel("Collection issue type", { exact: true })
    .selectOption("stop_contact");
  await capture
    .getByLabel("Claim identifier", { exact: true })
    .fill(`stop-${randomUUID()}`);
  await capture
    .getByLabel("Issue description", { exact: true })
    .fill(
      "Human verifies the existing enforced restriction for this synthetic request.",
    );
  const stopCreated = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `${base}/cases` &&
      response.request().method() === "POST",
  );
  await capture
    .getByRole("button", { name: "Record collection case", exact: true })
    .click();
  const stopCreatedResponse = await stopCreated;
  expect(stopCreatedResponse.status(), await stopCreatedResponse.text()).toBe(
    201,
  );
  const stopCase = ((await stopCreatedResponse.json()) as { case: Case }).case;
  await expect(page).toHaveURL(new RegExp(`/cases/${stopCase.id}`));
  await controls
    .getByLabel("Next case status", { exact: true })
    .selectOption("INVESTIGATING");
  await controls
    .getByLabel("Case status reason", { exact: true })
    .fill("Inspect actual current restriction and preserve its holds.");
  const investigating = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `${base}/cases/${stopCase.id}` &&
      response.request().method() === "PATCH",
  );
  await controls
    .getByRole("button", { name: "Save case status", exact: true })
    .click();
  const investigatingResponse = await investigating;
  expect(investigatingResponse.status()).toBe(200);
  const stopCurrent = (await investigatingResponse.json()) as Case;
  await expect(controls).toContainText(
    `current version ${stopCurrent.version}`,
  );
  await controls
    .getByLabel("Recorded case evidence", { exact: true })
    .selectOption(stopCurrent.evidence[0].id);
  await controls
    .getByLabel("Documented resolution action", { exact: true })
    .fill(
      "Current human verified canonical customer contact holds remain enforced; no contact resumes.",
    );
  const resolveButton = controls.getByRole("button", {
    name: "Resolve selected invoice issue",
    exact: true,
  });
  await expect(resolveButton).toBeDisabled();
  await controls
    .getByLabel("Case invoice to resolve", { exact: true })
    .selectOption(invoice.id);
  const refsReady = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
      `${base}/cases/${stopCase.id}/disposition-references`,
  );
  await controls
    .getByRole("button", {
      name: "Review current verified references",
      exact: true,
    })
    .click();
  const refsResponse = await refsReady;
  expect(refsResponse.status()).toBe(200);
  const refs = (await refsResponse.json()) as {
    items: { id: string; version: number; material_hash: string }[];
  };
  expect(refs.items.length).toBeGreaterThan(0);
  await controls
    .getByLabel("Reviewed disposition reference", { exact: true })
    .selectOption(refs.items[0].id);
  await expect(
    controls.getByRole("article", { name: "Captured disposition source" }),
  ).toContainText(refs.items[0].material_hash);
  // Switching invoice scope invalidates the visible review; returning does not silently reuse it.
  await controls
    .getByLabel("Case invoice to resolve", { exact: true })
    .selectOption(secondary.id);
  await controls
    .getByLabel("Case invoice to resolve", { exact: true })
    .selectOption(invoice.id);
  await expect(resolveButton).toBeDisabled();
  await expect(
    controls.getByRole("article", { name: "Captured disposition source" }),
  ).toHaveCount(0);
  await controls
    .getByRole("button", {
      name: "Review current verified references",
      exact: true,
    })
    .click();
  await controls
    .getByLabel("Reviewed disposition reference", { exact: true })
    .selectOption(refs.items[0].id);
  const dispositionSaved = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `${base}/cases/${stopCase.id}/invoice-resolution` &&
      response.request().method() === "POST",
  );
  await resolveButton.click();
  const dispositionResponse = await dispositionSaved;
  expect(dispositionResponse.status(), await dispositionResponse.text()).toBe(
    200,
  );
  const posted = dispositionResponse.request().postDataJSON();
  expect(posted.expected_reference_version).toBe(refs.items[0].version);
  expect(posted.expected_reference_hash).toBe(refs.items[0].material_hash);
  expect(
    ((await dispositionResponse.json()) as Case).invoice_links.find(
      (link) => link.invoice_id === invoice.id,
    )?.issue_state,
  ).toBe("RESOLVED");
  await page.reload();
  const persistedStop = (await (
    await page.request.get(`${base}/cases/${stopCase.id}`)
  ).json()) as Case;
  expect(
    persistedStop.invoice_links.find((link) => link.invoice_id === invoice.id)
      ?.issue_state,
  ).toBe("RESOLVED");
  const retainedStops = await (await page.request.get(stopsPath)).json();
  expect(
    retainedStops.items.some((item: { pending: boolean }) => item.pending),
  ).toBe(true);
  expect(
    (
      (await (
        await page.request.get(`${base}/receivables/${invoice.id}`)
      ).json()) as { invoice: Invoice }
    ).invoice.outstanding,
  ).toEqual(invoice.outstanding);
  const currentMembership = (await (
    await page.request.get("/api/me")
  ).json()) as {
    workspaces: {
      id: string;
      outbound_paused: boolean;
      automation_enabled: boolean;
    }[];
  };
  const membership = currentMembership.workspaces.find(
    (row) => row.id === workspace,
  );
  expect(membership?.outbound_paused).toBe(true);
  expect(membership?.automation_enabled).toBe(false);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(
      () =>
        page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      { timeout: 5000 },
    )
    .toBe(true);
  await expect
    .poll(
      () =>
        page.evaluate(() =>
          Array.from(document.querySelectorAll("main *"))
            .filter(
              (element) =>
                element.getBoundingClientRect().width > 0 &&
                element.getBoundingClientRect().right > innerWidth,
            )
            .map((element) => ({
              tag: element.tagName,
              css: element.className,
              right: element.getBoundingClientRect().right,
              text: element.textContent?.slice(0, 90),
            }))
            .slice(0, 12),
        ),
      { timeout: 5000 },
    )
    .toEqual([]);
});

test("reviewed communication links keep one original and expose unlinked source through Conversations", async ({ page }) => {
  test.setTimeout(150_000);
  page.setDefaultTimeout(15_000);
  expect(process.env.ACO_TEST_PROFILE).toBe("synthetic-inbound");
  const password = process.env.ACO_TEST_DEMO_PASSWORD;
  if (!password) throw new Error("Explicit synthetic browser password required");
  await page.goto("/login");
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Username or email").fill("owner@example.test");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await expect(page).toHaveURL(/\/onboarding$/);
  const me = await (await page.request.get("/api/me")).json() as { csrf_token: string };
  const headers = { origin: "http://127.0.0.1:53000", "x-csrf-token": me.csrf_token };
  const created = await page.request.post("/api/workspaces", { headers, data: { name: `Link review ${randomUUID()}`, timezone: "UTC" } });
  expect(created.status()).toBe(201);
  const workspace = (await created.json() as { id: string }).id;
  const base = `/api/workspaces/${workspace}`;
  expect((await page.request.post(`${base}/demo/accounting/seed`, { headers, data: {} })).status()).toBe(201);
  const invoiceRows = await (await page.request.get(`${base}/receivables?page_size=100`)).json() as { items: Invoice[] };
  const invoices = invoiceRows.items.filter(row => ["INV-101", "INV-107"].includes(row.number));
  expect(invoices).toHaveLength(2);
  expect(new Set(invoices.map(row => row.customer.id)).size).toBe(1);
  const before = invoices.map(row => [row.id, row.outstanding, row.financial_status]);
  const prepared = await page.request.post(`${base}/email-ingress/setup`, { headers, data: { request_id: randomUUID(), evidence: "Current Owner prepares fixed synthetic reference review with private originals." } });
  expect(prepared.status()).toBe(200);
  const setup = await prepared.json() as { email_connections: { id: string }[]; storage_connections: { id: string }[] };
  const enabled = await page.request.post(`${base}/email-ingress/activations`, { headers, data: { email_connection_id: setup.email_connections[0].id, storage_connection_id: setup.storage_connections[0].id, evidence: "Reviewed synthetic capture only; no real sender or outreach authority.", expires_in_seconds: 3600 } });
  expect(enabled.status()).toBe(200);
  const activation = await enabled.json() as { id: string; version: number };
  // Actual public UI creates a fixed native source, then maintained ingress captures it.
  await page.goto(`/settings/email?workspace=${workspace}`);
  const inbound = page.getByRole("region", { name: "Synthetic inbound email" });
  await inbound.getByLabel("Current inbound activation", { exact: true }).selectOption(activation.id);
  await inbound.getByRole("button", { name: "Review selected activation", exact: true }).click();
  await inbound.getByLabel("Select inbound INV-101", { exact: true }).check();
  await inbound.getByLabel("Select inbound INV-107", { exact: true }).check();
  await inbound.getByLabel("Synthetic message scenario", { exact: true }).selectOption("unverified_reference_reply");
  const capture = page.waitForResponse(r => r.request().method() === "POST" && r.url().endsWith(`/email-ingress/activations/${activation.id}/simulate`));
  await inbound.getByRole("button", { name: "Capture synthetic sample", exact: true }).click();
  const captured = await capture;
  expect(captured.status()).toBe(202);
  const receiptId = (await captured.json() as { id: string }).id;
  const receiptPath = `${base}/email-ingress/receipts/${receiptId}`;
  type Receipt = { state: string; communication_id: string | null; agent_run_id: string | null; review_required: boolean };
  const receipt = async () => await (await page.request.get(receiptPath)).json() as Receipt;
  await expect.poll(async () => (await receipt()).state, { timeout: 60_000 }).toBe("processed");
  const stored = await receipt();
  expect(stored.agent_run_id).toBeNull();
  expect(stored.review_required).toBe(true);
  if (!stored.communication_id) throw new Error("Actual retained source required");
  const sourcePath = `${base}/communications/${stored.communication_id}`;

  const source = async () => await (await page.request.get(sourcePath)).json() as Source;
  const original = await source();
  if (!original.content.text_body) throw new Error("Retained text body required");
  expect(original.invoice_links).toEqual([]);
  expect(original.sources.filter(row => row.representation === "provider_original")).toHaveLength(1);
  await page.goto(`/conversations?workspace=${workspace}`);
  await expect(page.getByText("No invoice is linked to this message. Its sender remains unverified.", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Review invoice links", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/communications/${stored.communication_id}`));
  const review = page.getByRole("region", { name: "Review invoice links", exact: true });
  await review.getByRole("button", { name: "Review current references", exact: true }).click();
  await review.getByLabel(/INV-101 · explicit reference/).check();
  await review.getByRole("button", { name: "Cancel / refresh review", exact: true }).click();
  expect((await source()).invoice_links).toEqual([]);
  await review.getByRole("button", { name: "Review current references", exact: true }).click();
  await review.getByLabel(/INV-101 · explicit reference/).check();
  await review.getByLabel(/INV-107 · explicit reference/).check();
  await review.getByRole("textbox", { name: "Exact source excerpt", exact: true }).fill("Fabricated excerpt");
  await review.getByLabel(/I reviewed the exact source and invoice references/).check();
  await review.getByRole("button", { name: "Save reviewed invoice links", exact: true }).click();
  await expect(review.getByRole("alert")).toContainText("match the original text");
  expect((await source()).invoice_links).toEqual([]);
  await review.getByRole("textbox", { name: "Exact source excerpt", exact: true }).fill(original.content.text_body);
  await review.getByLabel(/I reviewed the exact source and invoice references/).check();
  const saved = page.waitForResponse(r => r.request().method() === "POST" && r.url().endsWith(`${sourcePath}/invoice-links`));
  await review.getByRole("button", { name: "Save reviewed invoice links", exact: true }).click();
  expect((await saved).status()).toBe(200);
  await expect(review.getByRole("status")).toContainText("Invoice links saved");
  await page.reload();
  for (const invoice of invoices) await expect(page.getByRole("link", { name: invoice.number, exact: true })).toBeVisible();
  const linked = await source();
  expect(linked.invoice_links.map(row => row.invoice_id).sort()).toEqual(invoices.map(row => row.id).sort());
  expect(linked.invoice_links.every(row => row.provenance === "explicit_reference" && row.confidence === "contextual_unverified_sender" && row.evidence_excerpt === original.content.text_body)).toBe(true);
  expect(linked.sources).toEqual(original.sources);
  expect(linked.content).toEqual(original.content);
  expect(linked.thread_id).toBe(original.thread_id);
  expect(linked.origin_invoice_id).toBeNull();
  const after = await (await page.request.get(`${base}/receivables?page_size=100`)).json() as { items: Invoice[] };
  expect(after.items.filter(row => invoices.some(i => i.id === row.id)).map(row => [row.id, row.outstanding, row.financial_status])).toEqual(before);
  const customerHistory = await (await page.request.get(`${base}/communications?customer_id=${invoices[0].customer.id}`)).json() as { items: { id: string }[] };
  expect(customerHistory.items.filter(row => row.id === stored.communication_id)).toHaveLength(1);
  expect((await receipt()).agent_run_id).toBeNull();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
