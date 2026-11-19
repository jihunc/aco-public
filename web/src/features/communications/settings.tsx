"use client";
import { useState, type FormEvent } from "react";
import { InvoiceRef } from "@/lib/invoice-numbers";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ErrorState, stamp } from "@/features/receivables/shared";
import type { WorkspaceView } from "@/lib/generated-api";
import { SyntheticInboundPanel } from "./ingress";
import { ProviderEmailSettings } from "./provider-settings";
import { CompanyConnections } from "./connections";
import { ActionFailures } from "./action-failures";
import { communicationMutation, communicationsApi, type EmailExecution } from "./api";

export function EmailExecutionPanel({ workspace, csrfToken, receiptId }: { workspace: WorkspaceView; csrfToken: string; receiptId?: string }) {
  return <><CompanyConnections key={workspace.id} workspace={workspace} csrfToken={csrfToken} /><SyntheticInboundPanel key={`${workspace.id}:${receiptId ?? ""}`} workspace={workspace} csrfToken={csrfToken} receiptId={receiptId} /><EmailOutboundPanel workspace={workspace} csrfToken={csrfToken} /></>;
}
function EmailOutboundPanel({ workspace, csrfToken }: { workspace: WorkspaceView; csrfToken: string }) {
  const cache = useQueryClient();
  const execution = useQuery({ queryKey: ["email-execution", workspace.id], queryFn: ({ signal }) => communicationsApi.execution(workspace.id, signal), retry: false });
  const actions = useQuery({ queryKey: ["scheduled-actions", workspace.id, "queued"], queryFn: ({ signal }) => communicationsApi.actions(workspace.id, "queued", signal), retry: false });
  const unknown = useQuery({ queryKey: ["scheduled-actions", workspace.id, "unknown_outcome"], queryFn: ({ signal }) => communicationsApi.actions(workspace.id, "unknown_outcome", signal), retry: false });
  const [sender, setSender] = useState("");
  const [destinations, setDestinations] = useState("");
  const [cap, setCap] = useState(10);
  const [evidence, setEvidence] = useState("");
  const [reviewedCapture, setReviewedCapture] = useState(false);
  const [reviewedAddresses, setReviewedAddresses] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [outboundNotice, setOutboundNotice] = useState("");
  const [error, setError] = useState("");
  const [asOf] = useState(() => Date.now());
  const owner = workspace.permissions.includes("settings");
  const canPause = workspace.outbound_paused ? workspace.permissions.includes("resume") : workspace.permissions.includes("pause");
  const canRun = workspace.permissions.includes("demo_run_due");
  async function action(fn: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await fn(); await Promise.all([
      cache.invalidateQueries({ queryKey: ["email-execution", workspace.id] }),
      cache.invalidateQueries({ queryKey: ["scheduled-actions", workspace.id] }),
      cache.invalidateQueries({ queryKey: ["me"] }),
    ]); } catch (reason) { setError(reason instanceof Error ? reason.message : "The email action failed."); }
    finally { setBusy(false); }
  }
  async function review(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!reviewedCapture || !reviewedAddresses) return;
    await action(async () => {
      await communicationMutation<EmailExecution>(workspace.id, "/email-execution", {
        expected_activation_id: execution.data?.activation?.id ?? null,
        sender: sender.trim(), destinations: destinations.split(/[\s,;]+/).map(v => v.trim()).filter(Boolean),
        request_cap: cap, synthetic_capture_reviewed: true, sender_and_destinations_reviewed: true, evidence: evidence.trim(),
      }, csrfToken, "PUT");
      setNotice("Synthetic email activation reviewed and saved.");
    });
  }
  async function pause(paused: boolean) {
    await action(async () => {
      await communicationMutation(workspace.id, "/automation/pause", { scope: "tenant", paused }, csrfToken);
      const message = paused ? "Workspace outbound paused." : "Workspace outbound resume requested; current guards still apply.";
      setOutboundNotice(message);
    });
  }
  async function runDue(id: string, version: number) {
    await action(async () => {
      await communicationMutation(workspace.id, "/demo/reminders/run-due", { action_id: id, expected_version: version }, csrfToken);
      setNotice("Due reminder accepted into the worker queue. Check delivery evidence in Conversations.");
    });
  }
  async function reconcile(id: string) {
    await action(async () => {
      const result = await communicationMutation<{ state: string; resubmitted: false }>(workspace.id, `/scheduled-actions/${id}/reconcile-delivery`, {}, csrfToken);
      setNotice(`Delivery reconciliation: ${result.state}. No message was resubmitted.`);
    });
  }
  if (execution.isPending) return <p role="status">Loading email execution state…</p>;
  if (execution.isError) return <ErrorState label="Email execution unavailable" error={execution.error} retry={() => execution.refetch()} />;
  const activation = execution.data.activation;
  const due = actions.data?.filter(a => new Date(a.due_at).getTime() <= asOf) ?? [];
  return <>
    <section className="panel"><h2>Email execution</h2>
      <p>{execution.data.system_enabled ? "Email execution is enabled, subject to reviewed scope and current guards." : "System email execution is disabled."}</p>
      <p>Current channel: {execution.data.mode === "LIVE" ? "Live provider" : execution.data.mode === "MOCKED_HTTP" ? "Mocked HTTP provider" : execution.data.mode === "SYNTHETIC" ? "Synthetic demo" : "No activation"}.</p>
      <p>{activation?.current ? `Current reviewed activation until ${stamp(activation.expires_at)}.` : "No current reviewed activation."}</p>
      {activation && <p>Reviewed sender {activation.sender}; {activation.destinations.length} permitted destination{activation.destinations.length === 1 ? "" : "s"}; cap {activation.request_cap}.</p>}
      <p className="muted">Provider acceptance is not delivery confirmation. Synthetic data and mocked transport are labeled separately from live provider execution.</p>
    </section>
    {owner && <ProviderEmailSettings key={workspace.id} workspace={workspace} csrfToken={csrfToken} />}
    {owner && execution.data.provider === "demo_email" && <section className="panel"><h2>Review synthetic sender and recipients</h2>
      <form onSubmit={review} className="task-form">
        <label>Sender email<input type="email" required value={sender} onChange={e => setSender(e.target.value)} /></label>
        <label>Permitted recipient emails<textarea required rows={3} value={destinations} onChange={e => setDestinations(e.target.value)} placeholder="One or more approved test inboxes" /></label>
        <label>Maximum requests<input type="number" min={1} max={100} value={cap} onChange={e => setCap(Number(e.target.value))} /></label>
        <label>Review evidence<textarea required minLength={1} maxLength={1000} rows={3} value={evidence} onChange={e => setEvidence(e.target.value)} /></label>
        <label><input type="checkbox" checked={reviewedCapture} onChange={e => setReviewedCapture(e.target.checked)} /> I reviewed synthetic source capture.</label>
        <label><input type="checkbox" checked={reviewedAddresses} onChange={e => setReviewedAddresses(e.target.checked)} /> I reviewed this sender and every destination.</label>
        <button type="submit" disabled={busy || !reviewedCapture || !reviewedAddresses}>Save synthetic activation</button>
      </form>
    </section>}
    <section className="panel"><h2>Outbound control</h2>
      <p>Workspace outbound is {workspace.outbound_paused ? "paused" : "enabled subject to current policy and source guards"}.</p>
      {canPause && <button type="button" disabled={busy} onClick={() => void pause(!workspace.outbound_paused)}>{workspace.outbound_paused ? "Request workspace resume" : "Pause outbound"}</button>}
      {outboundNotice && <p role="status" className="field-hint">{outboundNotice}</p>}
    </section>
    <section className="panel"><h2>Due reminders</h2>
      {actions.isPending ? <p role="status">Loading scheduled actions…</p> : actions.isError ? <ErrorState label="Scheduled actions unavailable" error={actions.error} retry={() => actions.refetch()} /> :
        due.length === 0 ? <p>No queued due policy reminders. Preview or activate a policy to schedule work.</p> :
        <ul>{due.map(a => <li key={a.id}><InvoiceRef id={a.invoice_id} /> · Due {stamp(a.due_at)}
          {canRun && <button type="button" disabled={busy || workspace.outbound_paused || !activation?.current || !execution.data.system_enabled}
            onClick={() => void runDue(a.id, a.version)}>Queue due reminder</button>}</li>)}</ul>}
      <p className="muted">Queued work runs in the worker after server-side authorization. Opening this page does not send mail.</p>
    </section>
    <section className="panel"><h2>Unknown delivery outcomes</h2>
      {unknown.isPending ? <p role="status">Loading uncertain actions…</p> : unknown.isError ? <ErrorState label="Delivery review unavailable" error={unknown.error} retry={() => unknown.refetch()} /> :
        unknown.data.length === 0 ? <p>No unresolved delivery outcomes.</p> : <ul>{unknown.data.map(a => <li key={a.id}><InvoiceRef id={a.invoice_id} /> · {a.last_error ?? "Outcome uncertain"}
          {workspace.permissions.includes("investigate") && <button type="button" disabled={busy} onClick={() => void reconcile(a.id)}>Reconcile delivery evidence</button>}</li>)}</ul>}
      <p className="muted">Reconciliation inspects stored provider evidence. It does not resend an uncertain message.</p>
    </section>
    <ActionFailures workspace={workspace.id} />
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </>;
}
