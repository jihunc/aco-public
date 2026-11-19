/** Actual application/SDK path; ONLY external Vapi HTTP and Daily/microphone are doubles. */
import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import type {} from "../browser_voice_daily";
import type {
  ApprovalView,
  PageInvoiceView,
  VoicePage,
  VoiceSessionView,
  WorkspaceView,
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
      ["Last name", "LIVE-code fixture"],
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
const fixture = (command: "install" | "terminal", workspace: string): unknown =>
  JSON.parse(
    execFileSync(
      ".venv/bin/python",
      ["tests/voice_live_browser_state.py", command, workspace],
      {
        encoding: "utf8",
        env: process.env,
        timeout: 30000,
      },
    ),
  );

test("explicit mocked LIVE-code browser joins one approved native call and retains stop then authenticated completion", async ({
  page,
  browser,
}) => {
  test.setTimeout(180000);
  page.setDefaultTimeout(15000);
  expect(process.env.ACO_TEST_PROFILE).toBe("voice-live-code");
  const password = process.env.ACO_TEST_DEMO_PASSWORD;
  if (!password) throw Error("Explicit synthetic browser password required");
  const outside: string[] = [];
  page.on("request", (request) => {
    const host = new URL(request.url()).hostname;
    if (["api.vapi.ai", "vapi.daily.co"].includes(host)) outside.push(host);
  });
  await signIn(page, password, `voice-live-${randomUUID()}@example.test`);
  await page
    .getByLabel("Workspace name", { exact: true })
    .fill(`Voice live-code ${randomUUID()}`);
  await page.getByLabel("Timezone", { exact: true }).fill("UTC");
  const creating = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/api/workspaces" &&
      r.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "Create demo workspace", exact: true })
    .click();
  const created = await creating;
  expect(created.status()).toBe(201);
  const workspace = (await created.json()) as WorkspaceView;
  const base = `/api/workspaces/${workspace.id}`;
  const seed = page.getByRole("region", {
    name: "Synthetic accounting setup",
    exact: true,
  });
  const importing = page.waitForResponse(
    (response) => new URL(response.url()).pathname === `${base}/demo/accounting/seed` && response.request().method() === "POST",
    { timeout: 30000 },
  );
  await seed
    .getByRole("button", { name: "Import synthetic demo data", exact: true })
    .click();
  expect((await importing).status()).toBe(201);
  await expect(seed).toContainText(
    "24 imported invoices · 6 imported customers", { timeout: 15000 },
  );
  await page
    .getByLabel("Teammate email", { exact: true })
    .fill("owner@example.test");
  await page
    .getByRole("combobox", { name: "Role", exact: true })
    .selectOption("finance_manager");
  await page
    .getByRole("button", { name: "Create invitation", exact: true })
    .click();
  const invitation = page.getByLabel("Invitation code", { exact: true });
  await expect(invitation).not.toHaveValue("");
  const code = await invitation.inputValue();
  const reviewerContext = await browser.newContext();
  try {
    const reviewer = await reviewerContext.newPage();
    reviewer.on("request", (request) => {
      const host = new URL(request.url()).hostname;
      if (["api.vapi.ai", "vapi.daily.co"].includes(host)) outside.push(host);
    });
    reviewer.setDefaultTimeout(15000);
    await signIn(reviewer, password);
    await reviewer.getByLabel("Join code", { exact: true }).fill(code);
    const accepting = reviewer.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/invitations/accept" &&
        response.request().method() === "POST",
    );
    await reviewer
      .getByRole("button", { name: "Accept invitation", exact: true })
      .click();
    expect((await accepting).status()).toBe(200);
    const initial = (await (
      await page.request.get(`${base}/receivables?search=INV-101&page_size=50`)
    ).json()) as PageInvoiceView;
    const invoice = initial.items.find((item) => item.number === "INV-101");
    if (!invoice) throw Error("Actual seeded synthetic invoice required");
    const before = {
      outstanding: invoice.outstanding,
      financial_status: invoice.financial_status,
      revision: invoice.source.revision,
    };
    fixture("install", workspace.id);
    await page.goto(`/voice?workspace=${workspace.id}`);
    await expect(
      page.getByRole("heading", {
        name: "LIVE browser connection",
        exact: true,
      }),
    ).toBeVisible();
    const checking = page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === `${base}/voice-sessions/setup` &&
        r.request().method() === "POST",
    );
    await page
      .getByRole("button", {
        name: "Verify saved browser connection",
        exact: true,
      })
      .click();
    const checked = await checking;
    expect(checked.status()).toBe(200);
    const ready = (await checked.json()) as VoicePage;
    expect(ready.evidence_mode).toBe("LIVE");
    expect(ready.connections).toHaveLength(1);
    const connection = ready.connections[0];
    if (!connection) throw Error("Actual observed READY connection required");
    await page
      .getByRole("combobox", { name: "Voice connection", exact: true })
      .selectOption(connection.id);
    await expect(
      page.getByRole("combobox", {
        name: "Optional saved synthetic consent",
        exact: true,
      }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("combobox", {
        name: "Fixed synthetic scenario",
        exact: true,
      }),
    ).toHaveCount(0);
    await page.getByLabel("Find invoices", { exact: true }).fill("INV-101");
    await page.getByRole("checkbox", { name: "INV-101", exact: true }).check();
    await page
      .getByLabel("Session objective", { exact: true })
      .fill(
        "Explicit mocked LIVE-code browser fixture; no real microphone, provider or capture",
      );
    const saving = page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === `${base}/voice-sessions` &&
        r.request().method() === "POST",
    );
    await page
      .getByRole("button", { name: "Save session for approval", exact: true })
      .click();
    const response = await saving;
    expect(response.status()).toBe(201);
    const saved = (await response.json()) as VoiceSessionView;
    expect(saved.evidence_mode).toBe("LIVE");
    expect(saved.provider_call_id).toBeNull();
    const read = async () =>
      (await (
        await page.request.get(`${base}/voice-sessions/${saved.id}`)
      ).json()) as VoiceSessionView;
    const own = (await (
      await page.request.get(`${base}/approvals/${saved.approval_id}`)
    ).json()) as ApprovalView;
    expect(own.can_approve).toBe(false);
    await reviewer.goto(
      `/review?workspace=${workspace.id}&approval=${saved.approval_id}`,
    );
    const approval = reviewer.getByRole("article", {
      name: `Approval ${saved.approval_id}`,
      exact: true,
    });
    await expect(approval).toContainText("LIVE_HUMAN_MICROPHONE_CAPTURE_OFF");
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
        "Independent review of the actual saved synthetic LIVE-code fixture. All external HTTP/Daily boundaries doubled, no real microphone consent/provider readiness claimed.",
      );
    await captured
      .getByRole("button", { name: "Approve proposal", exact: true })
      .click();
    await expect.poll(async () => (await read()).can_start).toBe(true);
    expect((await read()).provider_call_id).toBeNull();
    await page
      .getByRole("checkbox", {
        name: "I reviewed this saved objective, invoice scope and independent approval.",
        exact: true,
      })
      .check();
    const starting = page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname ===
          `${base}/voice-sessions/${saved.id}/start` &&
        r.request().method() === "POST",
    );
    await page
      .getByRole("button", {
        name: "Start approved browser session",
        exact: true,
      })
      .click();
    const started = await starting;
    expect(started.status()).toBe(200);
    const native = (await started.json()) as VoiceSessionView;
    expect(native.provider_call_id).toBeTruthy();
    await expect(
      page.getByRole("region", { name: "Saved voice session", exact: true }),
    ).toContainText("Browser voice session");
    await reviewer.goto(`/voice/${saved.id}?workspace=${workspace.id}`);
    const controls = reviewer.getByRole("region", {
      name: "Browser call controls",
      exact: true,
    });
    await expect(
      controls.getByRole("button", {
        name: "Join existing browser call",
        exact: true,
      }),
    ).toBeDisabled();
    await controls
      .getByRole("checkbox", {
        name: "I am the saved participant and agree to join this reviewed browser call with my microphone, capture off.",
        exact: true,
      })
      .check();
    const joining = reviewer.waitForResponse(
      (r) =>
        new URL(r.url()).pathname ===
          `${base}/voice-sessions/${saved.id}/join` &&
        r.request().method() === "POST",
    );
    await controls
      .getByRole("button", { name: "Join existing browser call", exact: true })
      .click();
    const joined = await joining;
    expect(joined.status()).toBe(200);
    expect(joined.headers()["cache-control"]).toContain("no-store");
    expect(joined.request().headers()["x-csrf-token"]).toBeTruthy();
    expect(joined.request().postDataJSON()).toEqual({
      expected_version: native.version,
    });
    await expect(controls).toContainText("Browser audio connected.", { timeout: 30000 });
    expect(await reviewer.evaluate(() => window.__acoVoiceDaily)).toEqual({
      objects: 1,
      joins: 1,
      destroys: 0,
      recordings: 0,
    });
    expect(await reviewer.locator("body").innerText()).not.toContain(
      "vapi.daily.co",
    );
    const ending = reviewer.waitForResponse(
      (r) =>
        new URL(r.url()).pathname ===
          `${base}/voice-sessions/${saved.id}/end` &&
        r.request().method() === "POST",
    );
    await controls
      .getByRole("checkbox", {
        name: "I reviewed this saved call and request its one bounded safety stop.",
        exact: true,
      })
      .check();
    await controls
      .getByRole("button", { name: "Request end call", exact: true })
      .click();
    const ended = await ending;
    expect(ended.status()).toBe(200);
    await expect.poll(async () => (await read()).stop_outcome).toBe("ACCEPTED");
    expect((await read()).state).toBe("active");
    await expect(
      controls.getByRole("button", {
        name: "Join existing browser call",
        exact: true,
      }),
    ).toHaveCount(0);
    await expect(
      reviewer.getByRole("region", {
        name: "Saved voice session",
        exact: true,
      }),
    ).toContainText("Stop accepted — waiting for authenticated completion.");
    await expect
      .poll(async () =>
        reviewer.evaluate(() => window.__acoVoiceDaily.destroys),
      )
      .toBe(1);
    const observed = JSON.parse(
      readFileSync(".tooling/voice-live-browser-http-observed.json", "utf8"),
    ) as { method: string; path: string }[];
    expect(
      observed.filter(
        (request) => request.method === "POST" && request.path === "/call/web",
      ),
    ).toHaveLength(1);
    expect(
      observed.filter(
        (request) =>
          request.method === "POST" && request.path.endsWith("/control"),
      ),
    ).toHaveLength(1);
    fixture("terminal", workspace.id);
    await expect.poll(async () => (await read()).state).toBe("completed");
    await reviewer.reload();
    await expect(
      reviewer.getByRole("region", {
        name: "Saved voice session",
        exact: true,
      }),
    ).toContainText("Authenticated completion received.");
    const final = await read();
    expect(
      final.events?.filter((event) => event.kind === "call.completed"),
    ).toHaveLength(1);
    expect(final.provider_call_id).toBe(native.provider_call_id);
    expect(final.stop_outcome).toBe("ACCEPTED");
    expect(final.outcomes).toEqual([]);
    expect(JSON.stringify(final)).not.toMatch(
      /web_call_url|controlUrl|synthetic-owned-browser-room/,
    );
    const after = (await (
      await page.request.get(`${base}/receivables?search=INV-101&page_size=50`)
    ).json()) as PageInvoiceView;
    const financial = after.items.find((item) => item.id === invoice.id);
    expect(financial).toBeTruthy();
    if (!financial) throw Error("Retained source invoice required");
    expect({
      outstanding: financial.outstanding,
      financial_status: financial.financial_status,
      revision: financial.source.revision,
    }).toEqual(before);
    expect(
      (await (await page.request.get(`${base}/communications`)).json()).items,
    ).toEqual([]);
    expect(outside).toEqual([]);
    await reviewer.setViewportSize({ width: 390, height: 844 });
    await expect
      .poll(() =>
        reviewer.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      )
      .toBe(true);
  } finally {
    await reviewerContext.close();
  }
});
