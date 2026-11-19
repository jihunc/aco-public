"use client";
import { useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { ProviderEmailConfiguration, WorkspaceView } from "@/lib/generated-api";
import { ErrorState, stamp } from "@/features/receivables/shared";
import { communicationMutation, communicationsApi } from "./api";
import { captureProviderReview, providerReviewCommand, providerReviewIsCurrent } from "./provider-review-api";

export function ProviderEmailSettings({ workspace, csrfToken }: { workspace: WorkspaceView; csrfToken: string }) {
  const cache = useQueryClient();
  const query = useQuery({ queryKey: ["email-provider-review", workspace.id],
    queryFn: ({ signal }) => communicationsApi.providerReview(workspace.id, signal), retry: false });
  const [reviewed, setReviewed] = useState<ProviderEmailConfiguration | null>(null);
  const [evidence, setEvidence] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  async function refresh() {
    setReviewed(null); setAcknowledged(false); setError(""); setNotice("");
    await query.refetch();
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!reviewed || !acknowledged || busy || !providerReviewIsCurrent(reviewed, query.data?.configurations ?? [])) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await communicationMutation(workspace.id, "/email-execution/provider", providerReviewCommand(reviewed, evidence), csrfToken, "PUT");
      setNotice("Company email provider activation saved. Due actions remain subject to current policies, approvals and pause controls.");
      setReviewed(null); setAcknowledged(false); setEvidence("");
      await Promise.all([cache.invalidateQueries({ queryKey: ["email-execution", workspace.id] }), query.refetch()]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Provider review could not be saved. Refresh and review the current configuration.");
      // A stale or uncertain command is never refreshed and resubmitted automatically.
      setReviewed(null); setAcknowledged(false);
    } finally { setBusy(false); }
  }
  return <section className="panel" aria-label="Company email provider">
    <h2>Company email provider</h2>
    <p>Review the configured sender and permitted recipients before enabling provider-backed reminders. Viewing this page and saving a review do not send mail.</p>
    <button type="button" disabled={busy || query.isFetching} onClick={() => void refresh()}>Refresh provider configuration</button>
    {query.isPending ? <p role="status">Loading configured email provider…</p> : query.isError ?
      <ErrorState label="Email provider configuration unavailable" error={query.error} retry={() => void refresh()} /> :
      !query.data.available ? <p>No current provider configuration is available for review. An authorized operator must configure the company connection and bounded test scope.</p> :
      <ul>{query.data.configurations.map(configuration => <li key={configuration.connection_id}>
        <p><strong>Resend · {configuration.evidence_mode === "LIVE" ? "Live provider" : "Mocked HTTP provider"}</strong></p>
        <p>{configuration.mode === "OUTBOUND_TEST_ONLY" ? "Outbound test only; receiving is unavailable." : "Reviewed sending and inbound capture scope."}</p>
        <p>Sender: {configuration.sender}</p>
        <p>Permitted recipients: {configuration.destinations.join(", ")}</p>
        <p>Maximum sent messages: {configuration.request_cap} · Expires {stamp(configuration.expires_at)}</p>
        {configuration.max_requests != null && <p>API request ceiling: {configuration.max_requests} · Retained spending ceiling: USD {configuration.max_usd ?? "unavailable"}.</p>}
        <button type="button" disabled={busy} onClick={() => { setReviewed(captureProviderReview(configuration)); setAcknowledged(false); setEvidence(""); setError(""); setNotice(""); }}>Review this provider configuration</button>
      </li>)}</ul>}
    {reviewed && <form className="task-form" aria-label="Captured provider email review" onSubmit={save}>
      <h3>Captured company email review</h3>
      <p>{reviewed.evidence_mode === "LIVE" ? "This activation permits genuine provider requests within the displayed scope." : "This configuration uses mocked provider HTTP; it is not live verification."}</p>
      <p>Sender {reviewed.sender}; recipients {reviewed.destinations.join(", ")}; maximum {reviewed.request_cap} sent messages.</p>
      <label>Provider review evidence<textarea required minLength={1} maxLength={1000} value={evidence} onChange={event => setEvidence(event.target.value)} /></label>
      <label><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} /> I reviewed this exact sender, all recipients, capture scope and limits.</label>
      {!providerReviewIsCurrent(reviewed, query.data?.configurations ?? []) && <p role="alert">The configuration changed. Refresh and review it again.</p>}
      <div className="button-row"><button type="submit" disabled={busy || !acknowledged || !evidence.trim() || !providerReviewIsCurrent(reviewed, query.data?.configurations ?? [])}>Save company provider activation</button>
        <button type="button" disabled={busy} onClick={() => { setReviewed(null); setAcknowledged(false); }}>Cancel provider review</button></div>
    </form>}
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </section>;
}
