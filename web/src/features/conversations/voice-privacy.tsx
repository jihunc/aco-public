"use client";
import { useRef, useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { stamp } from "@/features/receivables/shared";
import type {
  ArtifactPrivacyView,
  CaptureScopes,
  PrivacyCommand,
  RecordVoiceConsent,
  VoiceConsentView,
  VoiceSessionView,
  WorkspaceView,
} from "@/lib/generated-api";
import {
  emptyCaptureScopes,
  privatePath,
  samePrivacyReview,
  voicePrivacyApi,
  type TranscriptTurn,
} from "./voice-privacy-api";
export const scopeLabels: Record<keyof CaptureScopes, string> = {
  audio_capture: "Audio capture",
  transcript: "Transcript",
  private_storage: "Private storage",
  playback: "Playback",
  provider_retention: "Provider retention",
};
const scopeKeys = Object.keys(scopeLabels) as (keyof CaptureScopes)[];
const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "Result unavailable. Refresh saved history.";
export function ConsentRecord({ consent }: { consent: VoiceConsentView }) {
  return (
    <section
      aria-label="Saved processing consent"
      style={{ minWidth: 0, overflowWrap: "anywhere" }}
    >
      <h4>Explicit synthetic consent</h4>
      <p>
        {consent.active ? "Current" : "Unavailable"} ·{" "}
        {consent.command.decision} · FAKE originals only
      </p>
      <p>{consent.command.purpose}</p>
      <p>Notice: {consent.command.notice}</p>
      <p>
        Jurisdiction: {consent.command.jurisdiction} · Recorded{" "}
        {stamp(consent.recorded_at)} · Expires{" "}
        {stamp(consent.command.expires_at)}
      </p>
      <p>
        Recorded by {consent.recorded_by} · Subject {consent.command.subject} ·
        Method {consent.command.method}
      </p>
      <p>Evidence: {consent.command.evidence}</p>
      <ul>
        {scopeKeys.map((key) => (
          <li key={key}>
            {scopeLabels[key]}:{" "}
            {consent.command.scopes[key] ? "Permitted" : "Not permitted"}
          </li>
        ))}
      </ul>
      <p>
        Consent {consent.id} · Material hash {consent.material_hash}
      </p>
      {consent.revoked_at && (
        <p>
          Withdrawn {stamp(consent.revoked_at)} · {consent.revocation_reason}
        </p>
      )}
      {consent.reasons.map((reason) => (
        <p key={reason}>{reason.replaceAll("_", " ")}</p>
      ))}
    </section>
  );
}
export function ConsentPanel({
  workspace,
  csrfToken,
}: {
  workspace: WorkspaceView;
  csrfToken: string;
}) {
  const cache = useQueryClient();
  const query = useQuery({
    queryKey: ["voice-consents", workspace.id],
    queryFn: ({ signal }) => voicePrivacyApi.list(workspace.id, signal),
    retry: false,
    refetchInterval: 5000,
  });
  const customers = useQuery({
    queryKey: ["voice-consent-customers", workspace.id],
    queryFn: ({ signal }) => api.customers(workspace.id, "", signal),
    retry: false,
  });
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [scopes, setScopes] = useState<CaptureScopes>(emptyCaptureScopes),
    [decision, setDecision] = useState<"GRANTED" | "DECLINED">("DECLINED"),
    [reviewed, setReviewed] = useState(false);
  const [customer, setCustomer] = useState(""),
    [voice, setVoice] = useState(""),
    [storage, setStorage] = useState("");
  const [purpose, setPurpose] = useState(""),
    [notice, setNotice] = useState(""),
    [jurisdiction, setJurisdiction] = useState(""),
    [evidence, setEvidence] = useState(""),
    [expires, setExpires] = useState("");
  const pending = useRef<RecordVoiceConsent | null>(null),
    setupRequest = useRef<string | null>(null);
  async function refresh() {
    await cache.invalidateQueries({
      queryKey: ["voice-consents", workspace.id],
    });
  }
  async function setup() {
    if (!query.data?.can_prepare_storage || busy) return;
    setBusy(true);
    setError("");
    setupRequest.current ??= crypto.randomUUID();
    try {
      await voicePrivacyApi.setup(
        workspace.id,
        {
          request_id: setupRequest.current,
          evidence:
            "Current Owner explicitly requested local private storage preparation for synthetic voice originals",
        },
        csrfToken,
      );
      await refresh();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    if (!reviewed || !query.data?.can_record || busy) return;
    setBusy(true);
    setError("");
    try {
      const fields = {
        customer_id: customer,
        voice_connection_id: voice,
        storage_connection_id: storage || null,
        decision,
        scopes,
        purpose,
        notice,
        jurisdiction,
        evidence,
        expires_at: new Date(expires).toISOString(),
      };
      const previous = pending.current;
      const command: RecordVoiceConsent =
        previous &&
        JSON.stringify({ ...previous, request_id: undefined }) ===
          JSON.stringify(fields)
          ? previous
          : { request_id: crypto.randomUUID(), ...fields };
      pending.current = command;
      await voicePrivacyApi.record(workspace.id, command, csrfToken);
      pending.current = null;
      setReviewed(false);
      setScopes(emptyCaptureScopes());
      setDecision("DECLINED");
      await refresh();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="panel"
      aria-label="Synthetic voice privacy"
      style={{ minWidth: 0, overflowWrap: "anywhere" }}
    >
      <h3>Consent and private originals</h3>
      <p>
        Real provider capture remains disabled. Optional FAKE transcript and
        silent audio fixtures require separate explicit processing choices and
        independent session approval. Synthetic retention is at most 30 days;
        withdrawal revokes access immediately.
      </p>
      {query.isError ? (
        <p role="alert">
          Privacy eligibility unavailable.{" "}
          <button onClick={() => query.refetch()}>Refresh privacy</button>
        </p>
      ) : (
        query.data && (
          <>
            <p>{query.data.storage_setup_reasons?.join(" · ")}</p>
            {query.data.can_prepare_storage && (
              <button disabled={busy || query.isFetching} onClick={setup}>
                {busy
                  ? "Preparing private storage…"
                  : "Prepare private voice storage"}
              </button>
            )}
            <p>{query.data.reasons.join(" · ")}</p>
            {query.data.can_record && (
              <fieldset
                style={{ minWidth: 0 }}
                disabled={busy || query.isFetching}
                onChange={() => setReviewed(false)}
              >
                <legend>Record a synthetic participant decision</legend>
                <label>
                  Consent customer
                  <select
                    value={customer}
                    onChange={(e) => setCustomer(e.target.value)}
                  >
                    <option value="">Choose actual synthetic customer</option>
                    {customers.data?.items
                      .filter((c) => c.synthetic)
                      .map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.name}
                        </option>
                      ))}
                  </select>
                </label>
                <label>
                  Consent voice connection
                  <select
                    value={voice}
                    onChange={(e) => setVoice(e.target.value)}
                  >
                    <option value="">Choose probed voice connection</option>
                    {query.data.voice_connections.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Consent storage connection
                  <select
                    value={storage}
                    onChange={(e) => setStorage(e.target.value)}
                  >
                    <option value="">No storage selected</option>
                    {query.data.storage_connections.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Participant decision
                  <select
                    value={decision}
                    onChange={(e) => {
                      setDecision(
                        e.target.value === "GRANTED" ? "GRANTED" : "DECLINED",
                      );
                      setScopes(emptyCaptureScopes());
                    }}
                  >
                    <option value="DECLINED">
                      Declined — all processing off
                    </option>
                    <option value="GRANTED">
                      Explicitly permit selected purposes
                    </option>
                  </select>
                </label>
                {scopeKeys.map((key) => (
                  <label key={key}>
                    <input
                      type="checkbox"
                      checked={scopes[key] === true}
                      disabled={decision !== "GRANTED"}
                      onChange={(e) =>
                        setScopes({ ...scopes, [key]: e.target.checked })
                      }
                    />
                    {scopeLabels[key]}
                  </label>
                ))}
                <label>
                  Processing purpose
                  <input
                    value={purpose}
                    maxLength={500}
                    onChange={(e) => setPurpose(e.target.value)}
                  />
                </label>
                <label>
                  Notice reviewed
                  <textarea
                    value={notice}
                    maxLength={1000}
                    onChange={(e) => setNotice(e.target.value)}
                  />
                </label>
                <label>
                  Jurisdiction
                  <input
                    value={jurisdiction}
                    maxLength={100}
                    onChange={(e) => setJurisdiction(e.target.value)}
                  />
                </label>
                <label>
                  Consent evidence
                  <textarea
                    value={evidence}
                    maxLength={1000}
                    onChange={(e) => setEvidence(e.target.value)}
                  />
                </label>
                <label>
                  Consent expiry (your local time)
                  <input
                    type="datetime-local"
                    value={expires}
                    onChange={(e) => setExpires(e.target.value)}
                  />
                </label>
              </fieldset>
            )}
            {query.data.can_record && (
              <>
                <label>
                  <input
                    type="checkbox"
                    checked={reviewed}
                    disabled={busy}
                    onChange={(e) => setReviewed(e.target.checked)}
                  />
                  I reviewed this synthetic participant, notice and each
                  separate processing choice.
                </label>
                <button
                  disabled={
                    !reviewed ||
                    busy ||
                    !customer ||
                    !voice ||
                    !expires ||
                    purpose.length < 10 ||
                    notice.length < 10 ||
                    jurisdiction.length < 2 ||
                    evidence.length < 10
                  }
                  onClick={save}
                >
                  Save synthetic consent decision
                </button>
              </>
            )}
            <details>
              <summary>
                Saved consent history ({query.data.items.length})
              </summary>
              {query.data.items.map((c) => (
                <div key={c.id}>
                  <ConsentRecord consent={c} />
                  {workspace.role === "owner" && !c.revoked_at && (
                    <WithdrawConsent
                      consent={c}
                      workspace={workspace.id}
                      csrf={csrfToken}
                      refresh={refresh}
                    />
                  )}
                </div>
              ))}
            </details>
          </>
        )
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
function WithdrawConsent({
  consent,
  workspace,
  csrf,
  refresh,
}: {
  consent: VoiceConsentView;
  workspace: string;
  csrf: string;
  refresh: () => Promise<unknown>;
}) {
  const [reason, setReason] = useState(""),
    [reviewed, setReviewed] = useState<VoiceConsentView | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  async function withdraw() {
    if (!reviewed || reviewed.version !== consent.version || busy) return;
    setBusy(true);
    try {
      await voicePrivacyApi.revoke(
        workspace,
        reviewed.id,
        {
          request_id: crypto.randomUUID(),
          expected_version: reviewed.version,
          reason,
        },
        csrf,
      );
      setReviewed(null);
      await refresh();
    } catch (e) {
      setError(message(e));
      setReviewed(null);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div>
      <label>
        Withdrawal reason
        <textarea
          value={reason}
          onChange={(e) => {
            setReason(e.target.value);
            setReviewed(null);
          }}
        />
      </label>
      <label>
        <input
          type="checkbox"
          checked={!!reviewed && reviewed.version === consent.version}
          onChange={(e) =>
            setReviewed(e.target.checked ? structuredClone(consent) : null)
          }
        />
        Withdraw all saved purposes and revoke private access now
      </label>
      <button
        disabled={!reviewed || reason.length < 10 || busy}
        onClick={withdraw}
      >
        Withdraw consent
      </button>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
function PrivateTranscript({
  workspace,
  id,
}: {
  workspace: string;
  id: string;
}) {
  const [turns, setTurns] = useState<TranscriptTurn[] | null>(null),
    [error, setError] = useState("");
  async function open() {
    setTurns(null);
    setError("");
    try {
      setTurns(await voicePrivacyApi.transcript(workspace, id));
    } catch (e) {
      setError(message(e));
    }
  }
  return (
    <div>
      <button onClick={open}>Read permitted private transcript</button>
      {turns && (
        <section aria-label="Private synthetic transcript">
          <p>FAKE fixed fixture; not customer speech.</p>
          {turns.map((t, i) => (
            <p key={i}>
              <strong>{t.speaker}</strong> · {t.offset_ms} ms · {t.text}
            </p>
          ))}
          <button onClick={() => setTurns(null)}>Close transcript</button>
        </section>
      )}
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
export function VoicePrivacyDetail({
  session,
  workspace,
  csrf,
  refresh,
}: {
  session: VoiceSessionView;
  workspace: WorkspaceView;
  csrf: string;
  refresh: () => Promise<unknown>;
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  async function reconcile() {
    if (!session.can_reconcile || busy) return;
    setBusy(true);
    setError("");
    try {
      await voicePrivacyApi.reconcile(workspace.id, session.id, csrf);
      await refresh();
    } catch (e) {
      setError(message(e));
      await refresh();
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="panel"
      aria-label="Post-call history and privacy"
      style={{ minWidth: 0, overflowWrap: "anywhere" }}
    >
      <h3>Post-call history and privacy</h3>
      <p>
        Reconciliation preserves the completed call and permitted originals
        once. It does not repeat committed tools or enable external analysis.
      </p>
      {session.communication_id && (
        <Link
          href={`/communications/${session.communication_id}?workspace=${workspace.id}`}
        >
          Open canonical call history
        </Link>
      )}
      {session.consent ? (
        <>
          <ConsentRecord consent={session.consent} />
          {workspace.role === "owner" && !session.consent.revoked_at && (
            <WithdrawConsent
              consent={session.consent}
              workspace={workspace.id}
              csrf={csrf}
              refresh={refresh}
            />
          )}
        </>
      ) : (
        <p>
          No processing consent recorded. Post-call history contains metadata
          only.
        </p>
      )}
      <p>{session.reconciliation_reasons?.join(" · ")}</p>
      <button disabled={!session.can_reconcile || busy} onClick={reconcile}>
        Reconcile completed call history
      </button>
      {error && <p role="alert">{error}</p>}
      {session.artifacts?.map((artifact) => (
        <section
          key={artifact.id}
          aria-label={`Private original ${artifact.id}`}
        >
          <p>
            {artifact.content_type} · {artifact.storage_state} ·{" "}
            {artifact.size_bytes} bytes · FAKE
          </p>
          <p>
            Captured {stamp(artifact.captured_at)} · Expires{" "}
            {stamp(artifact.expires_at)} · SHA256 {artifact.sha256}
          </p>
          {artifact.content_type === "application/json" &&
            session.transcript_available && (
              <PrivateTranscript
                key={`${artifact.id}-${session.consent?.version}`}
                workspace={workspace.id}
                id={artifact.id}
              />
            )}{" "}
          {artifact.content_type.startsWith("audio/") &&
            session.recording_available && (
              <>
                <p>FAKE silent audio fixture; no customer audio.</p>
                <audio
                  style={{ maxWidth: "100%" }}
                  controls
                  preload="none"
                  src={`${privatePath(workspace.id, artifact.id)}/playback`}
                >
                  Private audio playback unavailable.
                </audio>
              </>
            )}
          <ArtifactPrivacy
            workspace={workspace.id}
            id={artifact.id}
            csrf={csrf}
            changed={refresh}
          />
        </section>
      ))}
    </section>
  );
}
function ArtifactPrivacy({
  workspace,
  id,
  csrf,
  changed,
}: {
  workspace: string;
  id: string;
  csrf: string;
  changed: () => Promise<unknown>;
}) {
  const query = useQuery({
    queryKey: ["artifact-privacy", workspace, id],
    queryFn: ({ signal }) => voicePrivacyApi.privacy(workspace, id, signal),
    retry: false,
    refetchInterval: 4000,
  });
  const [kind, setKind] = useState<PrivacyCommand["kind"]>("DELETE_REQUEST"),
    [reason, setReason] = useState(""),
    [authority, setAuthority] = useState(""),
    [evidence, setEvidence] = useState(""),
    [date, setDate] = useState(""),
    [parent, setParent] = useState("");
  const [reviewed, setReviewed] = useState<ArtifactPrivacyView | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const current = query.data;
  const exact =
    !!current &&
    !!reviewed &&
    samePrivacyReview(reviewed, current) &&
    !query.isError &&
    !query.isFetching;
  const legal = kind === "VERIFIED_HOLD" || kind === "RELEASE_HOLD",
    decision = kind === "DELETE_APPROVED" || kind === "DELETE_DENIED";
  async function save() {
    if (!exact || !reviewed || busy) return;
    setBusy(true);
    setError("");
    try {
      await voicePrivacyApi.disposition(
        workspace,
        id,
        {
          request_id: crypto.randomUUID(),
          expected_version: reviewed.version,
          kind,
          reason,
          parent_id: decision ? parent : null,
          authority: legal ? authority : null,
          evidence: legal ? evidence : null,
          review_at: legal ? new Date(date).toISOString() : null,
        },
        csrf,
      );
      setReviewed(null);
      await query.refetch();
      await changed();
    } catch (e) {
      setError(message(e));
      setReviewed(null);
      await query.refetch();
    } finally {
      setBusy(false);
    }
  }
  if (query.isError)
    return (
      <p role="alert">
        Privacy history unavailable; controls disabled.{" "}
        <button onClick={() => query.refetch()}>Refresh privacy history</button>
      </p>
    );
  if (!current) return <p>Loading privacy history…</p>;
  return (
    <details>
      <summary>Retention, deletion and legal review</summary>
      <p>
        Legal hold: {current.legal_hold_state} · Purge {current.purge_status} ·
        Due {stamp(current.purge_due_at)} · Provider{" "}
        {current.provider_purge_status}
      </p>
      <p>
        {current.failure_code} {current.provider_purge_receipt}
      </p>
      <ol>
        {current.events.map((event) => (
          <li key={event.id}>
            {event.command.kind} · {stamp(event.created_at)} · {event.actor_id}{" "}
            · {event.command.reason}
            {event.command.authority && (
              <p>
                Authority: {event.command.authority} · Evidence:{" "}
                {event.command.evidence} · Review{" "}
                {stamp(event.command.review_at)}
              </p>
            )}
          </li>
        ))}
      </ol>
      {current.can_request_deletion && (
        <>
          <fieldset disabled={busy} onChange={() => setReviewed(null)}>
            <legend>Explicit privacy disposition</legend>
            <label>
              Privacy action
              <select
                value={kind}
                onChange={(e) =>
                  setKind(e.target.value as PrivacyCommand["kind"])
                }
              >
                <option value="DELETE_REQUEST">Request deletion</option>
                <option value="QUARANTINE">
                  Quarantine pending legal review
                </option>
                {current.can_review && (
                  <>
                    <option value="DELETE_APPROVED">
                      Approve exact deletion request
                    </option>
                    <option value="DELETE_DENIED">
                      Deny exact deletion request
                    </option>
                    <option value="VERIFIED_HOLD">
                      Record verified legal hold
                    </option>
                    <option value="RELEASE_HOLD">
                      Release reviewed legal hold
                    </option>
                  </>
                )}
              </select>
            </label>
            {decision && (
              <label>
                Saved deletion request
                <select
                  value={parent}
                  onChange={(e) => setParent(e.target.value)}
                >
                  <option value="">Choose original request</option>
                  {current.events
                    .filter((event) => event.command.kind === "DELETE_REQUEST")
                    .map((event) => (
                      <option key={event.id} value={event.id}>
                        {event.id} · {event.command.reason}
                      </option>
                    ))}
                </select>
              </label>
            )}
            <label>
              Privacy reason
              <textarea
                value={reason}
                maxLength={1000}
                onChange={(e) => setReason(e.target.value)}
              />
            </label>
            {legal && (
              <>
                <label>
                  Legal authority
                  <textarea
                    value={authority}
                    onChange={(e) => setAuthority(e.target.value)}
                  />
                </label>
                <label>
                  Legal review evidence
                  <textarea
                    value={evidence}
                    onChange={(e) => setEvidence(e.target.value)}
                  />
                </label>
                <label>
                  Next legal review (your local time)
                  <input
                    type="datetime-local"
                    value={date}
                    onChange={(e) => setDate(e.target.value)}
                  />
                </label>
              </>
            )}
          </fieldset>
          <label>
            <input
              type="checkbox"
              checked={exact}
              disabled={busy || query.isFetching}
              onChange={(e) =>
                setReviewed(e.target.checked ? structuredClone(current) : null)
              }
            />
            I reviewed this original, saved history and disposition at version{" "}
            {current.version}.
          </label>
          <button
            disabled={
              !exact ||
              busy ||
              reason.length < 10 ||
              (decision && !parent) ||
              (legal &&
                (!date || authority.length < 10 || evidence.length < 10))
            }
            onClick={save}
          >
            Save privacy disposition
          </button>
        </>
      )}
      {error && <p role="alert">{error}</p>}
    </details>
  );
}
