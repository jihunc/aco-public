"use client";
import Link from "next/link";
import { useState, useSyncExternalStore, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import type {
  CohortResultView,
  ObservedCohortView,
  ObservedMember,
  ObservedPreview,
  ObservedSelection,
} from "@/lib/generated-api";
import { ErrorState, stamp } from "@/features/receivables/shared";
import {
  canSubmitObserved,
  captureObservedReview,
  observedApi,
  ObservedError,
  readObservedReview,
  type PendingObservedReview,
} from "./observed-api";
const changeEvent = "aco:observed-cohort-review";
function subscribe(callback: () => void) {
  window.addEventListener(changeEvent, callback);
  window.addEventListener("storage", callback);
  return () => {
    window.removeEventListener(changeEvent, callback);
    window.removeEventListener("storage", callback);
  };
}
const decimal = (value: string | null | undefined, unit = "") =>
  value == null ? "Insufficient data" : `${value}${unit}`;

export function ObservedResult({
  result,
  currency,
}: {
  result: CohortResultView;
  currency: string;
}) {
  return (
    <section
      aria-label={`Observed result version ${result.version}`}
      style={{ overflowWrap: "anywhere" }}
    >
      <h5>
        {result.label} · {result.status} · result version {result.version}
      </h5>
      <p>
        Frozen source snapshot · {stamp(result.as_of_at)} · completeness shown
        for this saved as-of, not current invoice status.
      </p>
      <dl className="cohort-metrics">
        <div>
          <dt>30-day cash recovery</dt>
          <dd>{decimal(result.recovery_percent, "%")}</dd>
        </div>
        <div>
          <dt>30-day cash-paid-in-full</dt>
          <dd>{decimal(result.paid_in_full_percent, "%")}</dd>
        </div>
        <div>
          <dt>Frozen eligible opening · {currency} minor units</dt>
          <dd>{decimal(result.opening_minor)}</dd>
        </div>
        <div>
          <dt>Qualifying cash · {currency} minor units</dt>
          <dd>{decimal(result.recovery_numerator_minor)}</dd>
        </div>
        <div>
          <dt>Days to full cash settlement</dt>
          <dd>{decimal(result.days_to_collect)}</dd>
        </div>
        <div>
          <dt>Days beyond original terms</dt>
          <dd>{decimal(result.days_beyond_terms)}</dd>
        </div>
      </dl>
      <p>
        {result.eligible_count} eligible · {result.excluded_count} excluded or
        unavailable. {result.reason}
      </p>
      <ul>
        {result.issues.map((issue) => (
          <li key={issue}>{issue}</li>
        ))}
      </ul>
      <details>
        <summary>Retained result and accounting evidence</summary>
        <p>
          Result {result.id} · prior result {result.prior_result_id ?? "None"}
        </p>
        <p>Source watermark {result.source_watermark}</p>
        <p>
          Raw signed net {decimal(result.raw_net_minor)} · refunds{" "}
          {result.refunds_minor} · reversals {result.reversals_minor} (
          {currency} minor units).
        </p>
        <p>
          Canonical source observations:{" "}
          {result.observation_ids.join(", ") || "Unavailable"}
        </p>
        <p>
          Actual adjustment identities:{" "}
          {result.adjustment_ids.join(", ") || "None recorded"}
        </p>
      </details>
    </section>
  );
}
export function ObservedMembers({
  members,
  workspace,
  currency,
}: {
  members: ObservedMember[];
  workspace: string;
  currency: string;
}) {
  return (
    <div className="impact-table-scroll">
      <table>
        <caption>
          Original membership and source references · {currency} minor units
        </caption>
        <thead>
          <tr>
            <th>Invoice / source</th>
            <th>Original / current due</th>
            <th>Opening exposure</th>
            <th>Eligibility / change</th>
          </tr>
        </thead>
        <tbody>
          {members.map((row) => (
            <tr key={row.invoice_id}>
              <td>
                <Link
                  href={`/receivables/${encodeURIComponent(row.invoice_id)}?workspace=${encodeURIComponent(workspace)}`}
                >
                  Invoice {row.invoice_id}
                </Link>
                <details>
                  <summary>Original source references</summary>
                  <p>
                    Issue {row.issue_date}; original amount {row.original_minor}
                    .
                  </p>
                  <p>Opening source {row.opening_source_id ?? "Unavailable"}</p>
                  <p>
                    Native invoice revisions{" "}
                    {row.source_revision_ids.join(", ") || "Unavailable"}
                  </p>
                  <p>Frozen watermark {row.source_watermark}</p>
                  <p>Segment {row.business_segment ?? "Unavailable"}</p>
                </details>
              </td>
              <td>
                {row.original_due_date}
                <br />
                {row.current_due_date}
              </td>
              <td>{decimal(row.opening_minor)}</td>
              <td>
                {row.eligibility}
                <p>{row.source_change}</p>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!members.length && (
        <p>
          No invoices in this exact due-date/currency/term population. No
          recovery is inferred.
        </p>
      )}
    </div>
  );
}
export function ObservedRecord({
  cohort,
  workspace,
}: {
  cohort: ObservedCohortView;
  workspace: string;
}) {
  return (
    <section
      aria-label={`Observed cohort ${cohort.id}`}
      className="cohort-pair"
      style={{ overflowWrap: "anywhere" }}
    >
      <h3>{cohort.label}</h3>
      <p>
        Execution basis {cohort.execution_basis} ·{" "}
        {cohort.synthetic
          ? "Synthetic financial source"
          : "Observed financial source"}
        . {cohort.caveat}
      </p>
      <p>
        Cohort {cohort.id} · source connection {cohort.source_connection_id}
      </p>
      {cohort.versions.map((version) => (
        <section
          key={version.id}
          aria-label={`Membership version ${version.version}`}
        >
          <h4>Frozen membership version {version.version}</h4>
          <p>
            {version.selection.period_start} ≤ original due date &lt;{" "}
            {version.selection.period_end} · {version.selection.currency} ·{" "}
            {version.selection.credit_terms_days} original credit-term days ·{" "}
            {version.timezone}
          </p>
          <p>
            Definition {version.id} · prior definition{" "}
            {version.prior_definition_id ?? "None"}. {version.reason}
          </p>
          <details>
            <summary>Inspect frozen members ({version.members.length})</summary>
            <ObservedMembers
              members={version.members}
              workspace={workspace}
              currency={version.selection.currency}
            />
          </details>
          <ul>
            {version.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
          {version.current_source_members.some((row) => row.source_change) && (
            <details>
              <summary>
                Current source changes — original membership retained
              </summary>
              <ObservedMembers
                members={version.current_source_members}
                workspace={workspace}
                currency={version.selection.currency}
              />
            </details>
          )}
          {version.results.map((result) => (
            <ObservedResult
              key={result.id}
              result={result}
              currency={version.selection.currency}
            />
          ))}
        </section>
      ))}
    </section>
  );
}
function PreviewRecord({
  preview,
  workspace,
}: {
  preview: ObservedPreview;
  workspace: string;
}) {
  return (
    <>
      <p>
        Preview only — no frozen cohort has been published by this preview.{" "}
        {preview.synthetic
          ? "Synthetic financial source remains synthetic."
          : "Observed accounting source."}
      </p>
      <p>
        Captured due range {preview.selection.period_start} to{" "}
        {preview.selection.period_end} (end excluded),{" "}
        {preview.selection.currency}, {preview.selection.credit_terms_days}{" "}
        days, {preview.timezone}; as-of {stamp(preview.selection.as_of_at)}.
      </p>
      <p>Source/population hash {preview.reviewed_hash}</p>
      <ul>
        {preview.warnings.map((warning) => (
          <li key={warning}>{warning}</li>
        ))}
      </ul>
      <ObservedResult
        result={preview.result}
        currency={preview.selection.currency}
      />
      <details>
        <summary>Review captured members ({preview.members.length})</summary>
        <ObservedMembers
          members={preview.members}
          workspace={workspace}
          currency={preview.selection.currency}
        />
      </details>
    </>
  );
}

export function ObservedCohortPanel({
  workspace,
  csrfToken,
}: {
  workspace: string;
  csrfToken?: string;
}) {
  const query = useQuery({
    queryKey: ["observed-cohorts", workspace],
    queryFn: ({ signal }) => observedApi.read(workspace, signal),
    retry: false,
  });
  const [connection, setConnection] = useState(""),
    [start, setStart] = useState(""),
    [end, setEnd] = useState(""),
    [currency, setCurrency] = useState("AUD"),
    [terms, setTerms] = useState("30"),
    [asOf, setAsOf] = useState(""),
    [evidence, setEvidence] = useState(""),
    [target, setTarget] = useState<string | null>(null),
    [mode, setMode] = useState<"REFRESH" | "RESTATE">("REFRESH"),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [confirmed, setConfirmed] = useState(false);
  const key = `aco:observed-cohort-review:${workspace}`;
  const raw = useSyncExternalStore(
    subscribe,
    () => {
      try {
        return sessionStorage.getItem(key);
      } catch {
        return null;
      }
    },
    () => null,
  );
  const pending = readObservedReview(raw);
  function savePending(value: PendingObservedReview | null) {
    if (value) sessionStorage.setItem(key, JSON.stringify(value));
    else sessionStorage.removeItem(key);
    window.dispatchEvent(new Event(changeEvent));
  }
  function choose(cohort: ObservedCohortView, kind: "REFRESH" | "RESTATE") {
    const latest = cohort.versions.at(-1);
    if (!latest) return;
    setTarget(cohort.id);
    setMode(kind);
    setConnection(latest.selection.connection_id);
    setStart(latest.selection.period_start);
    setEnd(latest.selection.period_end);
    setCurrency(latest.selection.currency);
    setTerms(String(latest.selection.credit_terms_days));
    setAsOf("");
    setEvidence("");
    setConfirmed(false);
  }
  async function preview(event: FormEvent) {
    event.preventDefault();
    if (!csrfToken || !query.data?.can_build || query.isFetching || busy)
      return;
    setBusy(true);
    setError("");
    try {
      const selection: ObservedSelection = {
        connection_id: connection,
        period_start: start,
        period_end: end,
        currency,
        credit_terms_days: Number(terms),
        as_of_at: asOf ? new Date(`${asOf}Z`).toISOString() : null,
      };
      const parent = target
        ? query.data.cohorts.find((row) => row.id === target)
        : undefined;
      if (target && !parent)
        throw Error("Saved cohort unavailable; refresh first.");
      const candidate = await observedApi.preview(
        workspace,
        selection,
        csrfToken,
        parent,
        mode,
      );
      savePending({
        review: captureObservedReview(
          candidate,
          evidence,
          crypto.randomUUID(),
          parent,
          mode,
        ),
        phase: "captured",
      });
      setConfirmed(false);
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Preview unavailable.",
      );
    } finally {
      setBusy(false);
    }
  }
  async function submit() {
    if (
      !pending ||
      !csrfToken ||
      !confirmed ||
      pending.phase === "conflict" ||
      !canSubmitObserved(pending.review, query.data, !query.isFetching) ||
      busy
    )
      return;
    setBusy(true);
    setError("");
    try {
      savePending(pending);
      await observedApi.submit(workspace, pending.review, csrfToken);
      savePending(null);
      setConfirmed(false);
      await query.refetch();
    } catch (reason) {
      savePending({
        ...pending,
        phase:
          reason instanceof ObservedError && reason.status < 500
            ? "conflict"
            : "uncertain",
      });
      setError(
        reason instanceof Error ? reason.message : "Publication not confirmed.",
      );
      await query.refetch();
    } finally {
      setBusy(false);
    }
  }
  if (query.isPending)
    return <p role="status">Loading observed cohort history…</p>;
  if (query.isError)
    return (
      <ErrorState
        label="Observed cohort history unavailable"
        error={query.error}
        retry={() => query.refetch()}
      />
    );
  const allowed = !!csrfToken && query.data.can_build;
  const locked = !!target && mode === "REFRESH";
  return (
    <section
      className="panel"
      aria-label="Observed frozen cohorts"
      style={{ overflowWrap: "anywhere" }}
    >
      <h2>Freeze an accounting-source cohort</h2>
      <p>
        Choose an original due-date population and review actual historical
        evidence. Missing opening history stays unavailable. Observed execution
        does not turn synthetic accounting into real customer evidence.
      </p>
      {query.data.cohorts.map((cohort) => (
        <div key={cohort.id}>
          <ObservedRecord cohort={cohort} workspace={workspace} />
          {allowed && (
            <div>
              <button
                type="button"
                disabled={busy || query.isFetching}
                onClick={() => choose(cohort, "REFRESH")}
              >
                Review source refresh {cohort.id}
              </button>
              <button
                type="button"
                disabled={busy || query.isFetching}
                onClick={() => choose(cohort, "RESTATE")}
              >
                Review membership restatement {cohort.id}
              </button>
            </div>
          )}
        </div>
      ))}
      {allowed && (
        <form
          className="task-form"
          onSubmit={preview}
          aria-label="Observed cohort selection"
        >
          <h3>
            {target
              ? mode === "REFRESH"
                ? "Refresh frozen membership"
                : "Explicit membership restatement"
              : "New frozen observed cohort"}
          </h3>
          {target && (
            <p>
              Selected cohort {target}. Refresh retains members; restatement
              explicitly creates a new membership version.
            </p>
          )}
          <label>
            Observed source connection
            <select
              value={connection}
              onChange={(event) => setConnection(event.target.value)}
              required
              disabled={!!target}
            >
              <option value="">Select actual accounting source</option>
              {query.data.sources.map((source) => (
                <option key={source.connection_id} value={source.connection_id}>
                  {source.provider} ·{" "}
                  {source.synthetic ? "synthetic" : "observed"} ·{" "}
                  {source.connection_id}
                </option>
              ))}
            </select>
          </label>
          <label>
            Original due-date start
            <input
              type="date"
              value={start}
              onChange={(event) => setStart(event.target.value)}
              required
              disabled={locked}
            />
          </label>
          <label>
            Original due-date end (excluded)
            <input
              type="date"
              value={end}
              onChange={(event) => setEnd(event.target.value)}
              required
              disabled={locked}
            />
          </label>
          <label>
            Cohort source currency
            <input
              value={currency}
              onChange={(event) =>
                setCurrency(event.target.value.toUpperCase())
              }
              pattern="[A-Z]{3}"
              required
              disabled={locked}
            />
          </label>
          <label>
            Original credit-term days
            <input
              type="number"
              min="0"
              max="3650"
              step="1"
              value={terms}
              onChange={(event) => setTerms(event.target.value)}
              required
              disabled={locked}
            />
          </label>
          <label>
            Cohort as-of (UTC; blank reads through now)
            <input
              type="datetime-local"
              step="1"
              value={asOf}
              onChange={(event) => setAsOf(event.target.value)}
            />
          </label>
          <label>
            Observed cohort review evidence
            <textarea
              value={evidence}
              onChange={(event) => setEvidence(event.target.value)}
              minLength={10}
              required
            />
          </label>
          <button disabled={busy || query.isFetching} type="submit">
            Preview observed source population
          </button>
          {target && (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setTarget(null);
                setConnection("");
              }}
            >
              Select a new cohort instead
            </button>
          )}
        </form>
      )}
      {pending && (
        <section
          aria-label="Captured observed cohort review"
          style={{ overflowWrap: "anywhere" }}
        >
          <h3>Captured {pending.review.mode.toLowerCase()} review</h3>
          <PreviewRecord
            preview={pending.review.preview}
            workspace={workspace}
          />
          <p>Owner evidence: {pending.review.command.evidence}</p>
          <p>
            Request {pending.review.command.request_id} · exact as-of and source
            hash retained across reload.
          </p>
          {pending.phase === "conflict" && (
            <p role="status">
              Captured source changed. Explicitly preview again; this request
              will not retry.
            </p>
          )}
          {pending.phase === "uncertain" && (
            <p role="status">
              Outcome uncertain. Inspect saved cohorts or retry this same
              captured request.
            </p>
          )}
          <label>
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            I reviewed the captured population, source authenticity, missing
            history and as-of.
          </label>
          <button
            type="button"
            disabled={
              !allowed ||
              !confirmed ||
              busy ||
              pending.phase === "conflict" ||
              !canSubmitObserved(pending.review, query.data, !query.isFetching)
            }
            onClick={submit}
          >
            {pending.phase === "uncertain"
              ? "Retry captured cohort request"
              : "Publish reviewed cohort version"}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              savePending(null);
              setConfirmed(false);
            }}
          >
            Discard observed cohort review
          </button>
        </section>
      )}
      {error && <p role="alert">{error}</p>}
      <button
        type="button"
        disabled={busy || query.isFetching}
        onClick={() => query.refetch()}
      >
        Reload observed cohort history
      </button>
    </section>
  );
}
