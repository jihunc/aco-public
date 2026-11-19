import type { UsageSummary } from "@/lib/generated-api";

/** Provider fees retain all twelve decimal places; invoice Money formatting is separate. */
export function UsageDetails({ view }: { view: UsageSummary }) {
  return (
    <>
      <h2>Usage &amp; budgets</h2>
      <p>
        Currencies are separate; no FX conversion. Simulated fees are not actual
        vendor charges.
      </p>
      {view.notices.map((notice) => (
        <p className="notice" key={notice}>
          {notice}
        </p>
      ))}
      {view.currencies.length === 0 && (
        <p>
          No provider usage recorded for this workspace. Missing history is not
          free usage.
        </p>
      )}
      {view.currencies.map((row) => {
        const key = `${row.currency}-${row.synthetic ? "synthetic" : "observed"}`;
        return (
          <section
            key={key}
            aria-label={`${row.currency} ${row.synthetic ? "simulated" : "observed"} usage`}
          >
            <h3>
              {row.currency} ·{" "}
              {row.synthetic
                ? "Simulated provider fees"
                : "Observed provider fees"}
            </h3>
            <p>
              {row.completeness} · {row.unknown_count} unknown{" "}
              {row.unknown_count === 1 ? "charge" : "charges"}
            </p>
            {row.bundle_issue_count > 0 && (
              <p>Total unavailable: unresolved bundle overlap. Top-level known subtotal only.</p>
            )}
            <dl className="metric-grid">
              <div className="metric-card">
                <dt>Provider-reported fees</dt>
                <dd data-testid={`usage-reported-${key}`}>
                  {row.currency} {row.reported_amount}
                </dd>
              </div>
              <div className="metric-card">
                <dt>Rate-estimated fees</dt>
                <dd data-testid={`usage-estimated-${key}`}>
                  {row.currency} {row.estimated_amount}
                </dd>
              </div>
              <div className="metric-card">
                <dt>{row.bundle_issue_count > 0 ? "Top-level known subtotal" : "Known fees (reported + estimated)"}</dt>
                <dd data-testid={`usage-known-${key}`}>
                  {row.currency} {row.known_amount}
                </dd>
              </div>
              <div className="metric-card">
                <dt>Retained reservation exposure</dt>
                <dd data-testid={`usage-reserved-${key}`}>
                  {row.currency} {row.reserved_amount}
                </dd>
              </div>
              <div className="metric-card">
                <dt>Reconciled budget spend</dt>
                <dd>
                  {row.currency} {row.spent_amount}
                </dd>
              </div>
            </dl>
          </section>
        );
      })}
      {view.providers.map((row) => (
        <p
          key={`${row.provider_connection_id}-${row.currency}-${row.synthetic}`}
        >
          {row.provider_kind} · {row.provider} ·{" "}
          {row.synthetic ? "simulated" : "observed"} · {row.currency}: reported{" "}
          {row.reported_amount}, estimated {row.estimated_amount}, unknown{" "}
          {row.unknown_count} · {row.completeness} · {row.bundle_issue_count} unresolved component relationships
        </p>
      ))}
      {view.bundle_issues.map((issue) => (
        <section key={issue.event_id} aria-label="Unresolved bundle evidence">
          <p>{issue.reason} · event {issue.event_id} · parent {issue.parent_event_id ?? "missing"}</p>
          {issue.outside_filter && <p>Context outside selected time range</p>}
          <p>{issue.component}: {issue.currency} {issue.component_amount ?? "UNKNOWN"}</p>
          <p>{issue.parent_component}: {issue.parent_currency ?? "unavailable"} {issue.parent_amount ?? "UNKNOWN"}</p>
          <p>Component evidence is retained separately; overlapping amounts are not added.</p>
        </section>
      ))}
      <p>
        Reservations are bounds, not additional provider fees. Unknown outcomes
        retain exposure and disable automatic retry.
      </p>
      {view.limits.map((limit) => (
        <p
          key={limit.id}
        >{`${limit.state} · ${limit.dimension} · ${limit.currency} ${limit.amount}`}</p>
      ))}
      <p className="muted">{view.history_coverage}</p>
    </>
  );
}
