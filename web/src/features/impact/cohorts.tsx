"use client";
import Link from "next/link";
import { ObservedCohortPanel } from "./observed-cohorts";
import { useState, useSyncExternalStore } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ErrorState, stamp } from "@/features/receivables/shared";
import { formatMoney } from "@/lib/money";
import type {
  CohortPairView,
  CohortResultView,
  CohortView,
} from "@/lib/generated-api";
import {
  captureCohortReview,
  cohortApi,
  CohortCommandError,
  latestResult,
  readPendingCohortReview,
  type PendingCohortReview,
} from "./cohort-api";
const decimal = (value: string | null) =>
  value === null
    ? "Insufficient data"
    : value.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
const signed = (value: string | null) =>
  value === null
    ? "Insufficient data"
    : `${value.startsWith("-") || /^0(?:\.0+)?$/.test(value) ? "" : "+"}${decimal(value)}`;
const sideLabel = (side: string) =>
  side === "BASELINE_SYNTHETIC"
    ? "Before ACO · synthetic baseline"
    : "With ACO · synthetic illustration";
const money = (minor: string | null, cohort: CohortView) =>
  minor === null
    ? "Insufficient data"
    : formatMoney({
        minor_units: minor,
        currency: cohort.currency,
        exponent: cohort.exponent,
      });

