"use client";
import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { readApi } from "@/lib/api";
import type { InvoiceAuditItem, InvoiceAuditPage } from "@/lib/generated-api";
import { reviewApi } from "@/features/review/api";
import { ErrorState, stamp } from "./shared";

type Props = { workspace: string; invoiceId: string };
// Audit rows keep seconds: one import can record several reconciliations within a minute.
export const auditStamp = (value: string | null | undefined) =>
  value
    ? new Intl.DateTimeFormat("en-AU", { dateStyle: "medium", timeStyle: "medium", timeZone: "UTC" }).format(new Date(value)) + " UTC"
    : "Unavailable";
const workspaceLink = (workspace: string, path: string) => `${path}?workspace=${encodeURIComponent(workspace)}`;

export function InvoiceTasks({ workspace, invoiceId }: Props) {
  const query = useQuery({
    queryKey: ["invoice-tasks", workspace, invoiceId],
    queryFn: ({ signal }) => reviewApi.tasks(workspace, invoiceId, signal),
    retry: false,
  });
  return <section className="panel" aria-label="Related invoice tasks">
    <h3>Related tasks</h3>
    <p>Current tasks explicitly linked to this invoice. Task completion does not resolve a case or change accounting.</p>
    {query.isPending ? <p role="status">Loading invoice tasks…</p> : query.isError ?
      <ErrorState label="Invoice tasks unavailable" error={query.error} retry={() => query.refetch()} /> :
      query.data.items.length === 0 ? <p>No current tasks are linked to this invoice. Deleted task history may remain in the audit below.</p> :
      <ul>{query.data.items.map(task => <li key={task.id}>
        <Link href={workspaceLink(workspace, `/tasks/${encodeURIComponent(task.id)}`)}>{task.title}</Link>
        <p>{task.status.replaceAll("_", " ")} · {task.origin === "generated" ? "Generated task" : "Human task"}{task.due_at ? ` · Due ${stamp(task.due_at)}` : " · No due date"}{task.deadline_locked ? " · Deadline locked" : ""}</p>
      </li>)}</ul>}
    <button type="button" disabled={query.isFetching} onClick={() => void query.refetch()}>Refresh related tasks</button>
  </section>;
}

function targetPath(item: InvoiceAuditItem): string | null {
  if (!item.target_available) return null;
  const id = encodeURIComponent(item.target_id);
  switch (item.target_kind) {
    case "task": return `/tasks/${id}`;
    case "case": return `/cases/${id}`;
    case "promise": return `/promises/${id}`;
    case "communication": return `/communications/${id}`;
    case "invoice": return `/receivables/${id}`;
    default: return null;
  }
}

export function InvoiceAudit({ workspace, invoiceId }: Props) {
  const [before, setBefore] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["invoice-audit", workspace, invoiceId, before],
    queryFn: ({ signal }) => readApi<InvoiceAuditPage>(
      `/api/workspaces/${encodeURIComponent(workspace)}/receivables/${encodeURIComponent(invoiceId)}/audit?limit=20${before ? `&before_id=${encodeURIComponent(before)}` : ""}`, signal),
    retry: false,
  });
  return <section className="panel" aria-label="Invoice business audit">
    <h3>Invoice audit</h3>
    <p>This finite invoice-linked business audit lists retained accounting reconciliation, task, case, promise, communication, approval, hold and action events. It is not the complete workspace audit log. Missing historic events are not reconstructed; private event payloads are excluded.</p>
    {query.isPending ? <p role="status">Loading invoice audit…</p> : query.isError ?
      <ErrorState label="Invoice audit unavailable" error={query.error} retry={() => query.refetch()} /> : <>
        {query.data.items.length === 0 ? <p>No retained events in this audit scope for this invoice.</p> : <ol>{query.data.items.map(item => {
          const path = targetPath(item);
          return <li key={item.id} style={{ overflowWrap: "anywhere" }}>
            <strong>{item.action}</strong> · {auditStamp(item.created_at)}
            <p>{item.actor_kind === "service" ? "Service actor" : "Human actor"}: {item.actor_id}</p>
            <p>{item.target_kind}: {path ? <Link href={workspaceLink(workspace, path)}>{item.target_id}</Link> : item.target_id}{!item.target_available ? " · Deleted target; retained history" : ""}</p>
            <details><summary>Audit provenance</summary><p>Event {item.id}</p><p>Correlation {item.correlation_id}</p></details>
          </li>;
        })}</ol>}
        <button type="button" disabled={query.isFetching || !query.data.next_before_id} onClick={() => setBefore(query.data.next_before_id)}>Older activity</button>
      </>}
    {before && <button type="button" onClick={() => setBefore(null)}>Newest activity</button>}
    <button type="button" disabled={query.isFetching} onClick={() => void query.refetch()}>Refresh audit</button>
  </section>;
}
