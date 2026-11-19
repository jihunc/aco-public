import { expect, test } from "@playwright/test";
import { createHash, randomUUID } from "node:crypto";

type Workspace = {
  id: string;
  role: string;
  outbound_paused: boolean;
  automation_enabled: boolean;
};
type Setup = {
  enabled: boolean;
  can_prepare: boolean;
  email_connections: { id: string; status: string }[];
  storage_connections: { id: string; status: string }[];
  activations: { id: string; version: number }[];
};
type Receipt = {
  id: string;
  state: string;
  communication_id: string | null;
  agent_run_id: string | null;
  sender_authority: string;
  sensitive_action_allowed: boolean;
  error_code: string | null;
};
type Run = {
  id: string;
  status: string;
  authenticity: string;
  source_receipt_id: string | null;
  source_communication_id: string | null;
  result: {
    proposals: { kind: string; executed: boolean; changed_ids: string[] }[];
  } | null;
};
type Source = {
  artifact_id: string;
  representation: string;
  available: boolean;
  sha256: string;
};
type Message = {
  id: string;
  synthetic: boolean;
  direction: string;
  source_state: string;
  content: { text_body: string | null };
  sources: Source[];
  invoice_links: { invoice_id: string }[];
};
type Invoice = {
  id: string;
  number: string;
  financial_status: string;
  outstanding: { minor_units: string; currency: string; exponent: number };
  source: { synthetic: boolean };
  verified: boolean;
};