export function CohortResult({
  result,
  cohort,
}: {
  result: CohortResultView;
  cohort: CohortView;
}) {
  return (
    <section
      className="cohort-result"
      aria-label={`${sideLabel(cohort.side)} result version ${result.version}`}
    >
      <p>
        <strong>
          Version {result.version} · {result.label.replaceAll("_", " ")} ·{" "}
          {result.status}
        </strong>
      </p>
      <p>
        Frozen source snapshot at{" "}
        <time dateTime={result.as_of_at}>{stamp(result.as_of_at)}</time>.
        Refresh to read source again; this is not current invoice status.
      </p>
      <p>
        Source last read{" "}
        <time dateTime={result.last_synced_at}>
          {stamp(result.last_synced_at)}
        </time>{" "}
        · Verified at the saved as-of time.
      </p>
      <dl className="cohort-metrics">
        <div>
          <dt>30-day cash recovery</dt>
          <dd data-testid={`cohort-recovery-${result.id}`}>
            {result.recovery_percent === null
              ? "Insufficient data"
              : `${decimal(result.recovery_percent)}%`}
          </dd>
          <p>
            {money(result.recovery_numerator_minor, cohort)} qualifying cash /{" "}
            {money(result.opening_minor, cohort)} frozen opening exposure
          </p>
        </div>
        <div>
          <dt>30-day paid-in-full rate</dt>
          <dd data-testid={`cohort-paid-in-full-${result.id}`}>
            {result.paid_in_full_percent === null
              ? "Insufficient data"
              : `${decimal(result.paid_in_full_percent)}%`}
          </dd>
          <p>
            {result.paid_in_full_count === null
              ? "Insufficient data"
              : `${result.paid_in_full_count} of ${result.eligible_count} eligible invoices`}
          </p>
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
        {result.eligible_count} eligible · {result.excluded_count} excluded.{" "}
        {result.reason}
      </p>
      {result.issues.length > 0 && (
        <ul className="notice">
          {result.issues.map((issue) => (
            <li key={issue}>{issue}</li>
          ))}
        </ul>
      )}
      <details>
        <summary>Inspect immutable result evidence</summary>
        <p>
          Result {result.id} · Prior result {result.prior_result_id ?? "None"}
        </p>
        <p>Source watermark: {result.source_watermark}</p>
        <p>
          Raw net cash: {money(result.raw_net_minor, cohort)} · Refunds:{" "}
          {money(result.refunds_minor, cohort)} · Reversals:{" "}
          {money(result.reversals_minor, cohort)}
        </p>
        <p>
          Source observations:{" "}
          {result.observation_ids.length
            ? result.observation_ids.join(", ")
            : "None returned"}
        </p>
        <p>
          Source adjustments:{" "}
          {result.adjustment_ids.length
            ? result.adjustment_ids.join(", ")
            : "None returned"}
        </p>
      </details>
    </section>
  );
}
function CohortSide({
  cohort,
  workspace,
}: {
  cohort: CohortView;
  workspace: string;
}) {
  const latest = latestResult(cohort.results);
  const prior = cohort.results
    .filter((result) => result.id !== latest?.id)
    .sort((a, b) => b.version - a.version);
  return (
    <article className="cohort-side" aria-label={sideLabel(cohort.side)}>
      <h3>{sideLabel(cohort.side)}</h3>
      <p>
        Original due-date cohort: {cohort.period_start} included →{" "}
        {cohort.period_end} excluded · {cohort.timezone}.
      </p>
      <p>
        Each member has its own 30-day collection window. Currency{" "}
        {cohort.currency}, exponent {cohort.exponent};{" "}
        {cohort.credit_terms_days}-day terms; segment {cohort.business_segment}.
      </p>
      <p>
        Date-only issue/due values use cohort-local midnight. Negative days
        beyond terms retain early settlement; this is not DSO.
      </p>
      {latest ? (
        <CohortResult result={latest} cohort={cohort} />
      ) : (
        <p>Insufficient data. No saved result version was returned.</p>
      )}
      <details>
        <summary>Inspect frozen membership and opening source</summary>
        <p>
          Cohort {cohort.id} · Algorithm {cohort.algorithm ?? "aco_cash_v1"}
        </p>
        <div className="impact-table-scroll">
          <table>
            <caption>Frozen cohort membership and opening source</caption>
            <thead>
              <tr>
                <th>Invoice</th>
                <th>Original dates</th>
                <th>Exposure / source</th>
              </tr>
            </thead>
            <tbody>
              {cohort.members.map((member) => (
                <tr key={member.invoice_id}>
                  <td>
                    <Link
                      href={`/receivables/${encodeURIComponent(member.invoice_id)}?workspace=${encodeURIComponent(workspace)}`}
                    >
                      Source invoice {member.invoice_id}
                    </Link>
                    <p>{member.eligibility}</p>
                  </td>
                  <td>
                    Issued {member.issue_date}
                    <br />
                    Original due {member.original_due_date}
                  </td>
                  <td>
                    Opening {money(member.opening_minor, cohort)}
                    <br />
                    Original {money(member.original_minor, cohort)}
                    <p>
                      Opening source {member.opening_source_id ?? "Unavailable"}
                    </p>
                    <p>Watermark {member.source_watermark}</p>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
      {prior.length ? (
        <details>
          <summary>Inspect prior result versions ({prior.length})</summary>
          {prior.map((result) => (
            <CohortResult key={result.id} result={result} cohort={cohort} />
          ))}
        </details>
      ) : (
        <p className="muted">No prior result versions are recorded.</p>
      )}
    </article>
  );
}
export function CohortPair({
  pair,
  workspace,
}: {
  pair: CohortPairView;
  workspace: string;
}) {
  return (
    <section className="cohort-pair" aria-label={`Cohort pair ${pair.id}`}>
      <h3>{pair.label ?? "Illustrative — Synthetic Data"}</h3>
      <p className="notice">
        {pair.caveat ??
          "Fixed synthetic illustration. Differences do not establish that ACO caused better payment outcomes."}
      </p>
      {pair.comparison_allowed ? (
        <dl className="detail-grid cohort-comparison">
          <div>
            <dt>Cash recovery change</dt>
            <dd>
              {pair.recovery_change_pp === null
                ? "Insufficient data"
                : `${signed(pair.recovery_change_pp)} percentage points`}
            </dd>
            <p>
              {pair.recovery_relative_change_percent === null
                ? "Relative change unavailable"
                : `${signed(pair.recovery_relative_change_percent)}% relative`}
            </p>
          </div>
          <div>
            <dt>Paid-in-full change</dt>
            <dd>
              {pair.paid_in_full_change_pp === null
                ? "Insufficient data"
                : `${signed(pair.paid_in_full_change_pp)} percentage points`}
            </dd>
          </div>
        </dl>
      ) : (
        <div className="notice">
          <strong>Comparison unavailable.</strong>
          <p>
            Both complete matched result sets are required; no change is
            inferred.
          </p>
          <ul>
            {pair.unmatched_strata.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}
      <p className="muted">
        Pair {pair.id} · Accounting source {pair.source_connection_id}
      </p>
      <div className="cohort-sides">
        {pair.cohorts.map((cohort) => (
          <CohortSide key={cohort.id} cohort={cohort} workspace={workspace} />
        ))}
      </div>
    </section>
  );
}
const changed = "aco:cohort-review";
function subscribe(callback: () => void) {
  window.addEventListener(changed, callback);
  window.addEventListener("storage", callback);
  return () => {
    window.removeEventListener(changed, callback);
    window.removeEventListener("storage", callback);
  };
}
function IllustrativeCohortPanel({
  workspace,
  csrfToken,
}: {
  workspace: string;
  csrfToken?: string;
}) {
  const cache = useQueryClient();
  const storageKey = `aco:cohort-source-review:${workspace}`;
  const raw = useSyncExternalStore(
    subscribe,
    () => {
      try {
        return sessionStorage.getItem(storageKey);
      } catch {
        return null;
      }
    },
    () => null,
  );
  const pending = readPendingCohortReview(raw);
  const [evidence, setEvidence] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const query = useQuery({
    queryKey: ["impact-cohorts", workspace],
    queryFn: ({ signal }) => cohortApi.read(workspace, signal),
    retry: false,
  });
  function save(value: PendingCohortReview | null) {
    if (value) sessionStorage.setItem(storageKey, JSON.stringify(value));
    else sessionStorage.removeItem(storageKey);
    window.dispatchEvent(new Event(changed));
  }
  function capture(kind: "seed" | "refresh", pair?: CohortPairView) {
    try {
      const review = captureCohortReview(
        kind,
        evidence,
        crypto.randomUUID(),
        pair,
      );
      save({ review, phase: "captured" });
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "This browser could not retain the captured review. No source action was submitted.",
      );
    }
  }
  async function submit() {
    if (!pending || !csrfToken || pending.phase === "conflict") return;
    setBusy(true);
    setError("");
    try {
      // Persist before the request; the same UUID/material survive unknown outcomes and reloads.
      save(pending);
      const result = await cohortApi.submit(
        workspace,
        pending.review,
        csrfToken,
      );
      cache.setQueryData(["impact-cohorts", workspace], result);
      save(null);
      setEvidence("");
      await Promise.all(
        [
          "impact",
          "operations",
          "receivables",
          "invoice",
          "customer",
          "usage",
        ].map((key) => cache.invalidateQueries({ queryKey: [key, workspace] })),
      );
    } catch (reason) {
      const message =
        reason instanceof Error
          ? reason.message
          : "The source action could not be confirmed. Retry the same captured request or reload saved results.";
      try {
        save({
          ...pending,
          phase:
            reason instanceof CohortCommandError && reason.status < 500
              ? "conflict"
              : "uncertain",
        });
        setError(message);
      } catch {
        setError(
          `${message} This browser could not save the latest outcome; the original captured request is still the retry basis.`,
        );
      }
    } finally {
      setBusy(false);
    }
  }
  const allowed =
    !!csrfToken && !!(query.data?.can_seed || query.data?.can_refresh);
  return (
    <section
      className="panel cohort-panel"
      aria-label="Frozen synthetic cohorts"
    >
      <h2>Illustrative before/after cohorts</h2>
      <p>
        Frozen matched source histories have their own due-date populations,
        30-day horizons and as-of times. They are separate from the current
        cash/activity reporting window above.
      </p>
      {query.isPending ? (
        <p role="status">Loading frozen cohort evidence…</p>
      ) : query.isError ? (
        <ErrorState
          label="Frozen cohorts unavailable"
          error={query.error}
          retry={() => query.refetch()}
        />
      ) : (
        <>
          {query.data.unavailable_reason && (
            <p>{query.data.unavailable_reason}</p>
          )}
          {query.data.pairs.map((pair) => (
            <CohortPair key={pair.id} pair={pair} workspace={workspace} />
          ))}
          {!pending && allowed && (
            <div className="cohort-source-review">
              <label htmlFor="cohort-evidence">Source action review note</label>
              <textarea
                id="cohort-evidence"
                minLength={10}
                maxLength={1000}
                value={evidence}
                onChange={(event) => setEvidence(event.target.value)}
              />
              <p>
                Only the current eligible Owner can create the fixed
                illustrative source history or explicitly refresh its source. No
                real customer data, payment claim or causal improvement is
                created.
              </p>
              {query.data.can_seed && (
                <button
                  type="button"
                  disabled={evidence.trim().length < 10}
                  onClick={() => capture("seed")}
                >
                  Review synthetic cohort creation
                </button>
              )}
              {query.data.can_refresh &&
                query.data.pairs.map((pair) => (
                  <button
                    key={pair.id}
                    type="button"
                    disabled={evidence.trim().length < 10}
                    onClick={() => capture("refresh", pair)}
                  >
                    Review source refresh
                  </button>
                ))}
            </div>
          )}
        </>
      )}
      {pending && (
        <section
          className="cohort-captured"
          aria-label="Captured cohort source review"
        >
          <h3>
            {pending.review.kind === "seed"
              ? "Create fixed illustrative synthetic history"
              : "Refresh the reviewed source snapshots"}
          </h3>
          <p className="task-notes">{pending.review.evidence}</p>
          {pending.review.kind === "seed" ? (
            <p>
              This imports the fixed synthetic accounting histories and freezes
              their matched cohort results. It does not claim a real customer
              payment or ACO-caused improvement.
            </p>
          ) : (
            <ul>
              {pending.review.reviewed.map((row) => (
                <li key={row.id}>
                  {sideLabel(row.side)} · Version {row.version} · As of{" "}
                  {stamp(row.as_of_at)}
                  <p>Reviewed result {row.id}</p>
                </li>
              ))}
            </ul>
          )}
          <details>
            <summary>Inspect captured request references</summary>
            <p>Request {pending.review.request_id}</p>
            <p>Pair {pending.review.pair_id ?? "New fixed synthetic pair"}</p>
          </details>
          {pending.phase === "uncertain" && (
            <p className="notice">
              This request has an unconfirmed outcome. Reload reads or retry the
              same captured request; its reviewed references have not changed.
            </p>
          )}
          {pending.phase === "conflict" && (
            <p className="notice">
              The captured review cannot be submitted again. Reload saved
              results, then explicitly discard it and review a new source
              action.
            </p>
          )}
          <button
            type="button"
            disabled={busy || !allowed || pending.phase === "conflict"}
            onClick={() => void submit()}
          >
            {busy
              ? "Reading and freezing source…"
              : pending.phase === "uncertain"
                ? "Retry same captured source request"
                : pending.review.kind === "seed"
                  ? "Create illustrative cohorts"
                  : "Refresh reviewed cohort source"}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              try {
                save(null);
                setError("");
                void query.refetch();
              } catch {
                setError(
                  "This browser could not discard the captured review. No source action was submitted.",
                );
              }
            }}
          >
            Discard captured review
          </button>
        </section>
      )}
      {error && <p role="alert">{error}</p>}
      <button
        type="button"
        disabled={query.isFetching || busy}
        onClick={() => void query.refetch()}
      >
        Reload saved cohorts
      </button>
    </section>
  );
}

export function CohortPanel(props: { workspace: string; csrfToken?: string }) {
  return (
    <>
      <IllustrativeCohortPanel {...props} />
      <ObservedCohortPanel {...props} />
    </>
  );
}
