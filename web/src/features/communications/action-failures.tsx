"use client";

import Link from "next/link";
import { InvoiceRef } from "@/lib/invoice-numbers";
import { useQuery } from "@tanstack/react-query";
import { readApi } from "@/lib/api";
import type { ActionView } from "@/lib/generated-api";
import { ErrorState, stamp } from "@/features/receivables/shared";

type StoppedStatus = "failed" | "dead_letter";

export function readActionFailures(workspace: string, status: StoppedStatus, signal?: AbortSignal) {
  return readApi<ActionView[]>(
    `/api/workspaces/${encodeURIComponent(workspace)}/scheduled-actions?status=${status}`,
    signal,
  );
}

function FailureGroup({ workspace, status }: { workspace: string; status: StoppedStatus }) {
  const query = useQuery({
    queryKey: ["scheduled-actions", workspace, status],
    queryFn: ({ signal }) => readActionFailures(workspace, status, signal),
    retry: false,
  });
  const label = status === "dead_letter" ? "Exhausted retries" : "Failed actions";
  return <section aria-label={label}>
    <h3>{label}</h3>
    {query.isPending ? <p role="status">Loading stopped actions…</p> : query.isError ?
      <ErrorState label={`${label} unavailable`} error={query.error} retry={() => query.refetch()} /> :
      query.data.length === 0 ? <p>No {label.toLowerCase()}.</p> : <ul>{query.data.map(action =>
        <li key={action.id}>
          <Link href={`/receivables/${encodeURIComponent(action.invoice_id)}?workspace=${encodeURIComponent(workspace)}`}><InvoiceRef id={action.invoice_id} /></Link>
          <p>{action.kind} · {action.status} · Attempts: {action.attempts}</p>
          <p>{action.last_error ?? "No failure reason was recorded."}</p>
          <p>Scheduled {stamp(action.due_at)}</p>
          <details><summary>Execution evidence</summary>
            {action.executions.length === 0 ? <p>No execution attempt was recorded.</p> : <ul>{action.executions.map(attempt =>
              <li key={attempt.attempt}>Attempt {attempt.attempt}: {attempt.status}; started {stamp(attempt.started_at)};
                {attempt.finished_at ? ` finished ${stamp(attempt.finished_at)};` : " completion unconfirmed;"}
                {attempt.retry_delay_seconds !== null && ` recorded retry wait ${attempt.retry_delay_seconds} seconds;`}
                {attempt.provider_operation_id && ` provider operation ${attempt.provider_operation_id}`}
              </li>)}</ul>}
          </details>
        </li>)}</ul>}
    <button type="button" disabled={query.isFetching} onClick={() => void query.refetch()}>Refresh {label.toLowerCase()}</button>
  </section>;
}

export function ActionFailures({ workspace }: { workspace: string }) {
  return <section className="panel">
    <h2>Stopped background actions</h2>
    <p>No automatic resend. Inspect the recorded failure and current source, connection and policy before scheduling new work. Uncertain outcomes remain in delivery review.</p>
    <p>Latest 100 actions per status, across background action kinds.</p>
    <Link href={`/settings/policies?workspace=${encodeURIComponent(workspace)}`}>Review current policies and scheduling</Link>
    <FailureGroup key={`${workspace}:failed`} workspace={workspace} status="failed" />
    <FailureGroup key={`${workspace}:dead_letter`} workspace={workspace} status="dead_letter" />
  </section>;
}