test("fresh Owner prepares a synthetic inbox and follows captured source through persisted FAKE investigation to its task", async ({
  page,
}) => {
  test.setTimeout(180_000);
  expect(process.env.ACO_TEST_PROFILE).toBe("synthetic-inbound");
  const password = process.env.ACO_TEST_DEMO_PASSWORD;
  if (!password)
    throw new Error("Explicit synthetic browser test password is required");
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
      name: `Inbound browser ${randomUUID()}`,
      timezone: "Australia/Melbourne",
    },
  });
  expect(created.status()).toBe(201);
  const workspace = ((await created.json()) as Workspace).id;
  const base = `/api/workspaces/${workspace}`;
  expect(
    (
      await page.request.post(`${base}/demo/accounting/seed`, {
        headers,
        data: {},
      })
    ).status(),
  ).toBe(201);
  const readWorkspace = async () => {
    const session = (await (await page.request.get("/api/me")).json()) as {
      workspaces: Workspace[];
    };
    const current = session.workspaces.find((item) => item.id === workspace);
    expect(current?.role).toBe("owner");
    expect(current?.outbound_paused).toBe(true);
    expect(current?.automation_enabled).toBe(false);
  };
  await readWorkspace();
  const beforeSetup = (await (
    await page.request.get(`${base}/email-ingress/setup`)
  ).json()) as Setup;
  expect(beforeSetup.can_prepare).toBe(true);
  expect(beforeSetup.enabled).toBe(false);
  expect(beforeSetup.email_connections).toHaveLength(0);
  expect(beforeSetup.storage_connections).toHaveLength(0);
  expect(beforeSetup.activations).toHaveLength(0);
  const invoices = (await (
    await page.request.get(`${base}/receivables?page_size=100`)
  ).json()) as { items: Invoice[] };
  const invoice = invoices.items.find((item) => item.number === "INV-101");
  if (!invoice) throw new Error("Actual seeded invoice unavailable");
  expect(invoice.source.synthetic).toBe(true);
  expect(invoice.verified).toBe(true);
  const financialBefore = {
    status: invoice.financial_status,
    outstanding: invoice.outstanding,
  };

  await page.goto(`/settings/email?workspace=${workspace}`);
  const inbound = page.getByRole("region", {
    name: "Synthetic inbound email",
    exact: true,
  });
  await expect(
    inbound.getByRole("heading", { name: "Try a synthetic inbound reply" }),
  ).toBeVisible();
  const preparation = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(`${base}/email-ingress/setup`),
  );
  await inbound
    .getByRole("button", { name: "Prepare synthetic inbox", exact: true })
    .click();
  expect((await preparation).status()).toBe(200);
  const setup = (await (
    await page.request.get(`${base}/email-ingress/setup`)
  ).json()) as Setup;
  expect(setup.enabled).toBe(true);
  expect(setup.activations).toHaveLength(0);
  expect(setup.email_connections.every((item) => item.status === "READY")).toBe(
    true,
  );
  expect(
    setup.storage_connections.every((item) => item.status === "READY"),
  ).toBe(true);
  expect(setup.email_connections.length).toBeGreaterThan(0);
  expect(setup.storage_connections.length).toBeGreaterThan(0);
  await inbound
    .getByLabel("Demo email connection", { exact: true })
    .selectOption(setup.email_connections[0].id);
  await inbound
    .getByLabel("Private source storage", { exact: true })
    .selectOption(setup.storage_connections[0].id);
  await inbound
    .getByRole("checkbox", {
      name: /I reviewed this synthetic private capture/,
    })
    .check();
  const activated = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(`${base}/email-ingress/activations`),
  );
  await inbound
    .getByRole("button", {
      name: "Enable synthetic inbound for one hour",
      exact: true,
    })
    .click();
  const activationResponse = await activated;
  expect(activationResponse.status()).toBe(200);
  const activation = (await activationResponse.json()) as {
    id: string;
    version: number;
  };
  await expect(
    inbound.getByRole("region", { name: "Reviewed inbound activation" }),
  ).toContainText(`Version ${activation.version}`);
  await inbound.getByLabel("Select inbound INV-101", { exact: true }).check();
  await inbound
    .getByLabel("Synthetic message scenario", { exact: true })
    .selectOption("invoice_reply");
  const submitted = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response
        .url()
        .endsWith(
          `${base}/email-ingress/activations/${activation.id}/simulate`,
        ),
  );
  await inbound
    .getByRole("button", { name: "Capture synthetic sample", exact: true })
    .focus();
  await page.keyboard.press("Enter");
  const response = await submitted;
  expect(response.status()).toBe(202);
  const command = response.request().postDataJSON() as {
    request_id: string;
    expected_activation_version: number;
    invoice_ids: string[];
    scenario: string;
  };
  expect(Object.keys(command).sort()).toEqual([
    "expected_activation_version",
    "invoice_ids",
    "request_id",
    "scenario",
  ]);
  expect(command.expected_activation_version).toBe(activation.version);
  expect(command.invoice_ids).toEqual([invoice.id]);
  expect(command.scenario).toBe("invoice_reply");
  const initial = (await response.json()) as Receipt;
  const bookmark = `/settings/email?workspace=${workspace}&receipt=${initial.id}`;
  await expect(page).toHaveURL(new RegExp(`receipt=${initial.id}`));
  // Closing the observer by navigating must not cancel the durable work.
  await page.goto(`/receivables?workspace=${workspace}`);
  const receiptPath = `${base}/email-ingress/receipts/${initial.id}`;
  await expect
    .poll(
      async () =>
        ((await (await page.request.get(receiptPath)).json()) as Receipt).state,
      { timeout: 60_000 },
    )
    .toBe("processed");
  const receipt = (await (
    await page.request.get(receiptPath)
  ).json()) as Receipt;
  expect(receipt.error_code).toBeNull();
  expect(receipt.sender_authority).toBe("UNVERIFIED");
  expect(receipt.sensitive_action_allowed).toBe(false);
  if (!receipt.communication_id || !receipt.agent_run_id)
    throw new Error("Committed source and investigation references required");
  const runPath = `${base}/agent-runs/${receipt.agent_run_id}`;
  await expect
    .poll(
      async () =>
        ((await (await page.request.get(runPath)).json()) as Run).status,
      { timeout: 60_000 },
    )
    .toBe("completed");
  const run = (await (await page.request.get(runPath)).json()) as Run;
  expect(run.authenticity).toBe("FAKE");
  expect(run.source_receipt_id).toBe(receipt.id);
  expect(run.source_communication_id).toBe(receipt.communication_id);
  const taskId = run.result?.proposals.find(
    (proposal) => proposal.kind === "create_task" && proposal.executed,
  )?.changed_ids[0];
  if (!taskId) throw new Error("Actual committed task outcome required");
  const actualTask = (await (
    await page.request.get(`${base}/tasks/${taskId}`)
  ).json()) as {
    id: string;
    title: string;
    invoice_id: string;
    deleted_at: string | null;
  };
  expect(actualTask.id).toBe(taskId);
  expect(actualTask.invoice_id).toBe(invoice.id);
  expect(actualTask.deleted_at).toBeNull();
  const source = (await (
    await page.request.get(`${base}/communications/${receipt.communication_id}`)
  ).json()) as Message;
  expect(source.synthetic).toBe(true);
  expect(source.direction).toBe("incoming");
  expect(source.source_state).toBe("stored");
  expect(source.invoice_links.map((link) => link.invoice_id)).toContain(
    invoice.id,
  );
  expect(source.content.text_body).toMatch(/synthetic/i);
  const originals = source.sources.filter(
    (item) => item.representation === "provider_original",
  );
  expect(originals).toHaveLength(1);
  expect(originals[0].available).toBe(true);
  const original = await page.request.get(
    `${base}/artifacts/${originals[0].artifact_id}/content`,
  );
  expect(original.status()).toBe(200);
  expect(
    createHash("sha256")
      .update(await original.body())
      .digest("hex"),
  ).toBe(originals[0].sha256);
  const replay = await page.request.post(
    `${base}/email-ingress/activations/${activation.id}/simulate`,
    { headers, data: command },
  );
  expect(replay.status()).toBe(202);
  expect(((await replay.json()) as Receipt).id).toBe(receipt.id);

  await page.goto(bookmark);
  await page.reload();
  const persisted = page.getByRole("region", {
    name: "Synthetic inbound email",
    exact: true,
  });
  await expect(
    persisted.getByRole("heading", { name: "Simulated model", exact: true }),
  ).toBeVisible();
  await expect(
    persisted.getByRole("link", {
      name: "Inspect captured conversation and source",
      exact: true,
    }),
  ).toHaveAttribute(
    "href",
    `/communications/${source.id}?workspace=${workspace}`,
  );
  await expect(
    persisted.getByText(
      "Sender identity remains unverified. No sensitive action authority is granted by this message.",
      { exact: true },
    ),
  ).toBeVisible();
  await persisted
    .getByRole("link", {
      name: "Inspect captured conversation and source",
      exact: true,
    })
    .click();
  await page.getByRole("button", { name: "Open message", exact: true }).click();
  await expect(
    page.getByRole("link", {
      name: "Download authenticated source",
      exact: true,
    }),
  ).toHaveCount(1);
  await page.goto(bookmark);
  await page
    .getByRole("link", { name: `Open task ${taskId}`, exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: actualTask.title, exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByText(`Task reference: ${taskId}`, { exact: true }),
  ).toBeVisible();
  await page.goto(bookmark);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const financialAfter = (await (
    await page.request.get(`${base}/receivables/${invoice.id}`)
  ).json()) as { invoice: Invoice };
  expect({
    status: financialAfter.invoice.financial_status,
    outstanding: financialAfter.invoice.outstanding,
  }).toEqual(financialBefore);
  await readWorkspace();
  const outbound = (await (
    await page.request.get(`${base}/email-execution`)
  ).json()) as { system_enabled: boolean; activation: unknown };
  expect(outbound.system_enabled).toBe(false);
  expect(outbound.activation).toBeNull();
});
