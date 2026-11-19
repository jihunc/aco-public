import { expect, test, type Page } from "@playwright/test";
import { readFileSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
type ActionState = {
  id: string;
  version: number;
  status: string;
  policy_version_id: string;
  occurrence_id: string;
  invoice_id: string;
  operation_key: string;
  command: object;
  due_at: string;
};
function snapshot(tenant: string): {
  actions: ActionState[];
  automation_enabled: boolean;
  attempts: number;
  contacts: number;
  tickets: number;
} {
  return JSON.parse(
    execFileSync(
      ".venv/bin/python",
      ["tests/policy_browser_state.py", tenant],
      { encoding: "utf8" },
    ),
  );
}
const env = Object.fromEntries(
  (existsSync(".env") ? readFileSync(".env", "utf8") : "")
    .split("\n")
    .filter((line) => line.includes("="))
    .map((line) => [
      line.slice(0, line.indexOf("=")),
      line.slice(line.indexOf("=") + 1),
    ]),
);
async function setup(page: Page) {
  await page.goto("/login");
  await page.getByRole("link", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Username or email").fill("owner@example.test");
  await page
    .getByLabel("Password", { exact: true })
    .fill(process.env.ACO_TEST_DEMO_PASSWORD || env.DEMO_PASSWORD);
  await page.getByRole("button", { name: "Sign In", exact: true }).click();
  await expect(page).toHaveURL(/\/onboarding$/);
  const me = await (await page.request.get("/api/me")).json();
  const headers = {
    origin: "http://127.0.0.1:53000",
    "x-csrf-token": me.csrf_token,
  };
  const created = await page.request.post("/api/workspaces", {
    headers,
    data: {
      name: `T08 browser ${Date.now()}`,
      timezone: "Australia/Melbourne",
    },
  });
  expect(created.status()).toBe(201);
  const tenant = (await created.json()).id;
  const seeded = await page.request.post(
    `/api/workspaces/${tenant}/demo/accounting/seed`,
    { headers, data: {} },
  );
  expect(seeded.status()).toBe(201);
  await page.goto(`/settings/policies?workspace=${tenant}`);
  return { tenant, headers };
}

test("policy draft edit preview and archive retain immutable versions", async ({
  page,
}) => {
  const { tenant } = await setup(page);
  await expect(
    page.getByRole("heading", { name: "Collection policies", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "New policy", exact: true }).click();
  await page
    .getByLabel("Policy name", { exact: true })
    .fill("Browser reviewed policy");
  await page.getByLabel("Days from due date 1").fill("120");
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Saved version 1");
  let policies = await (
    await page.request.get(`/api/workspaces/${tenant}/policies`)
  ).json();
  let policy = policies.find(
    (row: { name: string }) => row.name === "Browser reviewed policy",
  );
  expect(policy.definition.rules[0].offset_days).toBe(120);
  await page.getByLabel("Days from due date 1").fill("121");
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Saved version 2");
  await page
    .getByRole("button", { name: "Preview saved policy", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Schedule preview" }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Native accounting reminders have not been attested as disabled.",
    ),
  ).toBeVisible();
  await expect(page.getByText("recipient_unverified").first()).toBeVisible();
  await expect(
    page.getByText("OUTBOUND_EXECUTOR_UNAVAILABLE").first(),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Archive policy", exact: true })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Archive policy" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Confirm archive", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText("Policy archived");
  const history = await (
    await page.request.get(
      `/api/workspaces/${tenant}/policies/${policy.policy_id}/versions`,
    )
  ).json();
  expect(
    history.map(
      (row: { definition: { rules: { offset_days: number }[] } }) =>
        row.definition.rules[0].offset_days,
    ),
  ).toEqual([120, 121]);
  policies = await (
    await page.request.get(`/api/workspaces/${tenant}/policies`)
  ).json();
  policy = policies.find(
    (row: { name: string }) => row.name === "Browser reviewed policy",
  );
  expect(policy.archived).toBe(true);
  console.log(
    "T08 policy browser persisted history",
    JSON.stringify({
      tenant,
      policy: policy.policy_id,
      versions: history.map((row: { id: string }) => row.id),
    }),
  );
});

test("mobile keyboard validation loading and retry preserve editable draft", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { tenant } = await setup(page);
  await page.getByRole("button", { name: "New policy", exact: true }).focus();
  await page.keyboard.press("Enter");
  await page.getByLabel("Policy name", { exact: true }).fill("Mobile policy");
  await page.getByLabel("Days from due date 1").fill("120");
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Saved version 1");
  await page
    .getByRole("button", { name: "Activate reviewed policy", exact: true })
    .click();
  await expect(
    page.getByRole("alert", { name: "Policy error", exact: true }),
  ).toContainText("NATIVE_REMINDER_ATTESTATION_REQUIRED");
  await page.route(`**/api/workspaces/${tenant}/policies/*/preview`, (route) =>
    route.fulfill({ status: 503, body: "{}" }),
  );
  await page
    .getByRole("button", { name: "Preview saved policy", exact: true })
    .click();
  await expect(
    page.getByRole("alert", { name: "Policy error", exact: true }),
  ).toContainText("could not load");
  await page.unroute(`**/api/workspaces/${tenant}/policies/*/preview`);
  let release!: () => void;
  const pendingResponse = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(
    `**/api/workspaces/${tenant}/policies/*/preview`,
    async (route) => {
      await pendingResponse;
      await route.continue();
    },
  );
  await page
    .getByRole("button", { name: "Preview saved policy", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Preview saved policy", exact: true }),
  ).toBeDisabled();
  release();
  await expect(
    page.getByRole("heading", { name: "Schedule preview" }),
  ).toBeVisible();
  await page.unroute(`**/api/workspaces/${tenant}/policies/*/preview`);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("reviewed activation edits conflict and active archive retain real scheduled history", async ({
  page,
}) => {
  test.setTimeout(90000);
  const { tenant, headers } = await setup(page);
  const recipients = await (
    await page.request.get(`/api/workspaces/${tenant}/policy-recipients`)
  ).json();
  const destination = recipients[0].destination;
  const form = page.locator("form").filter({
    has: page.getByLabel(`Timezone for ${destination}`, { exact: true }),
  });
  await form
    .getByLabel(`Timezone for ${destination}`, { exact: true })
    .fill("Australia/Melbourne");
  await form
    .getByLabel("Destination and email permission verified", { exact: true })
    .check();
  await form.getByLabel("Allow email channel", { exact: true }).check();
  await form
    .getByLabel(`Review evidence for ${destination}`, { exact: true })
    .fill("Synthetic browser Owner review");
  await form
    .getByRole("button", { name: "Save recipient review", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText(
    "Recipient review saved",
  );
  await page.getByRole("button", { name: "New policy", exact: true }).click();
  await page
    .getByLabel("Policy name", { exact: true })
    .fill("Browser active policy");
  await page.getByLabel("Days from due date 1").fill("120");
  await page
    .getByLabel("I verified native accounting reminders are disabled", {
      exact: true,
    })
    .check();
  await page
    .getByLabel("I reviewed this policy configuration", { exact: true })
    .check();
  await page
    .getByRole("button", { name: "Activate reviewed policy", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText("Saved version 1");
  const before = snapshot(tenant);
  expect(before.actions.length).toBeGreaterThan(0);
  expect(before.actions.every((a) => a.status === "queued")).toBe(true);
  await page
    .getByRole("button", { name: "Recompute future checks", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText(
    "Future checks recomputed",
  );
  const recomputed = snapshot(tenant);
  expect(recomputed.actions.filter((a) => a.status === "queued").length).toBe(
    before.actions.length,
  );
  expect(
    recomputed.actions
      .filter((a) => before.actions.some((b) => b.id === a.id))
      .every((a) => a.status === "cancelled"),
  ).toBe(true);
  await page.getByLabel("Days from due date 1").fill("121");
  await page
    .getByRole("button", { name: "Activate reviewed policy", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText("Saved version 2");
  const edited = snapshot(tenant);
  for (const old of before.actions) {
    const preserved = edited.actions.find((a) => a.id === old.id);
    expect(preserved).toMatchObject({
      ...old,
      status: "cancelled",
      version: old.version + 1,
    });
  }
  const rows = await (
    await page.request.get(`/api/workspaces/${tenant}/policies`)
  ).json();
  const saved = rows.find(
    (p: { name: string }) => p.name === "Browser active policy",
  );
  const conflict = await page.request.put(
    `/api/workspaces/${tenant}/policies/${saved.policy_id}`,
    {
      headers,
      data: {
        name: saved.name,
        definition: saved.definition,
        expected_version: saved.version,
        activate: true,
        native_reminders_disabled: true,
        configuration_reviewed: true,
      },
    },
  );
  expect(conflict.status()).toBe(200);
  await page
    .getByRole("button", { name: "Activate reviewed policy", exact: true })
    .click();
  await expect(
    page.getByRole("alert", { name: "Policy error", exact: true }),
  ).toHaveText("VERSION_CONFLICT");
  await page.reload();
  await page
    .getByLabel("Saved policy", { exact: true })
    .selectOption(saved.policy_id);
  await page
    .getByRole("button", { name: "View version history", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Version history", exact: true }),
  ).toBeVisible();
  await expect(
    page.locator("summary").filter({ hasText: "Browser active policy" }),
  ).toHaveCount(3);
  await page
    .getByRole("button", { name: "Archive policy", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Archive policy",
    exact: true,
  });
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Tab");
  expect(
    await dialog.evaluate((element) =>
      element.contains(document.activeElement),
    ),
  ).toBe(true);
  await dialog
    .getByRole("button", { name: "Confirm archive", exact: true })
    .click();
  await expect(
    page.getByRole("alert", { name: "Policy error", exact: true }),
  ).toHaveText("REPLACEMENT_OR_SUSPEND_REQUIRED");
  await dialog
    .getByLabel("Suspend automation and cancel unsent actions", { exact: true })
    .check();
  await dialog
    .getByRole("button", { name: "Confirm archive", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText("Policy archived");
  const archived = snapshot(tenant);
  expect(archived.automation_enabled).toBe(false);
  expect(archived.actions.every((a) => a.status === "cancelled")).toBe(true);
  expect(archived.attempts).toBe(0);
  expect(archived.contacts).toBe(0);
  expect(archived.tickets).toBe(0);
  console.log(
    "T08 browser action history",
    JSON.stringify({ before, recomputed, edited, archived }),
  );
});

test("archive binds the displayed replacement version through refresh and requires fresh review", async ({
  page,
}) => {
  test.setTimeout(90000);
  const { tenant, headers } = await setup(page);
  const recipients = await (
    await page.request.get(`/api/workspaces/${tenant}/policy-recipients`)
  ).json();
  const recipient = await page.request.put(
    `/api/workspaces/${tenant}/policy-recipients/${recipients[0].contact_id}`,
    {
      headers,
      data: {
        expected_version: 0,
        timezone: "Australia/Melbourne",
        timezone_verified: true,
        destination_verified: true,
        allowed_channels: ["email"],
        expected_contact_revision: recipients[0].source_revision,
        expected_destination: recipients[0].destination,
        evidence: "Synthetic replacement browser review",
      },
    },
  );
  expect(recipient.status()).toBe(200);
  const definition = {
    rules: [
      {
        id: "74454ba6-5da8-4b30-ae89-33bd8c17e21e",
        offset_days: 120,
        local_time: "09:00",
        channel: "email",
        template_id: "routine",
      },
    ],
  };
  const created = await page.request.post(
    `/api/workspaces/${tenant}/policies`,
    {
      headers,
      data: {
        name: "Archive target",
        expected_version: 0,
        definition,
        activate: true,
        native_reminders_disabled: true,
        configuration_reviewed: true,
      },
    },
  );
  expect(created.status()).toBe(201);
  const target = await created.json();
  const draft = await page.request.post(`/api/workspaces/${tenant}/policies`, {
    headers,
    data: { name: "Replacement draft", expected_version: 0, definition },
  });
  expect(draft.status()).toBe(201);
  const replacement = await draft.json();
  await page.reload();
  await page
    .getByLabel("Saved policy", { exact: true })
    .selectOption(target.policy_id);
  await page
    .getByRole("button", { name: "Archive policy", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Archive policy",
    exact: true,
  });
  await dialog
    .getByLabel("Replacement policy", { exact: true })
    .selectOption(replacement.policy_id);
  await expect(
    dialog.getByText("Review replacement version 1", { exact: true }),
  ).toBeVisible();
  const reviewed = dialog.getByLabel("I reviewed this replacement version", {
    exact: true,
  });
  const native = dialog.getByLabel(
    "Native accounting reminders are disabled for this replacement",
    { exact: true },
  );
  await expect(reviewed).not.toBeChecked();
  await expect(native).not.toBeChecked();
  await dialog
    .getByRole("button", { name: "Confirm archive", exact: true })
    .click();
  await expect(
    dialog.getByRole("alert", { name: "Policy error", exact: true }),
  ).toHaveText("REPLACEMENT_REVIEW_REQUIRED");
  await reviewed.check();
  await native.check();
  const edited = await page.request.put(
    `/api/workspaces/${tenant}/policies/${replacement.policy_id}`,
    {
      headers,
      data: {
        name: replacement.name,
        expected_version: replacement.version,
        definition: {
          ...replacement.definition,
          rules: [{ ...replacement.definition.rules[0], offset_days: 121 }],
        },
      },
    },
  );
  expect(edited.status()).toBe(200);
  const latest = await edited.json();
  const before = snapshot(tenant);
  // Exercise React Query's actual background focus refresh while the reviewed snapshot stays v1.
  const refreshed = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/api/workspaces/${tenant}/policies`) &&
      response.request().method() === "GET",
  );
  await page.evaluate(() =>
    window.dispatchEvent(new Event("visibilitychange")),
  );
  await refreshed;
  await expect(
    dialog
      .getByLabel("Replacement policy", { exact: true })
      .locator("option")
      .filter({ hasText: "Replacement draft · v2" }),
  ).toHaveCount(1);
  await expect(
    dialog.getByText("Review replacement version 1", { exact: true }),
  ).toBeVisible();
  const submitted = page.waitForRequest(
    (request) =>
      request.url().endsWith(`/policies/${target.policy_id}/archive`) &&
      request.method() === "POST",
  );
  await dialog
    .getByRole("button", { name: "Confirm archive", exact: true })
    .click();
  expect((await submitted).postDataJSON().replacement_expected_version).toBe(1);
  await expect(
    dialog.getByRole("alert", { name: "Policy error", exact: true }),
  ).toHaveText("REPLACEMENT_VERSION_CONFLICT");
  expect(snapshot(tenant)).toEqual(before);
  await dialog
    .getByRole("button", { name: "Refresh replacement choices", exact: true })
    .click();
  await expect(
    dialog.getByLabel("Replacement policy", { exact: true }),
  ).toHaveValue("");
  await dialog
    .getByLabel("Replacement policy", { exact: true })
    .selectOption(replacement.policy_id);
  await expect(
    dialog.getByText("Review replacement version 2", { exact: true }),
  ).toBeVisible();
  await expect(reviewed).not.toBeChecked();
  await expect(native).not.toBeChecked();
  await reviewed.check();
  await native.check();
  const archivedResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `/api/workspaces/${tenant}/policies/${target.policy_id}/archive` &&
      response.request().method() === "POST",
  );
  await dialog
    .getByRole("button", { name: "Confirm archive", exact: true })
    .click();
  const archived = await archivedResponse;
  expect(archived.status()).toBe(200);
  expect(archived.request().postDataJSON()).toMatchObject({
    replacement_expected_version: latest.version,
    native_reminders_disabled: true,
    configuration_reviewed: true,
  });
  expect(await archived.json()).toMatchObject({
    policy_id: target.policy_id,
    archived: true,
    replacement_policy_id: replacement.policy_id,
    automation_enabled: true,
  });
  console.log("T08 archive response", JSON.stringify({
    path: new URL(archived.url()).pathname,
    status: archived.status(),
    duration_ms: archived.request().timing().responseEnd,
  }));
  await expect(page.getByRole("status")).toContainText("Policy archived");
  const persisted = await (
    await page.request.get(`/api/workspaces/${tenant}/policies`)
  ).json();
  expect(persisted.find((row: { policy_id: string }) => row.policy_id === target.policy_id))
    .toMatchObject({ archived: true, active: false });
  expect(persisted.find((row: { policy_id: string }) => row.policy_id === replacement.policy_id))
    .toMatchObject({ active: true, version: latest.version });
  const after = snapshot(tenant);
  expect(
    after.actions.filter((a) => a.status === "queued").length,
  ).toBeGreaterThan(0);
  expect(
    after.actions
      .filter((a) => a.status === "queued")
      .every((a) => a.policy_version_id === latest.id),
  ).toBe(true);
  expect(
    after.actions
      .filter((a) => before.actions.some((old) => old.id === a.id))
      .every((a) => a.status === "cancelled"),
  ).toBe(true);
  expect(after.attempts).toBe(0);
  expect(after.contacts).toBe(0);
  expect(after.tickets).toBe(0);
  const versions = await (
    await page.request.get(
      `/api/workspaces/${tenant}/policies/${replacement.policy_id}/versions`,
    )
  ).json();
  expect(versions.map((v: { version: number }) => v.version)).toEqual([1, 2]);
  console.log(
    "T08 browser replacement binding",
    JSON.stringify({
      tenant,
      target: target.policy_id,
      reviewed_version: 1,
      activated_version: 2,
      before,
      after,
    }),
  );
});

test("recipient reviews retain displayed source and timezone until explicit fresh review", async ({
  page,
}) => {
  test.setTimeout(120000);
  const { tenant, headers } = await setup(page);
  const base = `/api/workspaces/${tenant}`;
  const initial = (
    await (await page.request.get(`${base}/policy-recipients`)).json()
  )[0];
  const key = initial.contact_id;
  const form = page.locator(`form[data-recipient-id="${key}"]`);
  const save = form.getByRole("button", {
    name: "Save recipient review",
    exact: true,
  });
  const refresh = form.getByRole("button", {
    name: "Refresh recipient for review",
    exact: true,
  });
  const verified = form.getByLabel(
    "Destination and email permission verified",
    { exact: true },
  );
  const fallback = form.getByLabel(
    "I confirm the workspace timezone applies to this recipient",
    { exact: true },
  );
  const email = form.getByLabel("Allow email channel", { exact: true });
  async function submit() {
    const response = page.waitForResponse(
      (r) =>
        r.url().endsWith(`/policy-recipients/${key}`) &&
        r.request().method() === "PUT",
    );
    await save.click();
    return response;
  }
  async function backgroundRefresh() {
    const fetched = page.waitForResponse(
      (r) =>
        r.url().endsWith("/policy-recipients") &&
        r.request().method() === "GET",
    );
    await page.evaluate(() =>
      window.dispatchEvent(new Event("visibilitychange")),
    );
    await fetched;
  }
  async function importDestination(destination: string) {
    const source = JSON.parse(
      execFileSync(
        ".venv/bin/python",
        ["tests/policy_browser_source.py", tenant, key, destination],
        { encoding: "utf8" },
      ),
    );
    const sync = await page.request.post(`${base}/accounting/sync`, {
      headers,
      data: { provider_connection_id: source.provider_connection_id },
    });
    expect(sync.status()).toBe(200);
    return (
      await (await page.request.get(`${base}/policy-recipients`)).json()
    ).find((row: { contact_id: string }) => row.contact_id === key);
  }
  await expect(form).toContainText(
    `Reviewing destination: ${initial.destination}`,
  );
  await verified.check();
  await fallback.check();
  await email.check();
  await form
    .getByLabel(`Review evidence for ${initial.destination}`, { exact: true })
    .fill("Reviewed displayed source A and Melbourne");
  expect((await submit()).status()).toBe(200);
  await expect(page.getByRole("status")).toContainText(
    "Recipient review saved",
  );
  // This form adopts only its own returned version, allowing another normal edit.
  await form
    .getByLabel(`Review evidence for ${initial.destination}`, { exact: true })
    .fill("Second own successful review of A");
  const second = await submit();
  expect(second.request().postDataJSON().expected_version).toBe(1);
  expect(second.status()).toBe(200);
  await expect(page.getByRole("status")).toContainText(
    "Recipient review saved",
  );

  const imported = await importDestination("changed-browser@example.test");
  expect(imported.source_revision).not.toBe(initial.source_revision);
  expect(imported.review_matches_source).toBe(false);
  await backgroundRefresh();
  await expect(form).toContainText(
    "Current imported destination: changed-browser@example.test",
  );
  await expect(form).toContainText(
    `Reviewing destination: ${initial.destination}`,
  );
  await expect(form).toContainText("Saved review: historical");
  const before = snapshot(tenant);
  const refused = await submit();
  expect(refused.request().postDataJSON()).toMatchObject({
    expected_version: 2,
    expected_contact_revision: initial.source_revision,
    expected_destination: initial.destination,
  });
  expect(refused.status()).toBe(409);
  await expect(page.getByRole("alert", { name: "Policy error", exact: true })).toContainText(
    "RECIPIENT_SOURCE_CHANGED",
  );
  expect(snapshot(tenant)).toEqual(before);

  // Initial loading of a source-stale saved review shows old flags/evidence as
  // historical, while fresh B attestations and evidence start empty.
  await page.reload();
  await expect(form).toContainText(
    "Saved evidence: Second own successful review of A",
  );
  await expect(form).toContainText(
    `Saved reviewed destination: ${initial.destination}`,
  );
  await expect(verified).not.toBeChecked();
  await expect(fallback).not.toBeChecked();
  await expect(
    form.getByLabel("Review evidence for changed-browser@example.test", {
      exact: true,
    }),
  ).toHaveValue("");
  await refresh.click();
  await expect(page.getByRole("status")).toContainText("Recipient refreshed");
  await expect(email).not.toBeChecked();
  await verified.check();
  await fallback.check();
  await email.check();
  await form
    .getByLabel("Review evidence for changed-browser@example.test", {
      exact: true,
    })
    .fill("Fresh review of source B and Melbourne");
  const fresh = await submit();
  expect(fresh.request().postDataJSON()).toMatchObject({
    expected_version: 2,
    expected_contact_revision: imported.source_revision,
    expected_destination: imported.destination,
    expected_tenant_timezone: "Australia/Melbourne",
  });
  expect(fresh.status()).toBe(200);
  await expect(page.getByRole("status")).toContainText(
    "Recipient review saved",
  );
  const workspace = await (await page.request.get(base)).json();
  const changed = await page.request.patch(base, {
    headers,
    data: { name: workspace.name, timezone: "Pacific/Auckland" },
  });
  expect(changed.status()).toBe(200);
  await backgroundRefresh();
  await expect(form).toContainText(
    "Current workspace timezone: Pacific/Auckland",
  );
  await expect(form).toContainText(
    "Workspace timezone for this review: Australia/Melbourne",
  );
  const beforeTimezone = snapshot(tenant);
  const timezoneRefusal = await submit();
  expect(
    timezoneRefusal.request().postDataJSON().expected_tenant_timezone,
  ).toBe("Australia/Melbourne");
  expect(timezoneRefusal.status()).toBe(409);
  await expect(page.getByRole("alert", { name: "Policy error", exact: true })).toContainText(
    "RECIPIENT_TIMEZONE_CHANGED",
  );
  expect(snapshot(tenant)).toEqual(beforeTimezone);
  await refresh.click();
  await expect(page.getByRole("status")).toContainText("Recipient refreshed");
  await expect(form).toContainText(
    "Workspace timezone for this review: Pacific/Auckland",
  );
  await expect(fallback).not.toBeChecked();
  await expect(verified).not.toBeChecked();
  await expect(
    form.getByLabel("Review evidence for changed-browser@example.test", {
      exact: true,
    }),
  ).toHaveValue("");
  await fallback.check();
  await verified.check();
  await email.check();
  await form
    .getByLabel("Review evidence for changed-browser@example.test", {
      exact: true,
    })
    .fill("Fresh B review and explicit Auckland confirmation");
  const final = await submit();
  expect(final.status()).toBe(200);
  const result = await final.json();
  expect(result.version).toBe(4);
  expect(result.confirmed_tenant_timezone).toBe("Pacific/Auckland");
  expect(result.review_matches_source).toBe(true);
  await page.reload();
  await expect(form).toContainText("Saved review: current");
  await expect(fallback).toBeChecked();
  const finalState = snapshot(tenant);
  expect(finalState.attempts).toBe(0);
  expect(finalState.contacts).toBe(0);
  expect(finalState.tickets).toBe(0);
  console.log(
    "T08 captured recipient review",
    JSON.stringify({ initial, imported, result, finalState }),
  );
});
