"use client";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { ProviderConnectionView, WorkspaceView } from "@/lib/generated-api";
import { captureDisconnect, disconnectConnection, listConnections } from "./connection-api";

export function CompanyConnections({ workspace, csrfToken }: { workspace: WorkspaceView; csrfToken: string }) {
  const owner = workspace.permissions.includes("settings");
  const cache = useQueryClient();
  const query = useQuery({ queryKey: ["provider-connections", workspace.id],
    queryFn: ({ signal }) => listConnections(workspace.id, signal), enabled: owner, retry: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  if (!owner) return null;
  async function disconnect(row: ProviderConnectionView) {
    const captured = captureDisconnect(row);
    setBusy(true); setError(""); setNotice("");
    try {
      await disconnectConnection(workspace.id, captured, csrfToken);
      setNotice("Disconnected from ACO. History is retained. Provider credential revocation is unknown.");
      await Promise.all(["provider-connections", "email-execution", "email-provider-review", "ingress-setup", "scheduled-actions", "agent-runs", "agent-run", "voice-sessions", "voice-session"].map(
        key => cache.invalidateQueries({ queryKey: [key, workspace.id] }),
      ));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Connection could not be disconnected.");
      await query.refetch();
    } finally { setBusy(false); }
  }
  return <section className="panel" aria-labelledby="company-connections-title">
    <h2 id="company-connections-title">Company connections</h2>
    <p>Disconnecting stops new use of an email, LLM, or voice connection in ACO and preserves its history. An in-flight request may already have been submitted. Provider credentials are not revoked here, and an active provider call is not cancelled.</p>
    <button type="button" disabled={busy || query.isFetching} onClick={() => query.refetch()}>Refresh connections</button>
    {query.isPending && <p role="status">Loading connections…</p>}
    {query.isError && <p role="alert">{query.error.message}</p>}
    {query.data?.length === 0 && <p>No configured connections.</p>}
    {query.data && <ul>{query.data.map(row => <li key={row.id}>
      <h3>{row.provider} · {row.provider_type}</h3>
      <p>ACO status: {row.status} · Version {row.version}</p>
      <p>Connection {row.id}</p>
      {row.remote_revocation === "UNKNOWN" && <p>Provider credential revocation: unknown.</p>}
      {row.can_disconnect ? <button type="button" disabled={busy} onClick={() => disconnect(row)}>Disconnect {row.provider} from ACO</button>
        : <p>{row.disconnect_reasons.includes("ALREADY_DISCONNECTED") ? "Disconnected from ACO; history remains available." : "Lifecycle controls for this provider type are not available here."}</p>}
    </li>)}</ul>}
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </section>;
}
