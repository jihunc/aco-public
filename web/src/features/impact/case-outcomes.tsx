"use client";
import Link from "next/link";
import { formatDuration } from "@/lib/format";
import { useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import type {
  CaseOutcomeGroup,
  CaseOutcomeView,
  CaseOutcomesView,
} from "@/lib/generated-api";
import { ErrorState, stamp } from "@/features/receivables/shared";
import type { ImpactWindow } from "./api";
import { caseOutcomesApi } from "./case-outcomes-api";
const decimal = (value: string | null | undefined, unit = "") =>
  value == null ? "Insufficient data" : `${value}${unit}`;
// Readable duration first; the exact retained decimal stays visible for audit.
const duration = (value: string | null | undefined) =>
  value == null ? "Insufficient data" : `${formatDuration(Number(value))} (${value} seconds)`;
type HandlingCoverage = Pick<
  CaseOutcomeGroup,
  | "reported_human_minutes"
  | "known_reported_human_minutes"
  | "missing_handling_event_count"
>;
function ManualHandlingCoverage({ record }: { record: HandlingCoverage }) {
  return (
    <div className="case-handling-coverage">
      <p>
        <strong>MANUAL_REPORTED handling inputs</strong>
      </p>
      <dl>
        <div>
          <dt>Covered reported handling time</dt>
          <dd>{decimal(record.reported_human_minutes, " minutes")}</dd>
        </div>
        <div>
          <dt>Known reported subtotal</dt>
          <dd>{decimal(record.known_reported_human_minutes, " minutes")}</dd>
        </div>
      </dl>
      <p>
        {record.missing_handling_event_count === undefined
          ? "Missing handling-event coverage unavailable."
          : `${record.missing_handling_event_count} substantive handling events without time entries.`}
      </p>
      <p>
        Known subtotal does not cover missing time. These are operator-reported
        active minutes, not automatic measurement or time saved.
      </p>
    </div>
  );
}
function CaseOutcomeSummary({ group }: { group: CaseOutcomeGroup }) {
  return (
    <section
      className="cohort-side"
      aria-label={
        group.synthetic ? "Synthetic case history" : "Observed case history"
      }
    >
      <h3>
        {group.synthetic ? "Synthetic case history" : "Observed case history"}
      </h3>
      <p>
        {group.eligible_count} eligible cases · {group.unknown_count} with
        incomplete history.
      </p>
      {group.unknown_count > 0 && (
        <p className="notice">
          Incomplete history prevents a supported rate or mean duration for this
          group. Unknown is not zero.
        </p>
      )}
      <dl className="cohort-metrics">
        <div>
          <dt>Recorded autonomous resolution</dt>
          <dd>{decimal(group.autonomous_resolution_percent, "%")}</dd>
          <p>
            {group.autonomous_resolved_count} of {group.resolved_count} eligible
            resolved cases; zero substantive human handling required.
          </p>
        </div>
        <div>
          <dt>Substantive human intervention</dt>
          <dd>{decimal(group.human_intervention_percent, "%")}</dd>
          <p>
            {group.human_intervention_count} of {group.eligible_count} eligible
            cases.
          </p>
        </div>
        <div>
          <dt>Mean time to eligible resolution</dt>
          <dd>{duration(group.mean_resolution_seconds)}</dd>
        </div>
        <div>
          <dt>Mean open-backlog age</dt>
          <dd>{duration(group.mean_open_age_seconds)}</dd>
        </div>
      </dl>
      <ManualHandlingCoverage record={group} />
      <p>
        {group.resolved_count} currently eligible resolved ·{" "}
        {group.open_backlog_count} open backlog ·{" "}
        {group.administrative_closed_count} administrative closures.
      </p>
      <p>
        Administrative closures are separate from successful issue resolution.
        Creating a task, recording payment status or approving an action does
        not establish an autonomous case resolution.
      </p>
    </section>
  );
}
export function CaseOutcomeRecord({
  record,
  workspace,
}: {
  record: CaseOutcomeView;
  workspace: string;
}) {
  return (
    <li className="case-outcome-record">
      <h4>
        <Link
          href={`/cases/${encodeURIComponent(record.case_id)}?workspace=${encodeURIComponent(workspace)}`}
        >
          Case {record.case_id}
        </Link>
      </h4>
      <p>
        {record.synthetic ? "Synthetic" : "Observed"} · {record.status} ·{" "}
        {record.completeness}
      </p>
      <p>
        Opened {stamp(record.opened_at)} · {record.resolved_episode_count}{" "}
        recorded resolved episode
        {record.resolved_episode_count === 1 ? "" : "s"}.
      </p>
      <p>
        {record.eligible_resolved
          ? "Currently eligible as resolved"
          : "Not currently eligible as resolved"}
        {record.autonomous &&
        record.eligible_resolved &&
        record.completeness === "COMPLETE"
          ? " · Autonomously resolved in retained history"
          : ""}
        .
      </p>
      {record.substantive_human ? (
        <p>Substantive human handling recorded.</p>
      ) : (
        <p>
          {record.completeness === "COMPLETE"
            ? "No substantive human handling in retained history."
            : "Human handling conclusion unavailable with incomplete history."}
        </p>
      )}
      {record.open_backlog && (
        <p>
          Open backlog age: {duration(record.backlog_age_seconds)}. A
          prior resolved episode does not make a reopened case currently
          resolved.
        </p>
      )}
      {record.administrative_closed && (
        <p>
          Administrative closure ·{" "}
          {record.closure_reason?.replaceAll("_", " ") ?? "Reason unavailable"}.
          Excluded from successful resolutions.
        </p>
      )}
      {record.eligible_resolved && (
        <p>
          Resolved at {stamp(record.resolved_at)} · Elapsed from original
          opening: {duration(record.resolution_seconds)}.
        </p>
      )}
      {record.issues.length > 0 && (
        <ul className="notice">
          {record.issues.map((issue) => (
            <li key={issue}>{issue}</li>
          ))}
        </ul>
      )}
      <ManualHandlingCoverage record={record} />
      {!!record.approval_request_ids?.length && (
        <div>
          <p>
            Exact case-bound review decisions:{" "}
            {record.approval_decision_ids?.join(", ")}. Unchanged approval is
            separate from substantive handling.
          </p>
          <ul>
            {record.approval_request_ids.map((id) => (
              <li key={id}>
                <Link
                  href={`/review?workspace=${encodeURIComponent(workspace)}&approval=${encodeURIComponent(id)}`}
                >
                  Open case approval {id}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
      <details>
        <summary>Inspect retained case history references</summary>
        <p>
          History records:{" "}
          {record.history_ids.length
            ? record.history_ids.join(", ")
            : "No retained history returned"}
          .
        </p>
        <p>
          Resolution evidence:{" "}
          {record.resolution_evidence_ids.length
            ? record.resolution_evidence_ids.join(", ")
            : "No resolution evidence returned"}
          .
        </p>
        <p>
          Manual handling entries:{" "}
          {record.handling_entry_ids?.length
            ? record.handling_entry_ids.join(", ")
            : "No reported handling entry references returned"}
          .
        </p>
        <p>
          The linked case shows its saved history and source evidence. These
          references do not imply that an invoice was cash-paid.
        </p>
      </details>
    </li>
  );
}
export function CaseOutcomeReport({
  view,
  workspace,
}: {
  view: CaseOutcomesView;
  workspace: string;
}) {
  return (
    <div className="case-outcome-report">
      <p>
        Cases opened in <time dateTime={view.since}>{view.since}</time> included
        → <time dateTime={view.until}>{view.until}</time> excluded. History
        replayed through <time dateTime={view.as_of_at}>{view.as_of_at}</time>{" "}
        included; read at {stamp(view.generated_at)}.
      </p>
      <p className="notice">
        {view.caveat ??
          "Recorded issue outcomes do not establish that ACO caused collection or labour savings."}
      </p>
      {view.mixed_sources && (
        <p>
          No blended outcome rate or timing is shown. Synthetic and observed
          histories remain separate below.
        </p>
      )}
      {!view.cases.length ? (
        <p>
          No recorded cases opened in this window. Resolution rates and
          elapsed-time averages are unavailable for an empty population.
        </p>
      ) : (
        <>
          <p>
            {view.eligible_count} recorded eligible cases across the source
            groups below. Retained history completeness controls which outcome
            rates are available.
          </p>
          <div className="cohort-sides">
            {view.groups.map((group) => (
              <CaseOutcomeSummary key={String(group.synthetic)} group={group} />
            ))}
          </div>
          <details>
            <summary>
              Inspect recorded case outcomes ({view.cases.length})
            </summary>
            <ul className="case-outcome-list">
              {view.cases.map((record) => (
                <CaseOutcomeRecord
                  key={record.case_id}
                  record={record}
                  workspace={workspace}
                />
              ))}
            </ul>
          </details>
        </>
      )}
      <dl>
        <div>
          <dt>Reported human handling time</dt>
          <dd>{decimal(view.reported_human_minutes, " minutes")}</dd>
          <p>
            {view.human_time_reason ??
              "No recorded handling-time inputs; activity is not measured duration."}
          </p>
        </div>
        <div>
          <dt>Case-attributed approvals</dt>
          <dd>{view.case_approval_count ?? "Insufficient data"}</dd>
          <p>
            {view.approval_reason ??
              "Approval decisions have no verified case binding; invoice overlap is not attribution."}
          </p>
        </div>
      </dl>
      <p className="muted">
        Source: retained recorded case history ·{" "}
        {view.algorithm ?? "case_history_v1"}. Human time is not inferred from
        event counts. Cost per resolution, capacity savings and ROI need their
        separate allocation and assumption evidence.
      </p>
    </div>
  );
}
export function CaseOutcomesPanel({
  workspace,
  window,
}: {
  workspace: string;
  window: ImpactWindow | null;
}) {
  const [input, setInput] = useState("");
  const [asOf, setAsOf] = useState<string | undefined>();
  const query = useQuery({
    queryKey: ["case-outcomes", workspace, window?.since, window?.until, asOf],
    enabled: !!window,
    queryFn: ({ signal }) =>
      caseOutcomesApi.read(
        workspace,
        { ...window!, ...(asOf ? { as_of: asOf } : {}) },
        signal,
      ),
    retry: false,
  });
  function apply(event: FormEvent) {
    event.preventDefault();
    const next = input ? `${input}Z` : undefined;
    setAsOf(next);
    if (next === asOf) void query.refetch();
  }
  return (
    <section
      className="panel case-outcomes-panel"
      aria-label="Recorded case outcomes"
    >
      <h2>Recorded case outcomes</h2>
      <p>
        Issue dispositions and substantive handling are replayed from saved case
        history. Invoice balances remain accounting truth; recorded case
        resolution is not cash collection.
      </p>
      {!window ? (
        <p>
          Apply an explicit reporting window above to read its case population.
        </p>
      ) : (
        <>
          <form onSubmit={apply} className="case-outcome-controls">
            <label htmlFor="case-as-of">
              Case history as-of (UTC, included; optional)
            </label>
            <input
              id="case-as-of"
              type="datetime-local"
              step="1"
              value={input}
              onChange={(event) => setInput(event.target.value)}
            />
            <button type="submit" disabled={query.isFetching}>
              Apply case as-of
            </button>
            <p>
              Leave empty to replay through the server&apos;s current read time.
              This affects case history only, not cash, aging or frozen source
              cohorts.
            </p>
          </form>
          {query.isPending ? (
            <p role="status">Reading recorded case history…</p>
          ) : query.isError ? (
            <ErrorState
              label="Case outcomes unavailable"
              error={query.error}
              retry={() => query.refetch()}
            />
          ) : (
            <CaseOutcomeReport view={query.data} workspace={workspace} />
          )}
          <button
            type="button"
            disabled={query.isFetching}
            onClick={() => void query.refetch()}
          >
            Reload recorded case outcomes
          </button>
        </>
      )}
    </section>
  );
}
