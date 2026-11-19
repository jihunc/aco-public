import { expect, test, type Page } from "@playwright/test";
import { randomUUID } from "node:crypto";
import type {
  ApprovalView,
  VoiceConsentView,
  PageInvoiceView,
  ArtifactPrivacyView,
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
      ["Last name", "Voice initiator"],
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

for (const { privacy, businessFacts, agentRequested } of [
  { privacy: false, businessFacts: false, agentRequested: false },
  { privacy: true, businessFacts: false, agentRequested: false },
  { privacy: false, businessFacts: true, agentRequested: false },
  { privacy: false, businessFacts: false, agentRequested: true },
])
  test(`fresh Owner ${agentRequested ? "agent requested review" : businessFacts ? "signed business facts" : privacy ? "private consent workflow" : "capture off"} prepares an offline voice session and retains guarded outcomes`, async ({
    page,
    browser,
  }) => {
    test.setTimeout(180_000);
    page.setDefaultTimeout(15000);
    expect(process.env.ACO_TEST_PROFILE).toBe("synthetic-voice");
    const password = process.env.ACO_TEST_DEMO_PASSWORD;
    if (!password) throw Error("Explicit synthetic browser password required");
    await signIn(page, password, `voice-${randomUUID()}@example.test`);
    await page
      .getByLabel("Workspace name", { exact: true })
      .fill(`Offline voice ${randomUUID()}`);
    await page.getByLabel("Timezone", { exact: true }).fill("UTC");
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
    const base = `/api/workspaces/${workspace.id}`;
    const seed = page.getByRole("region", {
      name: "Synthetic accounting setup",
      exact: true,
    });
    const importing = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === `${base}/demo/accounting/seed` &&
        response.request().method() === "POST",
    );
    await seed
      .getByRole("button", { name: "Import synthetic demo data", exact: true })
      .click();
    expect((await importing).status()).toBe(201);
    await expect(seed).toContainText(
      "24 imported invoices · 6 imported customers",
    );
    await page
      .getByLabel("Teammate email", { exact: true })
      .fill("owner@example.test");
    await page
      .getByLabel("Role", { exact: true })
      .selectOption("finance_manager");
    await page
      .getByRole("button", { name: "Create invitation", exact: true })
      .click();
    const invite = page.getByLabel("Invitation code", { exact: true });
    await expect(invite).not.toHaveValue("");
    const inviteCode = await invite.inputValue();
    await page.goto(`/voice?workspace=${workspace.id}`);
    const before = (await (
      await page.request.get(`${base}/voice-sessions`)
    ).json()) as VoicePage;
    expect(before.items).toEqual([]);
    expect(before.connections).toEqual([]);
    const setup = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === `${base}/voice-sessions/setup` &&
        response.request().method() === "POST",
    );
    await page
      .getByRole("button", {
        name: "Prepare offline voice connection",
        exact: true,
      })
      .click();
    const setupResponse = await setup;
    expect(setupResponse.ok()).toBe(true);
    expect(Object.keys(setupResponse.request().postDataJSON())).toEqual([
      "request_id",
    ]);
    expect(setupResponse.request().headers()["x-csrf-token"]).toBeTruthy();
    const ready = (await setupResponse.json()) as VoicePage;
    expect(ready.connections).toHaveLength(1);
    expect(ready.can_prepare).toBe(true);
    const connection = ready.connections[0];
    if (!connection) throw Error("Actual probed offline connection required");
    if (privacy) {
      const panel = page.getByRole("region", {
        name: "Synthetic voice privacy",
        exact: true,
      });
      await panel
        .getByRole("button", {
          name: "Prepare private voice storage",
          exact: true,
        })
        .click();
      const actualInvoices = (await (
        await page.request.get(
          `${base}/receivables?search=INV-101&page_size=50`,
        )
      ).json()) as PageInvoiceView;
      const actualInvoice = actualInvoices.items.find(
        (i) => i.number === "INV-101",
      );
      if (!actualInvoice) throw Error("Actual seeded invoice required");
      await panel
        .getByRole("combobox", { name: "Consent customer", exact: true })
        .selectOption(actualInvoice.customer.id);
      await panel
        .getByRole("combobox", {
          name: "Consent voice connection",
          exact: true,
        })
        .selectOption(connection.id);
      await panel
        .getByRole("combobox", {
          name: "Consent storage connection",
          exact: true,
        })
        .selectOption({ index: 1 });
      await panel
        .getByRole("combobox", { name: "Participant decision", exact: true })
        .selectOption("GRANTED");
      for (const label of [
        "Audio capture",
        "Transcript",
        "Private storage",
        "Playback",
        "Provider retention",
      ])
        await panel.getByRole("checkbox", { name: label, exact: true }).check();
      await panel
        .getByLabel("Processing purpose", { exact: true })
        .fill("Synthetic privacy browser proof for one actual saved customer");
      await panel
        .getByLabel("Notice reviewed", { exact: true })
        .fill(
          "Fixed synthetic transcript and silent audio only. No customer recording.",
        );
      await panel
        .getByLabel("Jurisdiction", { exact: true })
        .fill("AU-VIC synthetic fixture");
      await panel
        .getByLabel("Consent evidence", { exact: true })
        .fill(
          "Current Owner explicitly reviewed each independent processing purpose.",
        );
      await panel
        .getByLabel("Consent expiry (your local time)", { exact: true })
        .fill(new Date(Date.now() + 86400000).toISOString().slice(0, 16));
      await panel
        .getByRole("checkbox", {
          name: "I reviewed this synthetic participant, notice and each separate processing choice.",
          exact: true,
        })
        .check();
      const saving = page.waitForResponse(
        (r) =>
          new URL(r.url()).pathname === `${base}/voice-consents` &&
          r.request().method() === "POST",
      );
      await panel
        .getByRole("button", {
          name: "Save synthetic consent decision",
          exact: true,
        })
        .click();
      const response = await saving;
      expect(response.status()).toBe(201);
      const consent = (await response.json()) as VoiceConsentView;
      await page
        .getByRole("combobox", { name: "Voice connection", exact: true })
        .selectOption(connection.id);
      await page
        .getByRole("combobox", {
          name: "Optional saved synthetic consent",
          exact: true,
        })
        .selectOption(consent.id);
    }
    await page
      .getByRole("combobox", { name: "Voice connection", exact: true })
      .selectOption(connection.id);
    await page.getByLabel("Find invoices", { exact: true }).fill("INV-101");
    await page.getByRole("checkbox", { name: "INV-101", exact: true }).check();
    const selectedPage = (await (
      await page.request.get(`${base}/receivables?search=INV-101&page_size=50`)
    ).json()) as PageInvoiceView;
    const primaryInvoice = selectedPage.items.find(
      (invoice) => invoice.number === "INV-101",
    );
    if (!primaryInvoice)
      throw Error("Actual verified seeded primary invoice required");
    const sourceFinancial = {
      outstanding: primaryInvoice.outstanding,
      financial_status: primaryInvoice.financial_status,
      source: { revision: primaryInvoice.source.revision },
    };
    if (businessFacts)
      await page
        .getByRole("combobox", {
          name: "Fixed synthetic scenario",
          exact: true,
        })
        .selectOption("business_facts");
    const objective = agentRequested
      ? "Review the selected synthetic invoice evidence in a browser conversation"
      : businessFacts
        ? "Review explicitly synthetic signed business facts without recording any speech"
        : "Inspect the synthetic invoice and create a collection follow-up task";
    let prepared: VoiceSessionView;
    if (agentRequested) {
      await page.goto(`/agents?workspace=${workspace.id}`);
      await page
        .getByRole("checkbox", { name: "Select INV-101", exact: true })
        .check();
      await page
        .getByRole("button", {
          name: "Use synthetic voice-review objective",
          exact: true,
        })
        .click();
      await expect(
        page.getByRole("textbox", {
          name: "Investigation objective",
          exact: true,
        }),
      ).toHaveValue(
        "Request an independently reviewed synthetic browser voice session",
      );
      await page
        .getByRole("button", { name: "Start investigation", exact: true })
        .click();
      await expect(page).toHaveURL(
        new RegExp(`/agent-runs/[a-f0-9-]+\\?workspace=${workspace.id}`),
      );
      await expect(
        page.getByText("Voice review requested · call not started", {
          exact: true,
        }),
      ).toBeVisible({ timeout: 30_000 });
      const sessionLink = page.getByRole("link", {
        name: "Open saved voice session and current eligibility",
        exact: true,
      });
      await expect(sessionLink).toBeVisible();
      const href = await sessionLink.getAttribute("href");
      if (!href) throw Error("Actual saved session link required");
      const sessionId = new URL(href, "http://test").pathname.split("/").at(-1);
      if (!sessionId) throw Error("Actual session ID required");
      prepared = (await (
        await page.request.get(`${base}/voice-sessions/${sessionId}`)
      ).json()) as VoiceSessionView;
      expect(prepared.state).toBe("awaiting_approval");
      expect(prepared.provider_call_id).toBeNull();
      expect(prepared.invocations).toEqual([]);
      await page.reload();
      await expect(
        page.getByText("Voice review requested · call not started", {
          exact: true,
        }),
      ).toBeVisible();
      await page
        .getByRole("link", {
          name: "Open saved voice session and current eligibility",
          exact: true,
        })
        .click();
    } else {
      await page
        .getByLabel("Session objective", { exact: true })
        .fill(objective);
      const preparing = page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname === `${base}/voice-sessions` &&
          response.request().method() === "POST",
      );
      await page
        .getByRole("button", { name: "Save session for approval", exact: true })
        .click();
      const preparedResponse = await preparing;
      expect(preparedResponse.status()).toBe(201);
      prepared = (await preparedResponse.json()) as VoiceSessionView;
    }
    expect(prepared.connection_id).toBe(connection.id);
    expect(prepared.source_scope).toHaveLength(1);
    await expect(page).toHaveURL(
      new RegExp(`/voice/${prepared.id}\\?workspace=${workspace.id}`),
    );
    const saved = page.getByRole("region", {
      name: "Saved voice session",
      exact: true,
    });
    await expect(saved).toContainText(objective);
    await expect(saved).toContainText("MOCKED_HTTP");
    await expect(saved).toContainText("Live voice BLOCKED");
    await expect(saved).toContainText("PSTN SKIPPED");
    await expect(
      page.getByRole("button", {
        name: "Start approved offline session",
        exact: true,
      }),
    ).toBeDisabled();
    const read = async () =>
      (await (
        await page.request.get(`${base}/voice-sessions/${prepared.id}`)
      ).json()) as VoiceSessionView;
    const ownApproval = (await (
      await page.request.get(`${base}/approvals/${prepared.approval_id}`)
    ).json()) as ApprovalView;
    expect(ownApproval.can_approve).toBe(false);
    const context = await browser.newContext();
    try {
      const reviewer = await context.newPage();
      await signIn(reviewer, password);
      await reviewer.getByLabel("Join code", { exact: true }).fill(inviteCode);
      await reviewer
        .getByRole("button", { name: "Accept invitation", exact: true })
        .click();
      await reviewer.goto(
        `/review?workspace=${workspace.id}&approval=${prepared.approval_id}`,
      );
      const approval = reviewer.getByRole("article", {
        name: `Approval ${prepared.approval_id}`,
        exact: true,
      });
      await expect(approval).toContainText("Offline voice session proposal");
      await expect(approval).toContainText("SYNTHETIC_OFFLINE");
      await expect(approval).toContainText(
        privacy ? "SYNTHETIC_ARTIFACTS" : "DISABLED",
      );
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
          "Independent review of exact synthetic invoice scope, offline participant and get_invoice/create_collection_task only. Real provider capture disabled; separately reviewed synthetic fixture purposes only; no live voice.",
        );
      const decision = reviewer.waitForResponse(
        (response) =>
          new URL(response.url()).pathname ===
            `${base}/approvals/${prepared.approval_id}/decisions` &&
          response.request().method() === "POST",
      );
      await captured
        .getByRole("button", { name: "Approve proposal", exact: true })
        .click();
      expect((await decision).status()).toBe(200);
      await expect.poll(async () => (await read()).can_start).toBe(true);
      // Approval has no automatic start authority: no provider call exists before the explicit action.
      const reviewedSession = await read();
      expect(reviewedSession.provider_call_id).toBeNull();
      const review = page.getByRole("checkbox", {
        name: "I reviewed this saved objective, invoice scope and independent approval.",
        exact: true,
      });
      await expect(review).toBeEnabled();
      await review.check();
      const starting = page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname ===
            `${base}/voice-sessions/${prepared.id}/start` &&
          response.request().method() === "POST",
      );
      await page
        .getByRole("button", {
          name: "Start approved offline session",
          exact: true,
        })
        .click();
      const startedResponse = await starting;
      expect(startedResponse.status()).toBe(200);
      expect(startedResponse.request().postDataJSON()).toEqual({
        expected_version: reviewedSession.version,
      });
      await expect
        .poll(async () => (await read()).changed_ids.length, {
          timeout: 30_000,
        })
        .toBe(1);
      const completed = await read();
      expect(completed.state).toBe("completed");
      expect(completed.events?.map((event) => event.kind)).toContain(
        "call.completed",
      );
      expect(completed.evidence_mode).toBe("MOCKED_HTTP");
      expect(completed.live_status).toBe("BLOCKED");
      expect(completed.pstn_status).toBe("SKIPPED");
      expect(completed.recording_available).toBe(false);
      expect(completed.transcript_available).toBe(false);
      expect(completed.invocations?.map((item) => item.tool)).toEqual(
        expect.arrayContaining(["get_invoice", "create_collection_task"]),
      );
      const task = completed.changed_ids[0];
      if (!task) throw Error("Actual committed callback task required");
      await expect(
        saved.getByRole("link", {
          name: `Open created task ${task}`,
          exact: true,
        }),
      ).toBeVisible();
      await page.reload();
      await expect(
        page.getByRole("link", {
          name: `Open created task ${task}`,
          exact: true,
        }),
      ).toBeVisible();
      expect((await read()).changed_ids).toEqual([task]);
      await page
        .getByRole("link", { name: `Open created task ${task}`, exact: true })
        .click();
      await expect(
        page.getByText(`Task reference: ${task}`, { exact: true }),
      ).toBeVisible();
      await page.reload();
      await expect(
        page.getByText(`Task reference: ${task}`, { exact: true }),
      ).toBeVisible();
      const finalApproval = (await (
        await page.request.get(`${base}/approvals/${prepared.approval_id}`)
      ).json()) as ApprovalView;
      expect(finalApproval.effective_status).toBe("consumed");
      const me = await (await page.request.get("/api/me")).json();
      expect(
        me.workspaces.find((item: WorkspaceView) => item.id === workspace.id),
      ).toMatchObject({ outbound_paused: true, automation_enabled: false });
      await page.goto(`/voice/${prepared.id}?workspace=${workspace.id}`);
      if (businessFacts) {
        expect(completed.demo_scenario).toBe("business_facts");
        if (!completed.business_evidence)
          throw Error("Persisted operational evidence is required");
        expect(completed.business_evidence).toHaveLength(3);
        expect(completed.artifacts).toEqual([]);
        expect(
          completed.outcomes?.map((outcome) => outcome.record_kind),
        ).toEqual(expect.arrayContaining(["task", "case", "promise", "hold"]));
        const caseId = completed.outcomes?.find(
          (outcome) => outcome.record_kind === "case",
        )?.changed_ids[0];
        const promiseId = completed.outcomes?.find(
          (outcome) => outcome.record_kind === "promise",
        )?.changed_ids[0];
        const commitmentSource = completed.business_evidence.find(
          (source) => source.fact_kind === "promise",
        );
        const stopSource = completed.business_evidence.find(
          (source) => source.fact_kind === "contact_stop",
        );
        if (!caseId || !promiseId || !commitmentSource || !stopSource)
          throw Error("Actual committed source/outcome IDs required");
        await page
          .getByRole("link", { name: `Open case ${caseId}`, exact: true })
          .click();
        await expect(page).toHaveURL(
          new RegExp(`/cases/${caseId}\\?workspace=${workspace.id}`),
        );
        await expect(
          page.getByRole("region", {
            name: "Signed call business facts",
            exact: true,
          }),
        ).toContainText("Unverified operational business facts");
        await expect(
          page.locator('a[href*="/communications/null"]'),
        ).toHaveCount(0);
        await page.reload();
        await expect(
          page.getByRole("link", {
            name: "Open source voice session",
            exact: true,
          }),
        ).toHaveAttribute(
          "href",
          `/voice/${prepared.id}?workspace=${workspace.id}`,
        );
        await page.goto(`/voice/${prepared.id}?workspace=${workspace.id}`);
        await page
          .getByRole("link", { name: `Open promise ${promiseId}`, exact: true })
          .click();
        await expect(page).toHaveURL(
          new RegExp(`/promises/${promiseId}\\?workspace=${workspace.id}`),
        );
        const promiseSource = page.getByRole("region", {
          name: "Signed call business facts",
          exact: true,
        });
        await promiseSource
          .getByText("Exact signed business facts and references", {
            exact: true,
          })
          .click();
        await expect(promiseSource).toContainText(commitmentSource.facts_hash);
        await expect(promiseSource).toContainText("4000");
        await expect(
          page.getByText("Check scheduling: Pending confirmation", {
            exact: true,
          }),
        ).toBeVisible();
        const human = page.getByRole("region", {
          name: "Human commitment review",
          exact: true,
        });
        await human
          .getByLabel("Review reason", { exact: true })
          .fill(
            "I reviewed these exact synthetic normalized call facts; no payment is verified.",
          );
        await human
          .getByRole("checkbox", {
            name: "I reviewed the source and confirm these exact commitment terms.",
            exact: true,
          })
          .check();
        await expect(human).toContainText(commitmentSource.facts_hash);
        const confirming = page.waitForResponse(
          (response) =>
            new URL(response.url()).pathname ===
              `${base}/promises/${promiseId}/confirm` &&
            response.request().method() === "POST",
        );
        await human
          .getByRole("button", { name: "Confirm commitment", exact: true })
          .click();
        const confirmed = await confirming;
        expect(confirmed.status()).toBe(200);
        expect(
          confirmed.request().postDataJSON().confirmation_evidence,
        ).toEqual({
          kind: "voice_operational",
          voice_evidence_id: commitmentSource.id,
          facts_hash: commitmentSource.facts_hash,
        });
        expect((await confirmed.json()).status).toBe("ACTIVE");
        await page.reload();
        await expect(
          page
            .getByRole("region", {
              name: "Human commitment review",
              exact: true,
            })
            .getByRole("button", { name: "Cancel commitment", exact: true }),
        ).toBeVisible();
        await page.goto(
          `/receivables/${primaryInvoice.id}?workspace=${workspace.id}`,
        );
        const restriction = page.getByRole("region", {
          name: "Customer contact restrictions",
          exact: true,
        });
        await expect(restriction).toContainText(
          "Customer contact paused pending human review",
        );
        await restriction
          .getByLabel("Human disposition reason", { exact: true })
          .fill(
            "Human reviewed the exact synthetic call contact-stop facts and all original holds.",
          );
        await restriction
          .getByRole("button", {
            name: "Review release of this customer restriction",
            exact: true,
          })
          .click();
        const capturedStop = page.getByRole("region", {
          name: "Captured contact restriction review",
          exact: true,
        });
        await capturedStop
          .getByText("Exact signed business facts and references", {
            exact: true,
          })
          .click();
        await expect(capturedStop).toContainText(stopSource.facts_hash);
        const releasing = page.waitForResponse(
          (response) =>
            new URL(response.url()).pathname.endsWith("/release") &&
            new URL(response.url()).pathname.startsWith(
              `${base}/contact-stops/`,
            ) &&
            response.request().method() === "POST",
        );
        await capturedStop
          .getByRole("button", {
            name: "Confirm reviewed release",
            exact: true,
          })
          .click();
        const released = await releasing;
        expect(released.status()).toBe(200);
        expect((await released.json()).pending).toBe(false);
        await page.reload();
        await expect(restriction).toContainText(
          "This contact restriction was explicitly released",
        );
        const currentPage = (await (
          await page.request.get(
            `${base}/receivables?search=INV-101&page_size=50`,
          )
        ).json()) as PageInvoiceView;
        const currentInvoice = currentPage.items.find(
          (invoice) => invoice.id === primaryInvoice.id,
        );
        expect(currentInvoice).toMatchObject(sourceFinancial);
        const reloadedSources = (await read()).business_evidence;
        if (!reloadedSources)
          throw Error("Retained source evidence is required on reload");
        expect(reloadedSources.map((source) => source.id)).toEqual(
          completed.business_evidence.map((source) => source.id),
        );
        expect((await read()).changed_ids).toEqual([task]);
      }
      if (privacy) {
        const privacyPanel = page.getByRole("region", {
          name: "Post-call history and privacy",
          exact: true,
        });
        const reconcile = page.waitForResponse(
          (r) =>
            new URL(r.url()).pathname ===
              `${base}/voice-sessions/${prepared.id}/reconcile` &&
            r.request().method() === "POST",
        );
        await privacyPanel
          .getByRole("button", {
            name: "Reconcile completed call history",
            exact: true,
          })
          .click();
        expect((await reconcile).status()).toBe(200);
        await privacyPanel
          .getByRole("button", {
            name: "Read permitted private transcript",
            exact: true,
          })
          .click();
        await expect(
          page.getByRole("region", {
            name: "Private synthetic transcript",
            exact: true,
          }),
        ).toContainText(
          "This fixed fixture verifies explicitly permitted transcript storage.",
        );
        const archived = await read();
        expect(archived.artifacts).toHaveLength(2);
        expect(archived.changed_ids).toEqual([task]);
        const recording = archived.artifacts?.find(
          (a) => a.content_type === "audio/wav",
        );
        if (!recording)
          throw Error("Actual private recording fixture required");
        const playback = await page.request.get(
          `${base}/artifacts/${recording.id}/playback`,
        );
        expect(playback.status()).toBe(200);
        expect(playback.headers()["content-type"]).toBe("audio/wav");
        expect(playback.headers()["cache-control"]).toBe("no-store");
        await expect(privacyPanel.locator("audio")).toHaveCount(1);
        await privacyPanel
          .getByLabel("Withdrawal reason", { exact: true })
          .fill(
            "Synthetic participant withdraws all permitted purposes after review.",
          );
        await privacyPanel
          .getByRole("checkbox", {
            name: "Withdraw all saved purposes and revoke private access now",
            exact: true,
          })
          .check();
        const withdrawal = page.waitForResponse(
          (r) =>
            r.url().includes("/voice-consents/") &&
            r.url().endsWith("/revoke") &&
            r.request().method() === "POST",
        );
        await privacyPanel
          .getByRole("button", { name: "Withdraw consent", exact: true })
          .click();
        expect((await withdrawal).status()).toBe(200);
        await expect(privacyPanel.locator("audio")).toHaveCount(0);
        for (const artifact of archived.artifacts ?? []) {
          expect(
            (
              await page.request.get(`${base}/artifacts/${artifact.id}/content`)
            ).status(),
          ).toBe(404);
          await expect
            .poll(
              async () => {
                const state = (await (
                  await page.request.get(
                    `${base}/artifacts/${artifact.id}/privacy`,
                  )
                ).json()) as ArtifactPrivacyView;
                return {
                  status: state.purge_status,
                  provider: state.provider_purge_status,
                };
              },
              { timeout: 30000 },
            )
            .toEqual({ status: "deleted", provider: "PURGED" });
        }
        await page.reload();
        await expect(
          page.getByRole("region", {
            name: "Post-call history and privacy",
            exact: true,
          }),
        ).toContainText("Withdrawn");
        expect((await read()).changed_ids).toEqual([task]);
      }
      await expect(page.locator("audio, video")).toHaveCount(0);
      await page.setViewportSize({ width: 390, height: 844 });
      await expect
        .poll(() =>
          page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        )
        .toBe(true);
    } finally {
      await context.close();
    }
  });
