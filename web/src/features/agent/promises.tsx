"use client";
import Link from "next/link";
import { InvoiceRef } from "@/lib/invoice-numbers";
import { VoiceBusinessSource } from "@/features/conversations/voice-business-source";
import { useState } from "react";
import type { WorkspaceView } from "@/lib/generated-api";
import { useQuery } from "@tanstack/react-query";
import { PromiseAmendmentPanel } from "./promise-capture";
import { ErrorState, stamp } from "@/features/receivables/shared";
import { formatMoney } from "@/lib/money";
import {
  promiseApi,
  samePromiseReview,
  type PaymentPromise,
  type PromiseCheckPlan,
  type PromiseSchedulingStatus,
} from "./business-api";
const schedulingLabels: Record<PromiseSchedulingStatus, string> = {
  PENDING_ENGINE: "Pending engine",
  PENDING_CONFIRMATION: "Pending confirmation",
  QUEUED: "Queued",
  RUNNING: "Running",
  COMPLETED: "Completed",
  REVIEW_REQUIRED: "Review required",
};
const verificationLabels = {
  UNVERIFIED: "Not yet verified",
  VERIFIED: "Source verification recorded",
  PENDING_REVIEW: "Verification pending review",
};
const checking = (status: PromiseSchedulingStatus) =>
  status === "QUEUED" || status === "RUNNING";
