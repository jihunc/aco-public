"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { readApi } from "@/lib/api";
import type { WorkloadView } from "@/lib/generated-api";
import { ErrorState, stamp } from "./shared";

export function WorkloadSummary({
  workspace,
  href,
}: {
  workspace: string;
  href: (path: string) => string;
}) {
  const query = useQuery({
    queryKey: ["overview-workload", workspace],
    queryFn: ({ signal }) =>
      readApi<WorkloadView>(
        `/api/workspaces/${encodeURIComponent(workspace)}/overview/workload`,
        signal,
      ),
    retry: false,
  });
  if (query.isPending)
    return (
      <section className="panel">
        <p role="status">Loading company workload…</p>
      </section>
    );
  if (query.isError)
    return (
      <ErrorState
        label="Company workload unavailable"
        error={query.error}
        retry={() => query.refetch()}
      />
    );
  const view = query.data;
  const counts: [string, number][] = [
    ["Open tasks", view.open_tasks],
    ["Pending approvals", view.pending_approvals],
    ["Unresolved cases", view.unresolved_cases],
    ["Active promises", view.active_promises],
    ["Promises needing confirmation", view.unconfirmed_promises],
    ["Missed promises", view.missed_promises],
    ["Active investigations", view.active_investigations],
    ["Completed investigations", view.completed_investigations],
    ["Failed or partial investigations", view.failed_investigations],
    ["Stopped or uncertain actions", view.stopped_actions],
  ];
  return (
    <section className="panel" aria-label="Company workload">
      <div className="section-heading">
        <h2>Company workload</h2>
        <button
          type="button"
          disabled={query.isFetching}
          onClick={() => void query.refetch()}
        >
          Refresh workload
        </button>
      </div>
      <p>
        Current saved company records · observed {stamp(view.as_of_at)}. Counts
        span currencies and are separate from cash and successfully resolved
        cases.
      </p>
      <dl className="metric-grid">
        {counts.map(([label, count]) => (
          <div className="metric-card" key={label}>
            <dt>{label}</dt>
            <dd>{count}</dd>
          </div>
        ))}
      </dl>
      <p>
        {view.completed_live_investigations} completed with a real model;{" "}
        {view.completed_investigations - view.completed_live_investigations}{" "}
        with a simulated model. Inspect run outcomes for actual actions and
        limitations.
      </p>
      <p>
        Outbound is{" "}
        {view.outbound_paused
          ? "paused"
          : "subject to current execution guards"}
        . Automation is{" "}
        {view.automation_enabled
          ? "enabled subject to current guards"
          : "disabled"}
        . A pending approval count does not establish current action
        eligibility.
      </p>
      <p>
        <Link href={href("/review")}>Review tasks and approvals</Link> ·{" "}
        <Link href={href("/agents")}>Investigations and saved outcomes</Link> ·{" "}
        <Link href={href("/settings/email")}>
          Connections and stopped action evidence
        </Link>
      </p>
      <h3>Accounting source state</h3>
      {view.accounting_sources.length === 0 ? (
        <p>No accounting source configured.</p>
      ) : (
        <ul>
          {view.accounting_sources.map((source) => (
            <li key={source.connection_id}>
              {source.provider} · {source.status} ·{" "}
              {source.imported_invoice_count} imported invoices ·{" "}
              {source.oldest_import_observation_at
                ? `Oldest import observation ${stamp(source.oldest_import_observation_at)}`
                : "Observation time unavailable"}
            </li>
          ))}
        </ul>
      )}
      <p className="muted">
        Connection state and retained import times are not a fresh provider
        probe. Financial freshness is shown with the balances above.
      </p>
    </section>
  );
}
