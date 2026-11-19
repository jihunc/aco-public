"use client";
import Link from "next/link";
import { InvoiceRef } from "@/lib/invoice-numbers";
import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { formatMoney } from "@/lib/money";
import { ErrorState, stamp } from "@/features/receivables/shared";
import { CurrencyOverview } from "@/features/receivables/overview";
import { UsageDetails } from "@/features/usage/details";
import {
  impactApi,
  type CashGroup,
  type ActivityGroup,
  type ActivityRecord,
  type ImpactWindow,
} from "./api";
import "./impact.css";
import { CohortPanel } from "./cohorts";
import { CaseOutcomesPanel } from "./case-outcomes";
import { EconomicsPanel } from "./economics";

export function utcWindowInput(value?: string): string {
  if (!value || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? ""
    : parsed.toISOString().slice(0, 19);
}
const kindLabels = {
  agent_runs: "Agent run records",
  scheduled_actions: "Scheduled action records",
  cases: "Case records",
  usage_events: "New provider usage identities",
};
const issueLabels: Record<string, string> = {
  SOURCE_SCOPE_MISMATCH:
    "Allocation does not match its accounting source and invoice scope.",
  PARENT_NOT_VERIFIED_IN_SCOPE:
    "The linked original cash is unavailable or lacks a valid verified source basis.",
  PARENT_SCOPE_MISMATCH:
    "The linked cash belongs to a different invoice, source, currency or provenance.",
  PARENT_AFTER_ADJUSTMENT:
    "The linked cash occurs after its adjustment; source history requires review.",
  PARENT_OVERDRAWN:
    "Linked refunds and reversals exceed the original cash allocation.",
  FUTURE_OBSERVATION:
    "The source observation is after this report's as-of time.",
  MISSING_SOURCE_REVISION: "A source revision is missing.",
  UNSUPPORTED_CURRENCY_BASIS: "The source currency or exponent is unsupported.",
};
export function CashRecord({
  group,
  workspace,
}: {
  group: CashGroup;
  workspace: string;
}) {
  return (
    <section
      className="panel impact-cash"
      aria-label={`${group.currency} ${group.exponent} ${group.synthetic ? "synthetic" : "observed"} cash`}
    >
      <h3>
        {group.currency} ·{" "}
        {group.synthetic ? "Synthetic accounting" : "Observed accounting"} ·
        Exponent {group.exponent}
      </h3>
      <p className="badge">
        {group.completeness} · {group.freshness} source · Last source read{" "}
        {stamp(group.last_synced_at)}
      </p>
      {group.net_cash ? (
        <p>
          Known imported net cash{" "}
          <strong
            className="impact-net"
            data-testid={`impact-net-${group.currency}-${group.exponent}-${group.synthetic ? "synthetic" : "observed"}`}
          >
            {formatMoney(group.net_cash)}
          </strong>
        </p>
      ) : (
        <p className="notice">
          Net cash unavailable. The report cannot establish a supported net
          total for this group.
        </p>
      )}
      <dl className="detail-grid">
        <div>
          <dt>Supported cash allocations</dt>
          <dd>{group.cash ? formatMoney(group.cash) : "Unavailable"}</dd>
        </div>
        <div>
          <dt>Supported linked refunds/reversals</dt>
          <dd>
            {group.outflows ? formatMoney(group.outflows) : "Unavailable"}
          </dd>
        </div>
      </dl>
      <p>
        {group.allocation_count} included cash/adjustment allocations ·{" "}
        {group.non_cash_count} non-cash facts excluded · {group.excluded_count}{" "}
        invalid facts requiring review.
      </p>
      {group.issues.length > 0 && (
        <ul>
          {group.issues.map((issue) => (
            <li key={issue.allocation_id}>
              {issueLabels[issue.reason] ??
                `Source validation requires review (${issue.reason}).`}{" "}
              Allocation {issue.allocation_id} ·{" "}
              <Link
                href={`/receivables/${encodeURIComponent(issue.invoice_id)}?workspace=${encodeURIComponent(workspace)}`}
              >
                Inspect invoice
              </Link>
            </li>
          ))}
        </ul>
      )}
      {group.evidence.length > 0 && (
        <details>
          <summary>Inspect cash allocation evidence</summary>
          <div className="impact-table-scroll">
            <table>
              <caption>
                Immutable imported allocations for this reporting window
              </caption>
              <thead>
                <tr>
                  <th>Source / invoice</th>
                  <th>Fact</th>
                  <th>Amount</th>
                  <th>Effective / observed / imported (UTC)</th>
                </tr>
              </thead>
              <tbody>
                {group.evidence.map((row) => (
                  <tr key={row.allocation_id}>
                    <td>
                      <Link
                        href={`/receivables/${encodeURIComponent(row.invoice_id)}?workspace=${encodeURIComponent(workspace)}`}
                      >
                        <InvoiceRef id={row.invoice_id} />
                      </Link>
                      <p>Allocation {row.allocation_id}</p>
                      <p>
                        Source {row.provider_connection_id} · Revision{" "}
                        {row.source_revision}
                      </p>
                      {row.parent_id && <p>Linked cash {row.parent_id}</p>}
                    </td>
                    <td>
                      {row.kind.replaceAll("_", " ")} ·{" "}
                      {row.included ? "included" : "excluded"}
                    </td>
                    <td>
                      {Number.isInteger(group.exponent) &&
                      group.exponent >= 0 &&
                      group.exponent <= 6
                        ? formatMoney({
                            minor_units: row.minor_units,
                            currency: group.currency,
                            exponent: group.exponent,
                          })
                        : `${row.minor_units} minor units (unsupported exponent)`}
                    </td>
                    <td>
                      <time dateTime={row.effective_at}>
                        {row.effective_at}
                      </time>
                      <br />
                      <time dateTime={row.observed_at}>{row.observed_at}</time>
                      <br />
                      <time dateTime={row.imported_at}>{row.imported_at}</time>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </section>
  );
}
export function ActivityRecords({
  workspace,
  groups,
  records,
}: {
  workspace: string;
  groups: ActivityGroup[];
  records: ActivityRecord[];
}) {
  const href = (path: string) =>
    `${path}?workspace=${encodeURIComponent(workspace)}`;
  return (
    <section className="panel" aria-label="Recorded activity">
      <h2>Recorded activity</h2>
      <p>
        Records created in the selected window, with their current saved status.
        Activity is not proof of autonomous resolution or financial impact.
      </p>
      {groups.length === 0 ? (
        <p>No new activity records are available in this window.</p>
      ) : (
        <div className="panel-grid">
          {groups.map((group) => (
            <section key={`${group.kind}-${group.synthetic}`}>
              <h3>
                {kindLabels[group.kind]} · {group.count}
              </h3>
              <p>
                {group.synthetic ? "Synthetic / simulated" : "Observed records"}
              </p>
              <ul>
                {Object.entries(group.statuses).map(([status, count]) => (
                  <li key={status}>
                    {status.replaceAll("_", " ")}: {count}
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}
      {records.length > 0 && (
        <details>
          <summary>Inspect recent saved records</summary>
          <ul>
            {records.map((row) => {
              const path =
                row.kind === "agent_runs"
                  ? `/agent-runs/${row.id}`
                  : row.kind === "cases"
                    ? `/cases/${row.id}`
                    : row.kind === "scheduled_actions" && row.invoice_id
                      ? `/receivables/${row.invoice_id}`
                      : "/overview";
              return (
                <li key={`${row.kind}-${row.id}`}>
                  <Link href={href(path)}>
                    {kindLabels[row.kind]} {row.id}
                  </Link>{" "}
                  · {row.status.replaceAll("_", " ")} ·{" "}
                  {row.synthetic ? "Synthetic / simulated" : "Observed"} ·{" "}
                  {stamp(row.created_at)}
                </li>
              );
            })}
          </ul>
        </details>
      )}
      <div className="communication-links">
        <Link href={href("/review")}>Open Review Queue</Link>
        <Link href={href("/agents")}>View investigation history</Link>
        <Link href={href("/overview")}>Inspect usage and budgets</Link>
      </div>
    </section>
  );
}
export function ImpactDashboard({
  workspace,
  initialWindow,
  csrfToken,
}: {
  workspace: string;
  initialWindow?: ImpactWindow;
  csrfToken?: string;
}) {
  const router = useRouter();
  const [start, setStart] = useState(utcWindowInput(initialWindow?.since));
  const [end, setEnd] = useState(utcWindowInput(initialWindow?.until));
  const [applied, setApplied] = useState<ImpactWindow | null>(
    initialWindow?.since && initialWindow.until ? initialWindow : null,
  );
  const [error, setError] = useState("");
  const query = useQuery({
    queryKey: ["impact", workspace, applied?.since, applied?.until],
    enabled: !!applied,
    queryFn: ({ signal }) => impactApi.read(workspace, applied!, signal),
    retry: false,
  });
  function apply(window: ImpactWindow) {
    setError("");
    setApplied(window);
    setStart(utcWindowInput(window.since));
    setEnd(utcWindowInput(window.until));
    router.replace(
      `/business-impact?${new URLSearchParams({ workspace, ...window })}`,
    );
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const since = new Date(`${start}Z`).toISOString(),
        until = new Date(`${end}Z`).toISOString();
      if (since >= until)
        throw new Error("The UTC start must precede the end.");
      apply({ since, until });
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Enter a valid UTC reporting window.",
      );
    }
  }
  return (
    <div className="impact-dashboard">
      <section className="panel">
        <h2>Source-backed financial activity</h2>
        <p>
          Current aging, known imported cash and measured provider evidence.
          Cash and provider fees stay separate by currency and provenance; this
          view does not establish incremental revenue or causal uplift.
        </p>
        <form className="task-form" onSubmit={submit}>
          <label>
            Window start (UTC, included)
            <input
              required
              type="datetime-local"
              step="1"
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
          </label>
          <label>
            Window end (UTC, excluded)
            <input
              required
              type="datetime-local"
              step="1"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
            />
          </label>
          <button type="submit">Apply reporting window</button>
          <button
            type="button"
            onClick={() => {
              const until = new Date();
              apply({
                since: new Date(until.getTime() - 30 * 86400000).toISOString(),
                until: until.toISOString(),
              });
            }}
          >
            Last 30 days through now
          </button>
        </form>
        {error && <p role="alert">{error}</p>}
      </section>
      <CohortPanel workspace={workspace} csrfToken={csrfToken} />
      <CaseOutcomesPanel workspace={workspace} window={applied} />
      <EconomicsPanel
        workspace={workspace}
        window={applied}
        csrfToken={csrfToken}
      />
      {!applied ? (
        <p>Choose an explicit UTC reporting window.</p>
      ) : query.isPending ? (
        <p role="status">Loading saved source and usage evidence…</p>
      ) : query.isError ? (
        <ErrorState
          label="Business Impact unavailable"
          error={query.error}
          retry={() => query.refetch()}
        />
      ) : (
        <>
          <section className="panel" aria-label="Reporting basis">
            <p>
              Cash and activity window:{" "}
              <time dateTime={query.data.since}>{query.data.since}</time>{" "}
              included →{" "}
              <time dateTime={query.data.until}>{query.data.until}</time>{" "}
              excluded.
            </p>
            <p>
              Report read as of {stamp(query.data.as_of_at)}. Current AR and
              aging below describe this current snapshot, not historical
              balances at the reporting-window end.
            </p>
            <button
              type="button"
              disabled={query.isFetching}
              onClick={() => void query.refetch()}
            >
              Refresh same reporting window
            </button>
            {query.data.source_warnings.map((warning) => (
              <p className="notice" key={warning}>
                {warning}
              </p>
            ))}
          </section>
          <section aria-label="Current accounting aging">
            <h2>Current AR and aging</h2>
            {query.data.operations.availability === "unavailable" ? (
              <p>No imported current accounting balance is available.</p>
            ) : (
              query.data.operations.currencies.map((group) => (
                <CurrencyOverview
                  key={`${group.currency}-${group.exponent}`}
                  group={group}
                />
              ))
            )}
          </section>
          <section aria-label="Known imported cash">
            <h2>Known imported net cash</h2>
            <p className="notice">
              Partial source history. No complete-history watermark is
              available, so these supported imported facts are not a
              complete-period cash total. Credits and write-offs do not count as
              cash. Linked refunds and reversals use their own effective
              timestamps.
            </p>
            <p>{query.data.cash_basis}</p>
            {query.data.cash.length ? (
              query.data.cash.map((group) => (
                <CashRecord
                  key={`${group.currency}-${group.exponent}-${group.synthetic}`}
                  group={group}
                  workspace={workspace}
                />
              ))
            ) : (
              <p>
                No imported cash or adjustment facts are available in this
                window. Missing history is not a verified zero.
              </p>
            )}
          </section>
          <ActivityRecords
            workspace={workspace}
            groups={query.data.activity}
            records={query.data.recent_activity}
          />
          <section
            className="panel"
            aria-label="Provider evidence in reporting window"
          >
            <h2>Provider evidence in this window</h2>
            <p>{query.data.usage_basis}</p>
            <p>
              Known provider subtotals are partial when any charge or bundle
              relationship is unknown. Unknown charges are not zero costs; these
              are not costs per resolved case.
            </p>
            <UsageDetails view={query.data.usage} />
          </section>
          <section
            className="panel"
            aria-label="Outcome metrics requiring more evidence"
          >
            <h2>Outcome comparisons and economics</h2>
            <p>
              Use the cohort, case outcomes and operating economics panels for
              their versioned results and source evidence. This reporting-window
              summary does not replace those scopes or establish causal uplift,
              measured labour savings or achieved ROI.
            </p>
            <dl>
              {query.data.unavailable.map((metric) => (
                <div key={metric.metric}>
                  <dt>{metric.metric}</dt>
                  <dd>{metric.reason}</dd>
                </div>
              ))}
            </dl>
          </section>
        </>
      )}
    </div>
  );
}