export function InvoicePromises({
  workspace,
  invoiceId,
}: {
  workspace: string;
  invoiceId: string;
}) {
  const query = useQuery({
    queryKey: ["promises", workspace, invoiceId],
    queryFn: ({ signal }) => promiseApi.list(workspace, invoiceId, signal),
    retry: false,
  });
  return (
    <section className="panel">
      <h3>Payment promises</h3>
      {query.isPending ? (
        <p role="status">Loading linked promises…</p>
      ) : query.isError ? (
        <ErrorState
          label="Promises unavailable"
          error={query.error}
          retry={() => query.refetch()}
        />
      ) : !query.data.items.length ? (
        <p>No payment promises are linked to this invoice.</p>
      ) : (
        <ul className="agent-record-list">
          {query.data.items.map((record) => (
            <li key={record.id}>
              <Link
                href={`/promises/${encodeURIComponent(record.id)}?workspace=${encodeURIComponent(workspace)}`}
              >
                Promise {record.id.slice(0, 8)}
              </Link>
              <p>
                {record.amount
                  ? formatMoney(record.amount)
                  : "Amount not confirmed"}{" "}
                · {record.due_date ?? "Date not confirmed"}
              </p>
              <p className="muted">
                {record.status.replaceAll("_", " ")} ·{" "}
                {record.synthetic ? "Synthetic" : "Observed"}
              </p>
              {record.needs_confirmation && <p>Needs confirmation</p>}
            </li>
          ))}
        </ul>
      )}
      <p className="muted">
        A promise is a commitment, not a verified payment.
      </p>
    </section>
  );
}
export function PromiseDetail({
  workspace,
  id,
  currentWorkspace,
  csrfToken,
}: {
  workspace: string;
  id: string;
  currentWorkspace?: WorkspaceView;
  csrfToken?: string;
}) {
  const query = useQuery({
    queryKey: ["promise", workspace, id],
    queryFn: ({ signal }) => promiseApi.get(workspace, id, signal),
    retry: false,
    refetchInterval: (state) =>
      state.state.data && checking(state.state.data.scheduling_status)
        ? 5000
        : false,
  });
  const lineage = useQuery({
    queryKey: [
      "promise-lineage",
      workspace,
      id,
      query.data?.replacement_lineage,
    ],
    queryFn: ({ signal }) =>
      Promise.all(
        (query.data?.replacement_lineage ?? []).map((snapshot) =>
          promiseApi.get(workspace, snapshot.id, signal),
        ),
      ),
    enabled: !!query.data?.replacement_lineage?.length,
    retry: false,
  });
  const original = useQuery({
    queryKey: ["promise", workspace, query.data?.replaces_promise_id],
    queryFn: ({ signal }) =>
      promiseApi.get(workspace, query.data!.replaces_promise_id!, signal),
    enabled: !!query.data?.replaces_promise_id,
    retry: false,
  });
  const hasChecks =
    !!query.data &&
    ["ACTIVE", "FULFILLED", "MISSED"].includes(query.data.status);
  const plans = useQuery({
    queryKey: ["promise-check-plans", workspace, id, query.data?.version],
    queryFn: ({ signal }) => promiseApi.checkPlans(workspace, id, signal),
    enabled: hasChecks,
    retry: false,
    refetchInterval: (state) =>
      state.state.data?.some((plan) => checking(plan.status)) ? 5000 : false,
  });
  return query.isPending ? (
    <p role="status">Loading promise evidence…</p>
  ) : query.isError ? (
    <ErrorState
      label="Promise unavailable"
      error={query.error}
      retry={() => query.refetch()}
    />
  ) : (
    <>
      <button
        type="button"
        disabled={query.isFetching || plans.isFetching}
        onClick={() => {
          void query.refetch();
          if (query.data?.replaces_promise_id) void original.refetch();
          if (query.data?.replacement_lineage?.length) void lineage.refetch();
          if (hasChecks) void plans.refetch();
        }}
      >
        Refresh saved promise
      </button>
      <PromiseRecord record={query.data} workspace={workspace} />
      {currentWorkspace?.permissions.includes("resolve") && (
        <PromiseDecisionControls
          key={`${query.data.id}:${query.data.version}`}
          workspace={workspace}
          record={query.data}
          replacement={original.data}
          lineage={lineage.data ?? []}
          replacementLoading={
            (!!query.data.replacement_lineage?.length &&
              (lineage.isPending || lineage.isFetching || lineage.isError)) ||
            (!!query.data.replaces_promise_id &&
              (original.isPending || original.isFetching || original.isError))
          }
          csrfToken={csrfToken ?? ""}
          onChanged={async () => {
            await query.refetch();
            if (hasChecks) await plans.refetch();
          }}
        />
      )}
      {query.data.replaces_promise_id && original.isError && (
        <ErrorState
          label="Original promise unavailable"
          error={original.error}
          retry={() => original.refetch()}
        />
      )}
      {currentWorkspace && (
        <PromiseAmendmentPanel
          workspace={currentWorkspace}
          record={query.data}
          csrfToken={csrfToken ?? ""}
        />
      )}
      {hasChecks && (
        <section className="panel" aria-label="Saved promise checks">
          <h2>Saved source checks</h2>
          <p>
            A completed check is a worker outcome. Commitment performance is
            shown separately in verification and history.
          </p>
          {plans.isPending ? (
            <p role="status">Loading saved source checks…</p>
          ) : plans.isError ? (
            <ErrorState
              label="Source check plans unavailable"
              error={plans.error}
              retry={() => plans.refetch()}
            />
          ) : (
            <PromiseCheckPlans plans={plans.data} workspace={workspace} />
          )}
        </section>
      )}
    </>
  );
}
export function PromiseRecord({
  record,
  workspace,
}: {
  record: PaymentPromise;
  workspace: string;
}) {
  const suffix = `?workspace=${encodeURIComponent(workspace)}`;
  return (
    <div className="agent-business-record">
      <section className="panel">
        <p className="eyebrow">
          {record.synthetic ? "Synthetic promise" : "Observed promise"} ·
          Version {record.version}
        </p>
        <div className="section-heading">
          <h2>Payment commitment</h2>
          <span className="badge">{record.status.replaceAll("_", " ")}</span>
        </div>
        <p className="detail-balance">
          {record.amount ? formatMoney(record.amount) : "Amount not confirmed"}
        </p>
        <p className="muted">Promise reference: {record.id}</p>
        <p>
          A promise records what was committed. Financial payment status comes
          from accounting.
        </p>
        {record.needs_confirmation && (
          <div className="agent-notice">
            <strong>Needs confirmation</strong>
            <ul>
              {record.confirmation_reasons.map((reason) => (
                <li key={reason}>{reason.replaceAll("_", " ")}</li>
              ))}
            </ul>
          </div>
        )}
        <dl className="detail-grid">
          <div>
            <dt>Promised date</dt>
            <dd>{record.due_date ?? "Not confirmed"}</dd>
          </div>
          <div>
            <dt>Recipient timezone</dt>
            <dd>{record.timezone ?? "Not confirmed"}</dd>
          </div>
          <div>
            <dt>Exclusive cutoff</dt>
            <dd>{stamp(record.cutoff_at)}</dd>
          </div>
          <div>
            <dt>Confirmed</dt>
            <dd>{stamp(record.confirmed_at)}</dd>
          </div>
          <div>
            <dt>Created</dt>
            <dd>{stamp(record.created_at)}</dd>
          </div>
          <div>
            <dt>Updated</dt>
            <dd>{stamp(record.updated_at)}</dd>
          </div>
        </dl>
        <p>
          <strong>Check scheduling:</strong>{" "}
          {schedulingLabels[record.scheduling_status]}
        </p>
        <p>
          <strong>Source verification:</strong>{" "}
          {verificationLabels[record.verification_status]}
        </p>
        {record.verification_status === "PENDING_REVIEW" && (
          <p className="agent-notice">
            Prior commitment status is retained while current source
            verification is pending. No new fulfilled or missed result is
            established by this state.
          </p>
        )}
        <p>
          Checks use verified accounting allocations in the commitment interval,
          not invoice Paid status alone.
        </p>
        <p>
          <Link
            href={`/customers/${encodeURIComponent(record.customer_id)}${suffix}`}
          >
            Customer account
          </Link>
          {record.review_task_id && (
            <>
              {" "}
              ·{" "}
              <Link
                href={`/tasks/${encodeURIComponent(record.review_task_id)}${suffix}`}
              >
                Open review task
              </Link>
            </>
          )}
        </p>
        {record.replaces_promise_id && (
          <p>
            <Link
              href={`/promises/${encodeURIComponent(record.replaces_promise_id)}${suffix}`}
            >
              Prior commitment
            </Link>
          </p>
        )}
        {record.superseded_by_id && (
          <p>
            <Link
              href={`/promises/${encodeURIComponent(record.superseded_by_id)}${suffix}`}
            >
              Replacement commitment
            </Link>
          </p>
        )}
      </section>
      <section className="panel">
        <h2>Invoice scope &amp; allocations</h2>
        {record.invoice_links.length ? (
          <ul className="agent-record-list">
            {record.invoice_links.map((link) => (
              <li key={link.invoice_id}>
                <Link
                  href={`/receivables/${encodeURIComponent(link.invoice_id)}${suffix}`}
                >
                  <InvoiceRef id={link.invoice_id} />
                </Link>
                <p>
                  {link.amount
                    ? formatMoney(link.amount)
                    : "Allocation not confirmed"}
                </p>
                {link.hold_id && <p>Linked hold: {link.hold_id}</p>}
              </li>
            ))}
          </ul>
        ) : (
          <p>No confirmed invoice scope was returned.</p>
        )}
        {!!record.candidate_invoice_ids.length && (
          <>
            <h3>Candidate invoices · unconfirmed scope</h3>
            <ul>
              {record.candidate_invoice_ids.map((id) => (
                <li key={id}>
                  <Link
                    href={`/receivables/${encodeURIComponent(id)}${suffix}`}
                  >
                    <InvoiceRef id={id} />
                  </Link>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
      {!!record.installments.length && (
        <section className="panel">
          <h2>Installments</h2>
          <ol className="agent-record-list">
            {record.installments.map((item) => (
              <li key={item.id}>
                <strong>{formatMoney(item.amount)}</strong> · {item.due_date} ·{" "}
                {item.status.replaceAll("_", " ")}
                <p>Exclusive cutoff {stamp(item.cutoff_at)}</p>
                {item.allocations.length ? (
                  <ul>
                    {item.allocations.map((allocation) => (
                      <li key={allocation.invoice_id}>
                        <Link
                          href={`/receivables/${encodeURIComponent(allocation.invoice_id)}${suffix}`}
                        >
                          <InvoiceRef id={allocation.invoice_id} />
                        </Link>{" "}
                        · {formatMoney(allocation.amount)}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p>Invoice allocation not confirmed.</p>
                )}
              </li>
            ))}
          </ol>
        </section>
      )}
      <section className="panel">
        <h2>
          {record.voice_source
            ? "Captured commitment business facts"
            : "Original commitment evidence"}
        </h2>
        <p className="task-notes">{record.source_text}</p>
        <p className="muted">
          {record.synthetic ? "Synthetic" : "Observed"} ·{" "}
          {record.source_state.replaceAll("_", " ")} · {stamp(record.source_at)}{" "}
          ·{" "}
          {record.voice_source
            ? "Signed operational source"
            : `Offset ${record.source_offset}`}
        </p>
        {record.voice_source ? (
          <VoiceBusinessSource
            source={record.voice_source}
            workspace={workspace}
          />
        ) : record.source_communication_id ? (
          <Link
            href={`/communications/${encodeURIComponent(record.source_communication_id)}${suffix}`}
          >
            Open source message
          </Link>
        ) : (
          <p>Source evidence unavailable.</p>
        )}
        {record.confirmation_communication_id && (
          <p>
            <Link
              href={`/communications/${encodeURIComponent(record.confirmation_communication_id)}${suffix}`}
            >
              Open confirmation evidence
            </Link>
          </p>
        )}
      </section>
      <section className="panel">
        <h2>Promise history</h2>
        {record.history.length ? (
          <ol className="agent-record-list">
            {record.history.map((event) => (
              <li key={event.id}>
                <strong>{event.event.replaceAll("_", " ")}</strong>
                <p>
                  {stamp(event.created_at)} · Actor {event.actor_id}
                </p>
                <p className="muted">Immutable event {event.id}</p>
                <PromisePerformanceEvidence details={event.details} />
                <details>
                  <summary>Inspect recorded change</summary>
                  <pre>{JSON.stringify(event.details, null, 2)}</pre>
                </details>
              </li>
            ))}
          </ol>
        ) : (
          <p>No history entries were returned.</p>
        )}
      </section>
    </div>
  );
}

export function PromiseCheckPlans({
  plans,
  workspace,
}: {
  plans: PromiseCheckPlan[];
  workspace: string;
}) {
  if (!plans.length) return <p>No saved source check plans were returned.</p>;
  return (
    <ol className="agent-record-list">
      {plans.map((plan) => (
        <li key={`${plan.event_id}:${plan.installment_id ?? plan.due_at}`}>
          <strong>{schedulingLabels[plan.status]}</strong> · Due{" "}
          {stamp(plan.due_at)}
          <p>
            Promise {plan.promise_id} · Version {plan.promise_version}
          </p>
          <p>
            Confirmation event {plan.event_id} · Installment{" "}
            {plan.installment_id ?? "Not returned"}
          </p>
          <ul>
            {plan.invoice_ids.map((invoice) => (
              <li key={invoice}>
                <Link
                  href={`/receivables/${encodeURIComponent(invoice)}?workspace=${encodeURIComponent(workspace)}`}
                >
                  Invoice {invoice}
                </Link>
              </li>
            ))}
          </ul>
          {plan.action_ids.length ? (
            <details>
              <summary>
                Inspect saved action references ({plan.action_ids.length})
              </summary>
              <ul>
                {plan.action_ids.map((action) => (
                  <li key={action}>
                    <a
                      href={`/api/workspaces/${encodeURIComponent(workspace)}/scheduled-actions/${encodeURIComponent(action)}`}
                    >
                      Saved action data {action}
                    </a>
                  </li>
                ))}
              </ul>
            </details>
          ) : (
            <p>No saved action IDs were returned.</p>
          )}
        </li>
      ))}
    </ol>
  );
}
function PromisePerformanceEvidence({
  details,
}: {
  details: Record<string, unknown>;
}) {
  const refs = [
    "observation_ids",
    "allocation_ids",
    "excluded_allocation_ids",
    "reversal_ids",
  ] as const;
  return (
    <>
      {typeof details.performance === "string" && (
        <p>Recorded performance: {details.performance.replaceAll("_", " ")}</p>
      )}
      {typeof details.reason === "string" && (
        <p>Verification reason: {details.reason.replaceAll("_", " ")}</p>
      )}
      {typeof details.prior_status === "string" &&
        typeof details.status === "string" && (
          <p>
            Commitment status: {details.prior_status} → {details.status}
          </p>
        )}
      {refs.map((key) =>
        Array.isArray(details[key]) && details[key].length > 0 ? (
          <div key={key}>
            <strong>{key.replaceAll("_", " ")}</strong>
            <ul>
              {details[key]
                .filter((id): id is string => typeof id === "string")
                .map((id) => (
                  <li key={id}>{id}</li>
                ))}
            </ul>
          </div>
        ) : null,
      )}
      {details.source_failure !== null &&
        typeof details.source_failure === "object" && (
          <p>
            Failed source attempt recorded. Rejected source references are
            unverified and do not establish financial performance.
          </p>
        )}
    </>
  );
}

export function PromiseDecisionControls({
  workspace,
  record,
  csrfToken,
  onChanged,
  replacement,
  lineage = [],
  replacementLoading = false,
}: {
  workspace: string;
  record: PaymentPromise;
  replacement?: PaymentPromise;
  lineage?: PaymentPromise[];
  replacementLoading?: boolean;
  csrfToken: string;
  onChanged: () => Promise<void>;
}) {
  const [reason, setReason] = useState("");
  const [reviewed, setReviewed] = useState(false);
  const [captured, setCaptured] = useState<PaymentPromise | null>(null);
  const [capturedLineage, setCapturedLineage] = useState<PaymentPromise[]>([]);
  const lineageCurrent = (record.replacement_lineage ?? []).every((snapshot) =>
    lineage.some(
      (current) =>
        current.id === snapshot.id &&
        current.version === snapshot.version &&
        current.status === snapshot.status,
    ),
  );
  const termsResolved = !(record.confirmation_reasons ?? []).some(
    (reason) => reason !== "sender_authority_requires_confirmation",
  );
  const [capturedPrior, setCapturedPrior] = useState<
    PaymentPromise | undefined
  >(undefined);
  const reviewChanged =
    captured !== null &&
    (!samePromiseReview(captured, record) ||
      !lineageCurrent ||
      capturedLineage.some(
        (prior) =>
          !lineage.some((current) => samePromiseReview(prior, current)),
      ) ||
      (captured.replaces_promise_id !== null &&
        (!capturedPrior ||
          !replacement ||
          !samePromiseReview(capturedPrior, replacement))));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const confirmable =
    record.status === "NEEDS_CONFIRMATION" || record.status === "CAPTURED";
  if (!confirmable && record.status !== "ACTIVE") return null;
  async function perform(kind: "confirm" | "cancel") {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      if (kind === "confirm" && (!captured || reviewChanged))
        throw new Error(
          "The captured commitment changed. Review the current terms explicitly.",
        );
      if (kind === "confirm")
        await promiseApi.confirm(
          workspace,
          captured ?? record,
          reason,
          csrfToken,
          capturedPrior,
          capturedLineage,
        );
      else await promiseApi.cancel(workspace, record, reason, csrfToken);
      setReviewed(false);
      setCaptured(null);
      setCapturedPrior(undefined);
      setCapturedLineage([]);
      await onChanged();
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : "The result could not be confirmed. Refresh current records.",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="panel promise-controls"
      aria-label="Human commitment review"
    >
      <h2>Review this commitment</h2>
      <p>
        Review the original source, exact amount, date, timezone and invoice
        scope. Confirmation records explicit human review; payment still
        requires accounting evidence. Call business facts remain distinct from a
        transcript.
      </p>
      {record.replaces_promise_id && (
        <div className="agent-notice">
          <p>
            Confirmation proposes superseding original promise{" "}
            {record.replaces_promise_id}. Capture alone has not replaced it.
          </p>
          {replacementLoading ? (
            <p>Read the current original commitment before confirming.</p>
          ) : replacement ? (
            <>
              <p>
                Reviewed original version {replacement.version} ·{" "}
                {replacement.status} ·{" "}
                {replacement.amount
                  ? formatMoney(replacement.amount)
                  : "Amount unavailable"}{" "}
                · {replacement.due_date} · {replacement.timezone}.
              </p>
              <p>
                <Link
                  href={`/promises/${encodeURIComponent(replacement.id)}?workspace=${encodeURIComponent(workspace)}`}
                >
                  Inspect original commitment and evidence
                </Link>
              </p>
              <p>
                Replacement version {record.version}, its allocations and
                original statement are shown above. Confirmation checks both
                current versions atomically.
              </p>
            </>
          ) : (
            <p>
              Original commitment unavailable; amendment confirmation is
              blocked.
            </p>
          )}
        </div>
      )}
      {!!record.replacement_lineage?.length && (
        <section aria-label="Captured predecessor lineage">
          <h3>Review all predecessor commitments</h3>
          <p>
            Confirmation must preserve this captured lineage. It supersedes
            these predecessors only after the entire current chain and explicit
            terms pass review.
          </p>
          <ul>
            {record.replacement_lineage.map((snapshot) => {
              const current = lineage.find((item) => item.id === snapshot.id);
              return (
                <li key={snapshot.id}>
                  <Link
                    href={`/promises/${encodeURIComponent(snapshot.id)}?workspace=${encodeURIComponent(workspace)}`}
                  >
                    Promise {snapshot.id}
                  </Link>{" "}
                  · captured {snapshot.status}, version {snapshot.version}
                  {current ? (
                    <>
                      {" "}
                      · current {current.status}, version {current.version} ·{" "}
                      {current.amount
                        ? formatMoney(current.amount)
                        : "Amount unresolved"}{" "}
                      · {current.due_date ?? "Date unresolved"}
                    </>
                  ) : (
                    " · current record unavailable"
                  )}
                </li>
              );
            })}
          </ul>
          {!lineageCurrent && (
            <p role="alert">
              The captured lineage changed or is unavailable. Confirmation is
              blocked; it cannot silently adopt new predecessor versions.
            </p>
          )}
        </section>
      )}
      {confirmable && !termsResolved && (
        <p>
          Unresolved terms cannot be confirmed. Use the source-backed
          clarification below; the original and its holds stay pending until a
          complete replacement is separately confirmed.
        </p>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (reviewed && !replacementLoading) void perform("confirm");
        }}
      >
        <label>
          Review reason
          <textarea
            required
            maxLength={2000}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        {confirmable && (
          <label>
            <input
              type="checkbox"
              checked={reviewed}
              disabled={
                replacementLoading ||
                (!!record.replaces_promise_id &&
                  !["ACTIVE", "NEEDS_CONFIRMATION"].includes(
                    replacement?.status ?? "",
                  )) ||
                !lineageCurrent ||
                !termsResolved
              }
              onChange={(event) => {
                setReviewed(event.target.checked);
                setCapturedLineage(
                  event.target.checked ? structuredClone(lineage) : [],
                );
                setCaptured(
                  event.target.checked ? structuredClone(record) : null,
                );
                setCapturedPrior(
                  event.target.checked && replacement
                    ? structuredClone(replacement)
                    : undefined,
                );
              }}
            />
            I reviewed the source and confirm these exact commitment terms.
          </label>
        )}
        {captured && (
          <p>
            Captured commitment version {captured.version}
            {captured.voice_source
              ? ` · facts hash ${captured.voice_source.facts_hash}`
              : ""}
            .
          </p>
        )}
        {reviewChanged && (
          <div role="alert">
            <p>
              The current commitment changed after your review. The captured
              source has not been replaced.
            </p>
            <button
              type="button"
              onClick={() => {
                setReviewed(false);
                setCaptured(null);
                setCapturedPrior(undefined);
              }}
            >
              Review current commitment
            </button>
          </div>
        )}
        {error && <p role="alert">{error}</p>}
        {confirmable && (
          <button
            type="submit"
            disabled={
              busy ||
              !reviewed ||
              reviewChanged ||
              !reason.trim() ||
              !csrfToken ||
              replacementLoading ||
              !lineageCurrent ||
              !termsResolved ||
              (!!record.replaces_promise_id &&
                !["ACTIVE", "NEEDS_CONFIRMATION"].includes(
                  replacement?.status ?? "",
                ))
            }
          >
            Confirm commitment
          </button>
        )}
        <button
          type="button"
          disabled={busy || !reason.trim() || !csrfToken}
          onClick={() => void perform("cancel")}
        >
          Cancel commitment
        </button>
        {busy && <p role="status">Saving reviewed commitment…</p>}
      </form>
    </section>
  );
}
