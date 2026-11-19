"use client";
import { useRef, useState } from "react";
import Link from "next/link";
import { VoiceBusinessSource } from "@/features/conversations/voice-business-source";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { WorkspaceView, VoicePage } from "@/lib/generated-api";
import { ErrorState, stamp } from "@/features/receivables/shared";
import {
  captureVoice,
  canStartVoice,
  sameVoiceSnapshot,
  voiceApi,
  voiceStartCommand,
  type PrepareVoice,
  type VoiceSessionView,
} from "./voice-api";
import { ConsentPanel, VoicePrivacyDetail } from "./voice-privacy";
import { voicePrivacyApi } from "./voice-privacy-api";
import { VoiceLiveControls } from "./voice-live-controls";
const href = (path: string, workspace: string) =>
  `${path}?workspace=${encodeURIComponent(workspace)}`;
function Restrictions({ reasons }: { reasons: string[] }) {
  return reasons.length ? (
    <ul aria-label="Current restrictions">
      {reasons.map((reason) => (
        <li key={reason}>{reason.replaceAll("_", " ")}</li>
      ))}
    </ul>
  ) : null;
}
export function VoiceRecord({
  session,
  workspace,
}: {
  session: VoiceSessionView;
  workspace: string;
}) {
  return (
    <section className="panel" aria-label="Saved voice session">
      <div className="section-heading">
        <h2>
          {session.evidence_mode === "LIVE"
            ? "Browser voice session"
            : "Offline voice session"}
        </h2>
        <span className="badge">{session.state}</span>
      </div>
      <p>
        <strong>{session.evidence_mode ?? "Evidence mode unavailable"}</strong>{" "}
        · Live voice {session.live_status ?? "status unavailable"} · PSTN{" "}
        {session.pstn_status ?? "status unavailable"}
      </p>
      <p>
        {session.evidence_mode === "LIVE"
          ? "Recording and transcript capture disabled. Only the saved, consented participant may join this browser call."
          : session.consent
            ? "Optional private originals use the separately saved synthetic consent below. Real provider capture remains disabled."
            : "Recording and transcript capture disabled. This session uses a synthetic offline participant."}
      </p>
      <p className="task-notes">{session.objective}</p>
      {session.evidence_mode !== "LIVE" && (
        <p>
          Fixed synthetic scenario:{" "}
          {session.demo_scenario === "business_facts"
            ? "Business facts — task, amount issue, AUD 40 pending commitment due seven UTC days after saved scope, and customer contact stop"
            : "Invoice read and follow-up task"}
          .
        </p>
      )}
      {session.evidence_mode === "LIVE" && (
        <>
          <p>Saved participant: {session.participant_email ?? "Unavailable"}</p>
          <p>
            {session.state === "completed"
              ? `Authenticated completion received. Saved stop outcome: ${session.stop_outcome ?? "NOT_REQUESTED"}.`
              : session.stop_outcome === "ACCEPTED"
                ? "Stop accepted — waiting for authenticated completion."
                : session.stop_outcome === "UNKNOWN"
                  ? "Stop outcome unknown. No automatic retry; waiting for authenticated completion."
                  : session.stop_outcome === "ATTEMPTED"
                    ? "Stop requested; outcome not confirmed."
                    : session.stop_outcome === "FAILED"
                      ? "Stop not confirmed. Review the saved outcome; no automatic retry."
                      : session.stop_outcome === "RESERVED"
                        ? "Safety-stop capacity reserved; no stop requested."
                        : "No stop requested."}
          </p>
        </>
      )}
      <dl className="detail-grid">
        <div>
          <dt>Saved version</dt>
          <dd>{session.version}</dd>
        </div>
        <div>
          <dt>Expires</dt>
          <dd>{stamp(session.expires_at)}</dd>
        </div>
        <div>
          <dt>Connection</dt>
          <dd>{session.connection_id}</dd>
        </div>
        <div>
          <dt>Provider call reference</dt>
          <dd>{session.provider_call_id ?? "Not started"}</dd>
        </div>
      </dl>
      <Link
        href={`${href("/review", workspace)}&approval=${encodeURIComponent(session.approval_id)}`}
      >
        Open independent approval
      </Link>
      <h3>Captured source scope</h3>
      {(session.source_scope ?? []).length ? (
        <ul>
          {(session.source_scope ?? []).map((source) => (
            <li key={source.invoice_id}>
              <Link
                href={href(
                  `/receivables/${encodeURIComponent(source.invoice_id)}`,
                  workspace,
                )}
              >
                Invoice {source.invoice_id}
              </Link>{" "}
              · Revision {source.source_revision} · Scope captured{" "}
              {stamp(source.captured_at)}
            </li>
          ))}
        </ul>
      ) : (
        <p>No source scope recorded.</p>
      )}
      <p className="muted">
        Scope capture time records the session binding. Accounting observation
        time appears in the actual invoice tool result.
      </p>
      <Restrictions reasons={session.reasons ?? []} />
      {(session.business_evidence ?? []).map((source) => (
        <VoiceBusinessSource
          key={source.id}
          source={source}
          workspace={workspace}
        />
      ))}
      <h3>Committed outcomes</h3>
      {session.outcomes?.length ? (
        <ul>
          {session.outcomes.map((outcome) => (
            <li key={outcome.invocation_id}>
              <p>
                Committed {outcome.record_kind} · action {outcome.action_id}
              </p>
              {outcome.changed_ids.map((id) =>
                outcome.record_kind === "hold" ? (
                  <p key={id}>
                    Saved hold {id} ·{" "}
                    <Link
                      href={href(
                        `/receivables/${outcome.invoice_id}`,
                        workspace,
                      )}
                    >
                      Review current invoice restrictions
                    </Link>
                  </p>
                ) : (
                  <p key={id}>
                    <Link
                      href={href(
                        `/${outcome.record_kind === "case" ? "cases" : outcome.record_kind === "promise" ? "promises" : "tasks"}/${id}`,
                        workspace,
                      )}
                    >
                      Open{" "}
                      {outcome.record_kind === "task"
                        ? "created task"
                        : outcome.record_kind}{" "}
                      {id}
                    </Link>
                  </p>
                ),
              )}
            </li>
          ))}
        </ul>
      ) : session.changed_ids.length ? (
        <ul>
          {session.changed_ids.map((id) => (
            <li key={id}>
              <Link href={href(`/tasks/${id}`, workspace)}>
                Open created task {id}
              </Link>
            </li>
          ))}
        </ul>
      ) : (
        <p>No committed business outcome recorded.</p>
      )}
      <h3>Tool activity</h3>
      {(session.invocations ?? []).length ? (
        (session.invocations ?? []).map((item) => (
          <details key={item.id}>
            <summary>
              {item.tool} · {stamp(item.created_at)}
            </summary>
            <p>Invocation {item.native_id}</p>
            <h4>Arguments</h4>
            <pre className="communication-body">
              {JSON.stringify(item.arguments, null, 2)}
            </pre>
            <h4>Result</h4>
            {item.result === null ? (
              <p>No committed result</p>
            ) : (
              <pre className="communication-body">
                {JSON.stringify(item.result, null, 2)}
              </pre>
            )}
            {item.action_id && (
              <p>Business action reference: {item.action_id}</p>
            )}
          </details>
        ))
      ) : (
        <p>No tool activity recorded.</p>
      )}
      <h3>Session events</h3>
      {(session.events ?? []).length ? (
        <ol>
          {(session.events ?? []).map((event) => (
            <li key={event.id}>
              {event.kind} · {stamp(event.created_at)}
            </li>
          ))}
        </ol>
      ) : (
        <p>No session events recorded.</p>
      )}
    </section>
  );
}
export function VoicePanel({
  workspace,
  csrfToken,
  sessionId,
}: {
  workspace: WorkspaceView;
  csrfToken: string;
  sessionId?: string;
}) {
  return sessionId ? (
    <VoiceDetail
      key={`${workspace.id}-${sessionId}`}
      workspace={workspace}
      csrfToken={csrfToken}
      sessionId={sessionId}
    />
  ) : (
    <VoiceHome key={workspace.id} workspace={workspace} csrfToken={csrfToken} />
  );
}
function VoiceDetail({
  workspace,
  csrfToken,
  sessionId,
}: {
  workspace: WorkspaceView;
  csrfToken: string;
  sessionId: string;
}) {
  const query = useQuery({
    queryKey: ["voice-session", workspace.id, sessionId],
    queryFn: ({ signal }) => voiceApi.get(workspace.id, sessionId, signal),
    retry: false,
    refetchInterval: 3000,
  });
  const [reviewed, setReviewed] = useState<VoiceSessionView | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const current = query.data;
  const eligible = canStartVoice(
    reviewed,
    current,
    !query.isError && !query.isFetching && !busy,
  );
  async function start() {
    if (!eligible || !reviewed) return;
    setBusy(true);
    setError("");
    try {
      await voiceApi.start(
        workspace.id,
        sessionId,
        voiceStartCommand(reviewed),
        csrfToken,
      );
      setReviewed(null);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Session result unavailable. Refresh before trying again.",
      );
      setReviewed(null);
    } finally {
      await query.refetch();
      setBusy(false);
    }
  }
  return (
    <>
      <Link href={href("/voice", workspace.id)}>All voice sessions</Link>
      {query.isPending ? (
        <p role="status">Loading session…</p>
      ) : query.isError ? (
        <ErrorState
          label="Voice session unavailable"
          error={query.error}
          retry={() => query.refetch()}
        />
      ) : (
        <>
          <VoiceRecord session={query.data} workspace={workspace.id} />
          {query.data.evidence_mode === "LIVE" && (
            <VoiceLiveControls
              session={query.data}
              workspace={workspace.id}
              csrf={csrfToken}
              fresh={!query.isFetching && !query.isError}
              refresh={() => query.refetch()}
            />
          )}
          {query.data.evidence_mode !== "LIVE" && (
            <VoicePrivacyDetail
              session={query.data}
              workspace={workspace}
              csrf={csrfToken}
              refresh={() => query.refetch()}
            />
          )}
          <section
            className="panel"
            aria-label={
              query.data.evidence_mode === "LIVE"
                ? "Review and start browser session"
                : "Review and start offline session"
            }
          >
            <h3>Review before starting</h3>
            <p>
              An independent approval and current server checks are required.
              {query.data.evidence_mode === "LIVE"
                ? "Starting creates one bounded provider call. It does not join your microphone or enable capture."
                : "Starting this offline session does not enable live voice or recording."}
            </p>
            <label>
              <input
                type="checkbox"
                checked={!!reviewed && sameVoiceSnapshot(reviewed, query.data)}
                disabled={!query.data.can_start || query.isFetching || busy}
                onChange={(event) =>
                  setReviewed(
                    event.target.checked ? captureVoice(query.data) : null,
                  )
                }
              />
              I reviewed this saved objective, invoice scope and independent
              approval.
            </label>
            {reviewed && !sameVoiceSnapshot(reviewed, query.data) && (
              <p role="alert">
                The saved session changed. Review its current version again.
              </p>
            )}
            <button type="button" disabled={!eligible} onClick={start}>
              {busy
                ? "Starting session…"
                : query.data.evidence_mode === "LIVE"
                  ? "Start approved browser session"
                  : "Start approved offline session"}
            </button>
            {error && <p role="alert">{error}</p>}
          </section>
        </>
      )}
    </>
  );
}
function VoiceHome({
  workspace,
  csrfToken,
}: {
  workspace: WorkspaceView;
  csrfToken: string;
}) {
  const list = useQuery({
    queryKey: ["voice-sessions", workspace.id],
    queryFn: ({ signal }) => voiceApi.list(workspace.id, signal),
    retry: false,
    refetchInterval: 4000,
  });
  const setupRequest = useRef<string | null>(null);
  const [setupBusy, setSetupBusy] = useState(false);
  const [setupError, setSetupError] = useState("");
  async function setup() {
    if (!list.data?.can_setup || list.isError || list.isFetching || setupBusy)
      return;
    setupRequest.current ??= crypto.randomUUID();
    setSetupBusy(true);
    setSetupError("");
    try {
      await voiceApi.setup(
        workspace.id,
        { request_id: setupRequest.current },
        csrfToken,
      );
    } catch (reason) {
      setSetupError(
        reason instanceof Error
          ? reason.message
          : "Connection setup is not confirmed. Refresh its saved status.",
      );
    } finally {
      await list.refetch();
      setSetupBusy(false);
    }
  }
  return (
    <>
      <section className="panel">
        <h2>Voice sessions</h2>
        <p>
          Prepare a saved invoice brief for independent approval. Inspect actual
          saved tool activity and committed business outcomes here.
        </p>
        <p>
          Recording and transcripts are disabled by default. The saved server
          connection determines whether a session uses simulated HTTP or an
          explicitly authorized LIVE browser provider.
        </p>
      </section>
      {list.isPending ? (
        <p role="status">Loading voice sessions…</p>
      ) : list.isError ? (
        <ErrorState
          label="Voice sessions unavailable"
          error={list.error}
          retry={() => list.refetch()}
        />
      ) : (
        <>
          <section
            className="panel"
            aria-label={
              list.data.evidence_mode === "LIVE"
                ? "Browser voice connection setup"
                : "Offline voice connection setup"
            }
          >
            <h3>
              {list.data.evidence_mode === "LIVE"
                ? "LIVE browser connection"
                : "Offline connection"}
            </h3>
            <p>
              {list.data.evidence_mode === "LIVE"
                ? "This explicit action checks the saved provider assistant and authentication profile under the existing server grant. It starts no call and enables no capture."
                : "This explicit action probes the server-configured synthetic assistant. It enables no live voice or capture."}
            </p>
            <Restrictions reasons={list.data.setup_reasons ?? []} />
            {list.data.can_setup && (
              <button
                type="button"
                disabled={setupBusy || list.isFetching}
                onClick={setup}
              >
                {setupBusy
                  ? "Checking connection…"
                  : list.data.evidence_mode === "LIVE"
                    ? "Verify saved browser connection"
                    : "Prepare offline voice connection"}
              </button>
            )}
            {setupError && <p role="alert">{setupError}</p>}
          </section>
          {list.data.evidence_mode !== "LIVE" && (
            <ConsentPanel workspace={workspace} csrfToken={csrfToken} />
          )}
          <PrepareSession
            workspace={workspace}
            csrfToken={csrfToken}
            page={list.data}
            fresh={!list.isFetching}
          />
          <section className="panel">
            <div className="section-heading">
              <h3>Saved sessions</h3>
              <button
                type="button"
                disabled={list.isFetching}
                onClick={() => list.refetch()}
              >
                Refresh sessions
              </button>
            </div>
            {list.data.items.length ? (
              <ul>
                {list.data.items.map((session) => (
                  <li key={session.id}>
                    <Link
                      href={href(
                        `/voice/${encodeURIComponent(session.id)}`,
                        workspace.id,
                      )}
                    >
                      {session.objective}
                    </Link>{" "}
                    · {session.state} ·{" "}
                    {session.evidence_mode ?? "Evidence mode unavailable"}
                  </li>
                ))}
              </ul>
            ) : (
              <p>No saved voice sessions.</p>
            )}
          </section>
        </>
      )}
    </>
  );
}
function PrepareSession({
  workspace,
  csrfToken,
  page,
  fresh,
}: {
  workspace: WorkspaceView;
  csrfToken: string;
  page: VoicePage;
  fresh: boolean;
}) {
  const router = useRouter();
  const consents = useQuery({
    queryKey: ["voice-consents", workspace.id],
    queryFn: ({ signal }) => voicePrivacyApi.list(workspace.id, signal),
    retry: false,
  });
  const [consentId, setConsentId] = useState("");
  const [scenario, setScenario] = useState<"task" | "business_facts">("task");
  const [connection, setConnection] = useState(""),
    [objective, setObjective] = useState(""),
    [search, setSearch] = useState(""),
    [selected, setSelected] = useState<{ id: string; number: string }[]>([]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const pending = useRef<PrepareVoice | null>(null);
  const invoices = useQuery({
    queryKey: ["voice-invoice-choices", workspace.id, search],
    queryFn: ({ signal }) =>
      api.invoices(workspace.id, { search, page_size: 50 }, signal),
    retry: false,
    enabled: page.can_prepare,
  });
  const canPrepare =
    page.can_prepare &&
    fresh &&
    page.connections.some((item) => item.id === connection) &&
    selected.length > 0 &&
    selected.length <= 3 &&
    !!objective.trim() &&
    !busy;
  async function prepare() {
    if (!canPrepare) return;
    setBusy(true);
    setError("");
    const fields = {
      connection_id: connection,
      invoice_ids: selected.map((item) => item.id),
      objective: objective.trim(),
      ...(page.evidence_mode !== "LIVE" && scenario === "business_facts"
        ? { demo_scenario: scenario }
        : {}),
      ...(page.evidence_mode !== "LIVE" && consentId
        ? { consent_id: consentId }
        : {}),
    };
    const previous = pending.current;
    const command =
      previous &&
      previous.connection_id === fields.connection_id &&
      previous.objective === fields.objective &&
      previous.demo_scenario === fields.demo_scenario &&
      previous.consent_id === fields.consent_id &&
      JSON.stringify(previous.invoice_ids) ===
        JSON.stringify(fields.invoice_ids)
        ? previous
        : { request_id: crypto.randomUUID(), ...fields };
    pending.current = command;
    try {
      const saved = await voiceApi.prepare(workspace.id, command, csrfToken);
      router.push(href(`/voice/${encodeURIComponent(saved.id)}`, workspace.id));
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Preparation result unavailable. Refresh saved sessions before retrying.",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="panel"
      aria-label={
        page.evidence_mode === "LIVE"
          ? "Prepare browser voice session"
          : "Prepare offline voice session"
      }
    >
      <h3>
        {page.evidence_mode === "LIVE"
          ? "Prepare a capture-off browser session"
          : "Prepare an offline session"}
      </h3>
      <Restrictions reasons={page.reasons} />
      {page.can_prepare && (
        <>
          <label>
            Voice connection
            <select
              value={connection}
              disabled={busy}
              onChange={(event) => setConnection(event.target.value)}
            >
              <option value="">Choose an available connection</option>
              {page.connections.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          {page.evidence_mode !== "LIVE" && (
            <label>
              Optional saved synthetic consent
              <select
                value={consentId}
                disabled={busy || consents.isError}
                onChange={(e) => setConsentId(e.target.value)}
              >
                <option value="">Capture off — no consent selected</option>
                {consents.data?.items
                  .filter(
                    (c) =>
                      c.active && c.command.voice_connection_id === connection,
                  )
                  .map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.command.purpose} · {c.id}
                    </option>
                  ))}
              </select>
            </label>
          )}
          <label>
            Find invoices
            <input
              type="search"
              value={search}
              disabled={busy}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          {invoices.isPending ? (
            <p role="status">Loading invoices…</p>
          ) : invoices.isError ? (
            <ErrorState
              label="Invoice choices unavailable"
              error={invoices.error}
              retry={() => invoices.refetch()}
            />
          ) : (
            <ul>
              {invoices.data.items.map((invoice) => (
                <li key={invoice.id}>
                  <label>
                    <input
                      type="checkbox"
                      checked={selected.some((item) => item.id === invoice.id)}
                      disabled={
                        busy ||
                        (selected.length >= 3 &&
                          !selected.some((item) => item.id === invoice.id))
                      }
                      onChange={(event) =>
                        setSelected((items) =>
                          event.target.checked
                            ? [
                                ...items,
                                { id: invoice.id, number: invoice.number },
                              ]
                            : items.filter((item) => item.id !== invoice.id),
                        )
                      }
                    />
                    {invoice.number}
                  </label>
                </li>
              ))}
            </ul>
          )}
          <p>{selected.length} of 3 invoices selected</p>
          {selected.map((item) => (
            <button
              key={item.id}
              type="button"
              disabled={busy}
              onClick={() =>
                setSelected((items) =>
                  items.filter((other) => other.id !== item.id),
                )
              }
            >
              Remove {item.number}
            </button>
          ))}
          {page.evidence_mode !== "LIVE" && (
            <>
              <label htmlFor="voice-demo-scenario">
                Fixed synthetic scenario
              </label>
              <select
                id="voice-demo-scenario"
                value={scenario}
                disabled={busy}
                onChange={(event) =>
                  setScenario(
                    event.target.value === "business_facts"
                      ? "business_facts"
                      : "task",
                  )
                }
              >
                <option value="task">Invoice read and follow-up task</option>
                <option value="business_facts">
                  Signed business facts and customer contact stop
                </option>
              </select>
              {scenario === "business_facts" && (
                <p>
                  Explicit synthetic example: a normalized amount issue, AUD 40
                  commitment due seven UTC days after saving, and a contact-stop
                  request for the primary invoice's customer. Select an AUD
                  invoice with at least AUD 40 outstanding. Terms stay
                  unverified until human review; no payment, transcript or real
                  call is created.
                </p>
              )}
            </>
          )}
          <label>
            Session objective
            <textarea
              value={objective}
              maxLength={1000}
              disabled={busy}
              onChange={(event) => setObjective(event.target.value)}
            />
          </label>
          <button type="button" disabled={!canPrepare} onClick={prepare}>
            {busy ? "Saving session…" : "Save session for approval"}
          </button>
        </>
      )}
      {!page.can_prepare && (
        <p>
          Preparation is unavailable under the current server configuration or
          membership.
        </p>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
