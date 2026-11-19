"use client";
import Link from "next/link";
import { useRef, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import type {
  EconomicsAssumptionView,
  EconomicsGroup,
  EconomicsView,
  UsageCostEvidence,
} from "@/lib/generated-api";
import { ErrorState, stamp } from "@/features/receivables/shared";
import type { ImpactWindow } from "./api";
import {
  allocationCommand,
  approvalCommand,
  canApproveEconomics,
  captureAllocation,
  captureEconomicsReview,
  economicsApi,
  sameAllocation,
  type EconomicsReview,
} from "./economics-api";
const decimal = (value: string | null | undefined, unit = "") =>
  value == null ? "Insufficient data" : `${value}${unit}`;
const note = (reason: unknown) =>
  reason instanceof Error
    ? reason.message
    : "Result not confirmed. Refresh saved records.";

export function AssumptionRecord({ row }: { row: EconomicsAssumptionView }) {
  const c = row.command;
  return (
    <section
      aria-label={`Assumptions version ${row.version}`}
      style={{ overflowWrap: "anywhere" }}
    >
      <h4>
        Version {row.version} ·{" "}
        {row.approval_id ? "Owner approved" : "Draft — unapproved"}
      </h4>
      <p>
        {c.synthetic ? "Illustrative synthetic basis" : "Observed record basis"}{" "}
        · {c.currency} · {stamp(c.since)} to {stamp(c.until)}
      </p>
      <dl>
        <dt>Baseline minutes per eligible case</dt>
        <dd>{c.baseline_minutes_per_issue}</dd>
        <dt>Assumed wage per hour</dt>
        <dd>
          {c.wage_per_hour} {c.currency}
        </dd>
        <dt>Explicit fixed allocation</dt>
        <dd>{decimal(c.fixed_cost, ` ${c.currency}`)}</dd>
      </dl>
      <p>{c.evidence}</p>
      <p>{c.fixed_cost_evidence ?? "No fixed-cost allocation evidence."}</p>
      <p>
        Original assumption {row.id} · saved {stamp(row.created_at)} · reviewed
        hash {row.reviewed_hash}
      </p>
      {row.approval_id && (
        <p>
          Approval {row.approval_id} · Owner {row.approved_by} ·{" "}
          {stamp(row.approved_at)} · {row.approval_evidence}
        </p>
      )}
    </section>
  );
}
function EconomicsSummary({
  group,
  workspace,
}: {
  group: EconomicsGroup;
  workspace: string;
}) {
  return (
    <section
      className="cohort-side"
      aria-label={`${group.synthetic ? "Synthetic" : "Observed"} ${group.currency} operating estimate`}
    >
      <h4>
        {group.synthetic ? "Synthetic" : "Observed"} · {group.currency}
      </h4>
      <p>
        {group.eligible_case_count} eligible cases · {group.resolved_case_count}{" "}
        genuinely resolved cases
      </p>
      <p>{group.human_time_basis} · measured labour remains unavailable.</p>
      <dl className="cohort-metrics">
        <div>
          <dt>Known recorded provider subtotal</dt>
          <dd>{decimal(group.known_provider_cost, ` ${group.currency}`)}</dd>
        </div>
        <div>
          <dt>Reported human minutes</dt>
          <dd>{decimal(group.reported_human_minutes)}</dd>
        </div>
        <div>
          <dt>Estimated reported-labour component</dt>
          <dd>
            {decimal(group.estimated_reported_human_cost, ` ${group.currency}`)}
          </dd>
        </div>
        <div>
          <dt>Complete measured cost per resolved case</dt>
          <dd>
            {decimal(
              group.costs.complete_cost_per_resolution,
              ` ${group.currency}`,
            )}
          </dd>
        </div>
        <div>
          <dt>Modelled cost per resolved case · ESTIMATE</dt>
          <dd>
            {decimal(group.estimated_cost_per_resolution, ` ${group.currency}`)}
          </dd>
        </div>
        <div>
          <dt>Estimated capacity minutes</dt>
          <dd>{decimal(group.capacity.estimated_time_saved_minutes)}</dd>
        </div>
        <div>
          <dt>Estimated capacity value</dt>
          <dd>
            {decimal(
              group.capacity.estimated_capacity_value,
              ` ${group.currency}`,
            )}
          </dd>
        </div>
        <div>
          <dt>Estimated net benefit</dt>
          <dd>
            {decimal(
              group.capacity.estimated_net_benefit,
              ` ${group.currency}`,
            )}
          </dd>
        </div>
        <div>
          <dt>Incremental cost denominator</dt>
          <dd>
            {decimal(
              group.capacity.incremental_cost_denominator,
              ` ${group.currency}`,
            )}
          </dd>
        </div>
        <div>
          <dt>Modelled ROI · {group.capacity.label}</dt>
          <dd>{decimal(group.capacity.estimated_roi_percent, "%")}</dd>
        </div>
      </dl>
      <p>
        Capacity is an assumption-based estimate, not realized payroll savings
        or a causal benefit. Negative estimates remain negative.
      </p>
      <ul>
        {group.reasons.map((reason) => (
          <li key={reason}>{reason}</li>
        ))}
      </ul>
      <details>
        <summary>Original denominator and handling sources</summary>
        <ul>
          {group.case_ids.map((id) => (
            <li key={id}>
              <Link
                href={`/cases/${encodeURIComponent(id)}?workspace=${encodeURIComponent(workspace)}`}
              >
                Case {id}
              </Link>
            </li>
          ))}
        </ul>
        <p>
          Handling entries:{" "}
          {group.handling_entry_ids.length
            ? group.handling_entry_ids.join(", ")
            : "Unavailable"}
        </p>
        <p>Assumptions: {group.assumptions_id ?? "Unapproved / unavailable"}</p>
      </details>
    </section>
  );
}
export function UsageCostRecord({
  event,
  workspace,
}: {
  event: UsageCostEvidence;
  workspace: string;
}) {
  return (
    <details style={{ overflowWrap: "anywhere" }}>
      <summary>
        {event.provider} · {event.component} ·{" "}
        {decimal(event.amount, ` ${event.currency ?? "currency unavailable"}`)}{" "}
        · {event.quality}
      </summary>
      <p>
        Event {event.event_id} · revision {event.revision_id ?? "Unavailable"} ·
        original operation {event.operation_key} · connection{" "}
        {event.provider_connection_id}
      </p>
      <p>
        {event.synthetic
          ? "Synthetic provider evidence"
          : "Observed provider evidence"}{" "}
        ·{" "}
        {event.additive
          ? "Top-level charge"
          : "Contained component — excluded from additive subtotal"}{" "}
        · {stamp(event.created_at)}
      </p>
      <p>
        Canonical allocation{" "}
        {event.allocation.id ?? "Unsaved source-backed preview"} ·{" "}
        {event.allocation.basis} · version {event.allocation.version} · saved
        revision {event.allocation.basis_revision_id ?? "Not yet captured"}
      </p>
      {!event.allocation.current_source_matches && (
        <p role="status">
          Current source links changed. This retained allocation needs explicit
          review.
        </p>
      )}
      <ul>
        {event.allocation.leaves.map((leaf, index) => (
          <li key={`${leaf.case_id}:${leaf.invoice_id}:${index}`}>
            <span>Weight {leaf.weight} · </span>
            {leaf.case_id && (
              <Link
                href={`/cases/${encodeURIComponent(leaf.case_id)}?workspace=${encodeURIComponent(workspace)}`}
              >
                Case {leaf.case_id}
              </Link>
            )}{" "}
            {leaf.invoice_id ? (
              <Link
                href={`/receivables/${encodeURIComponent(leaf.invoice_id)}?workspace=${encodeURIComponent(workspace)}`}
              >
                Invoice {leaf.invoice_id}
              </Link>
            ) : (
              "Unallocated — no supported original linkage"
            )}
          </li>
        ))}
      </ul>
      <p>
        Original source references:{" "}
        {event.allocation.source_ids.join(", ") || "None — unallocated"}
      </p>
      <p>{event.allocation.evidence}</p>
    </details>
  );
}
export function EconomicsReport({
  view,
  workspace,
}: {
  view: EconomicsView;
  workspace: string;
}) {
  return (
    <section aria-label="Recorded operating economics">
      <h3>Operating economics · recorded evidence</h3>
      <p>
        {view.source_basis} · current reconciliation {stamp(view.as_of_at)}
      </p>
      <p>{view.caveat}</p>
      {view.groups.length ? (
        view.groups.map((group) => (
          <EconomicsSummary
            key={`${group.currency}:${group.synthetic}`}
            group={group}
            workspace={workspace}
          />
        ))
      ) : (
        <p>
          Insufficient data. No priced recorded usage or approved assumptions
          for this window.
        </p>
      )}
      <details>
        <summary>
          Original charges and canonical allocations ({view.events.length})
        </summary>
        {view.events.map((event) => (
          <UsageCostRecord
            key={event.event_id}
            event={event}
            workspace={workspace}
          />
        ))}
        {view.usage.bundle_issues.map((issue, index) => (
          <p key={`${issue.event_id}:${index}`}>
            {issue.reason}: component {issue.event_id}, parent{" "}
            {issue.parent_event_id ?? "unavailable"}; known component{" "}
            {issue.component_amount ?? "unknown"} {issue.currency}.
          </p>
        ))}
      </details>
      <details>
        <summary>
          Case and invoice allocation views — do not add together
        </summary>
        {(["case_costs", "invoice_costs"] as const).map((kind) => (
          <section key={kind}>
            <h4>{kind === "case_costs" ? "Case rollup" : "Invoice rollup"}</h4>
            <ul>
              {view[kind].map((row) => (
                <li key={`${row.target_id}:${row.currency}:${row.synthetic}`}>
                  {row.target_id ?? "Unallocated"} · {row.known_amount}{" "}
                  {row.currency ?? "unknown currency"} known subtotal ·{" "}
                  {row.unknown_event_count} unknown events ·{" "}
                  {row.synthetic ? "synthetic" : "observed"}
                </li>
              ))}
            </ul>
          </section>
        ))}
      </details>
      <details>
        <summary>Saved assumption history ({view.assumptions.length})</summary>
        {view.assumptions.map((row) => (
          <AssumptionRecord key={row.id} row={row} />
        ))}
      </details>
    </section>
  );
}

function AssumptionsEditor({
  view,
  window,
  workspace,
  csrfToken,
  fresh,
  refresh,
}: {
  view: EconomicsView;
  window: ImpactWindow;
  workspace: string;
  csrfToken: string;
  fresh: boolean;
  refresh: () => Promise<unknown>;
}) {
  const [baseline, setBaseline] = useState("10"),
    [wage, setWage] = useState("60"),
    [currency, setCurrency] = useState("AUD"),
    [synthetic, setSynthetic] = useState(true),
    [fixed, setFixed] = useState(""),
    [fixedEvidence, setFixedEvidence] = useState(""),
    [evidence, setEvidence] = useState(""),
    [reason, setReason] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [review, setReview] = useState<EconomicsReview | null>(null),
    [confirmed, setConfirmed] = useState(false);
  const saveRequest = useRef<{ body: string; id: string } | null>(null),
    decisionRequest = useRef<string | null>(null);
  const current = view.assumptions.find(
    (row) => row.id === view.latest_assumption_id,
  );
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!fresh || busy) return;
    setBusy(true);
    setError("");
    const fields = {
      expected_previous_version_id: view.latest_assumption_id,
      ...window,
      currency,
      synthetic,
      baseline_minutes_per_issue: baseline,
      wage_per_hour: wage,
      fixed_cost: fixed.trim() || null,
      fixed_cost_evidence: fixed.trim() ? fixedEvidence : null,
      evidence,
    };
    const body = JSON.stringify(fields);
    if (saveRequest.current?.body !== body)
      saveRequest.current = { body, id: crypto.randomUUID() };
    try {
      await economicsApi.save(
        workspace,
        { request_id: saveRequest.current.id, ...fields },
        csrfToken,
      );
      setReview(null);
      setConfirmed(false);
    } catch (reason) {
      setError(note(reason));
    } finally {
      await refresh();
      setBusy(false);
    }
  }
  async function approve() {
    if (
      !confirmed ||
      !canApproveEconomics(review, view, fresh) ||
      !review ||
      busy
    )
      return;
    setBusy(true);
    setError("");
    decisionRequest.current ??= crypto.randomUUID();
    try {
      await economicsApi.approve(
        workspace,
        review.assumption.id,
        approvalCommand(review, decisionRequest.current, reason),
        csrfToken,
      );
      setReview(null);
      setConfirmed(false);
    } catch (reason) {
      setError(note(reason));
    } finally {
      await refresh();
      setBusy(false);
    }
  }
  return (
    <section className="panel" aria-label="Owner economics assumptions">
      <h3>Review operating assumptions</h3>
      <p>
        10 minutes and AUD 60/hour are illustrative suggestions. Saving creates
        an unapproved draft; review its actual saved values before approval.
      </p>
      <form onSubmit={save}>
        <label>
          Baseline minutes per eligible case
          <input
            inputMode="decimal"
            value={baseline}
            onChange={(event) => setBaseline(event.target.value)}
            required
          />
        </label>
        <label>
          Assumed wage per hour
          <input
            inputMode="decimal"
            value={wage}
            onChange={(event) => setWage(event.target.value)}
            required
          />
        </label>
        <label>
          Assumption currency
          <input
            value={currency}
            onChange={(event) => setCurrency(event.target.value.toUpperCase())}
            pattern="[A-Z]{3}"
            required
          />
        </label>
        <label>
          <input
            type="checkbox"
            checked={synthetic}
            onChange={(event) => setSynthetic(event.target.checked)}
          />
          Synthetic evidence basis
        </label>
        <label>
          Fixed-cost allocation (blank means unknown)
          <input
            inputMode="decimal"
            value={fixed}
            onChange={(event) => setFixed(event.target.value)}
          />
        </label>
        <label>
          Fixed-cost evidence, including explicit zero
          <textarea
            value={fixedEvidence}
            onChange={(event) => setFixedEvidence(event.target.value)}
            minLength={10}
            required={fixed.trim() !== ""}
          />
        </label>
        <label>
          Assumption provenance
          <textarea
            value={evidence}
            onChange={(event) => setEvidence(event.target.value)}
            minLength={10}
            required
          />
        </label>
        <p>
          Captured period: {stamp(window.since)} to {stamp(window.until)}. No
          financial source or provider charge is edited.
        </p>
        <button disabled={!fresh || busy} type="submit">
          Save assumption draft
        </button>
      </form>
      {current && !current.approval_id && (
        <button
          type="button"
          disabled={!fresh || busy}
          onClick={() => {
            setReview(
              captureEconomicsReview(current, view.current_approval_id),
            );
            setConfirmed(false);
            decisionRequest.current = null;
          }}
        >
          Review saved assumptions
        </button>
      )}
      {review && (
        <section aria-label="Captured economics review">
          <AssumptionRecord row={review.assumption} />
          <label>
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            I reviewed these saved assumptions, exact window and
            synthetic/currency basis.
          </label>
          <label>
            Owner approval evidence
            <textarea
              value={reason}
              onChange={(event) => {
                setReason(event.target.value);
                decisionRequest.current = null;
              }}
              minLength={10}
            />
          </label>
          {!canApproveEconomics(review, view, fresh) && (
            <p>
              The captured review is no longer current. Refresh and explicitly
              review again.
            </p>
          )}
          <button
            type="button"
            disabled={
              !confirmed ||
              reason.trim().length < 10 ||
              busy ||
              !canApproveEconomics(review, view, fresh)
            }
            onClick={approve}
          >
            Approve reviewed assumptions
          </button>
        </section>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}

function AllocationEditor({
  view,
  workspace,
  csrfToken,
  fresh,
  refresh,
}: {
  view: EconomicsView;
  workspace: string;
  csrfToken: string;
  fresh: boolean;
  refresh: () => Promise<unknown>;
}) {
  const [review, setReview] = useState<UsageCostEvidence | null>(null),
    [evidence, setEvidence] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const request = useRef<string | null>(null);
  const current = view.events.find(
    (event) => event.event_id === review?.event_id,
  );
  const valid = !!review && fresh && sameAllocation(review, current);
  async function save() {
    if (!review || !valid || busy) return;
    setBusy(true);
    setError("");
    request.current ??= crypto.randomUUID();
    try {
      await economicsApi.allocate(
        workspace,
        review.event_id,
        allocationCommand(review, request.current, evidence),
        csrfToken,
      );
      setReview(null);
    } catch (reason) {
      setError(note(reason));
    } finally {
      await refresh();
      setBusy(false);
    }
  }
  return (
    <details>
      <summary>Owner review of canonical cost allocations</summary>
      <p>
        Each original component has one allocation set. Equal shares cover all
        verified leaves; unsupported source linkage stays unallocated. Other
        weights require an exact reviewed complete set through the same API.
      </p>
      <ul>
        {view.events.map((event) => (
          <li key={event.event_id}>
            {event.provider} · {event.component} · {event.event_id}{" "}
            <button
              type="button"
              disabled={!fresh || busy || !event.revision_id}
              onClick={() => {
                setReview(captureAllocation(event));
                setEvidence("");
                request.current = null;
              }}
            >
              Review allocation {event.event_id}
            </button>
          </li>
        ))}
      </ul>
      {review && (
        <section aria-label="Captured allocation review">
          <UsageCostRecord event={review} workspace={workspace} />
          <p>Current verified equal-share proposal:</p>
          <ul>
            {review.allocation.supported_leaves.map((leaf, index) => (
              <li key={index}>
                {leaf.case_id ?? "No case"} / {leaf.invoice_id ?? "Unallocated"}{" "}
                · {leaf.weight}
              </li>
            ))}
          </ul>
          <label>
            Allocation review evidence
            <textarea
              value={evidence}
              onChange={(event) => {
                setEvidence(event.target.value);
                request.current = null;
              }}
              minLength={10}
            />
          </label>
          {!valid && (
            <p>
              Source, charge revision or allocation changed. Review the latest
              source before saving.
            </p>
          )}
          <button
            type="button"
            disabled={!valid || busy || evidence.trim().length < 10}
            onClick={save}
          >
            Save reviewed allocation
          </button>
        </section>
      )}
      {error && <p role="alert">{error}</p>}
    </details>
  );
}
export function EconomicsPanel({
  workspace,
  window,
  csrfToken,
}: {
  workspace: string;
  window: ImpactWindow | null;
  csrfToken?: string;
}) {
  const query = useQuery({
    queryKey: ["economics", workspace, window],
    queryFn: ({ signal }) => economicsApi.read(workspace, window!, signal),
    enabled: !!window,
    retry: false,
  });
  if (!window)
    return (
      <section className="panel">
        <h2>Operating economics</h2>
        <p>
          Apply a reporting window to read recorded costs and reviewed
          assumptions.
        </p>
      </section>
    );
  if (query.isPending) return <p role="status">Loading operating evidence…</p>;
  if (query.isError)
    return (
      <ErrorState
        label="Operating evidence unavailable"
        error={query.error}
        retry={() => query.refetch()}
      />
    );
  return (
    <section className="panel" aria-label="Operating economics workflow">
      <div className="section-heading">
        <h2>Operating economics</h2>
        <button
          type="button"
          disabled={query.isFetching}
          onClick={() => query.refetch()}
        >
          Refresh operating evidence
        </button>
      </div>
      <EconomicsReport view={query.data} workspace={workspace} />
      {query.data.can_manage && csrfToken && (
        <>
          <AssumptionsEditor
            key={`${workspace}:${window.since}:${window.until}`}
            view={query.data}
            window={window}
            workspace={workspace}
            csrfToken={csrfToken}
            fresh={!query.isFetching}
            refresh={() => query.refetch()}
          />
          <AllocationEditor
            view={query.data}
            workspace={workspace}
            csrfToken={csrfToken}
            fresh={!query.isFetching}
            refresh={() => query.refetch()}
          />
        </>
      )}
    </section>
  );
}
