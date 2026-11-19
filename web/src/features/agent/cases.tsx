"use client";
import Link from "next/link";
import { InvoiceRef } from "@/lib/invoice-numbers";
import { VoiceBusinessSource } from "@/features/conversations/voice-business-source";
import type { WorkspaceView } from "@/lib/generated-api";
import { CaseTaskReview } from "./case-review";
import { CaseControls } from "./case-commands";
import { HandlingTimePanel } from "./handling-time";
import { useQuery } from "@tanstack/react-query";
import { ErrorState, stamp } from "@/features/receivables/shared";
import { businessApi, type CollectionCase } from "./business-api";
export function InvoiceCases({
  workspace,
  invoiceId,
}: {
  workspace: string;
  invoiceId: string;
}) {
  const query = useQuery({
    queryKey: ["cases", workspace, invoiceId],
    queryFn: ({ signal }) => businessApi.cases(workspace, invoiceId, signal),
    retry: false,
  });
  return (
    <section className="panel">
      <h3>Cases</h3>
      {query.isPending ? (
        <p role="status">Loading linked cases…</p>
      ) : query.isError ? (
        <ErrorState
          label="Cases unavailable"
          error={query.error}
          retry={() => query.refetch()}
        />
      ) : !query.data.items.length ? (
        <p>No cases are linked to this invoice.</p>
      ) : (
        <ul className="agent-record-list">
          {query.data.items.map((record) => (
            <li key={record.id}>
              <Link
                href={`/cases/${encodeURIComponent(record.id)}?workspace=${encodeURIComponent(workspace)}`}
              >
                {record.issue_type.replaceAll("_", " ")} ·{" "}
                {record.id.slice(0, 8)}
              </Link>
              <p>{record.substance}</p>
              <p className="muted">
                {record.status.replaceAll("_", " ")} ·{" "}
                {record.synthetic ? "Synthetic" : "Observed"}
                {record.review_required ? " · Review required" : ""}
              </p>
              {record.invoice_links
                .filter((link) => link.invoice_id === invoiceId)
                .map((link) => (
                  <p key={link.invoice_id}>
                    Invoice issue: {link.issue_state.replaceAll("_", " ")} ·{" "}
                    {link.impact.replaceAll("_", " ")}
                  </p>
                ))}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
export function CaseDetail({
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
    queryKey: ["case", workspace, id],
    queryFn: ({ signal }) => businessApi.case(workspace, id, signal),
    retry: false,
  });
  return query.isPending ? (
    <p role="status">Loading case evidence…</p>
  ) : query.isError ? (
    <ErrorState
      label="Case unavailable"
      error={query.error}
      retry={() => query.refetch()}
    />
  ) : (
    <>
      {currentWorkspace && (
        <CaseControls
          key={`${query.data.id}:${query.data.version}`}
          workspace={currentWorkspace}
          record={query.data}
          csrfToken={csrfToken ?? ""}
          onSaved={() => query.refetch()}
        />
      )}
      {currentWorkspace && (
        <HandlingTimePanel
          key={`handling:${query.data.id}:${query.data.version}`}
          workspace={currentWorkspace}
          record={query.data}
          csrfToken={csrfToken ?? ""}
        />
      )}
      {currentWorkspace && (
        <CaseTaskReview
          key={`review:${query.data.id}:${query.data.version}`}
          record={query.data}
          workspace={currentWorkspace}
          csrfToken={csrfToken ?? ""}
        />
      )}
      <CaseRecord record={query.data} workspace={workspace} />
    </>
  );
}
export function CaseRecord({
  record,
  workspace,
}: {
  record: CollectionCase;
  workspace: string;
}) {
  const suffix = `?workspace=${encodeURIComponent(workspace)}`;
  return (
    <div className="agent-business-record">
      <section className="panel">
        <p className="eyebrow">
          {record.synthetic ? "Synthetic case" : "Observed case"} · Version{" "}
          {record.version}
        </p>
        <div className="section-heading">
          <h2 className="capitalize">
            {record.issue_type.replaceAll("_", " ")}
          </h2>
          <span className="badge">{record.status.replaceAll("_", " ")}</span>
        </div>
        <p>{record.substance}</p>
        {record.review_required && (
          <p className="agent-notice">
            Review required. This record does not establish financial
            settlement.
          </p>
        )}
        <p className="muted">Case reference: {record.id}</p>
        <dl className="detail-grid">
          <div>
            <dt>Opened</dt>
            <dd>{stamp(record.opened_at)}</dd>
          </div>
          <div>
            <dt>Updated</dt>
            <dd>{stamp(record.updated_at)}</dd>
          </div>
          <div>
            <dt>Resolved</dt>
            <dd>{stamp(record.resolved_at)}</dd>
          </div>
          <div>
            <dt>Closed</dt>
            <dd>{stamp(record.closed_at)}</dd>
          </div>
        </dl>
        {record.closure_reason && (
          <p>Closure reason: {record.closure_reason.replaceAll("_", " ")}</p>
        )}
        <p>
          <Link
            href={`/customers/${encodeURIComponent(record.customer_id)}${suffix}`}
          >
            Customer account
          </Link>{" "}
          ·{" "}
          <Link href={`/tasks/${encodeURIComponent(record.task_id)}${suffix}`}>
            Open linked task
          </Link>
        </p>
        <p className="muted">
          Case status and invoice payment status are independent. Each affected
          invoice retains its own issue and resolution evidence.
        </p>
      </section>
      <section className="panel">
        <h2>Affected invoices</h2>
        <ul className="agent-record-list">
          {record.invoice_links.map((link) => (
            <li key={link.invoice_id}>
              <Link
                href={`/receivables/${encodeURIComponent(link.invoice_id)}${suffix}`}
              >
                <InvoiceRef id={link.invoice_id} />
              </Link>
              <p>
                Issue: {link.issue_state.replaceAll("_", " ")} · Impact:{" "}
                {link.impact.replaceAll("_", " ")}
              </p>
              {link.hold_id && <p>Linked hold: {link.hold_id}</p>}
              {link.resolution_action && (
                <p>Resolution action: {link.resolution_action}</p>
              )}
              {link.resolution_evidence_id && (
                <p>Resolution evidence: {link.resolution_evidence_id}</p>
              )}
              {link.resolved_at && <p>Resolved {stamp(link.resolved_at)}</p>}
            </li>
          ))}
        </ul>
      </section>
      <section className="panel">
        <h2>Source evidence</h2>
        {record.evidence.length ? (
          <ul className="agent-record-list">
            {record.evidence.map((evidence) => (
              <li key={evidence.id}>
                <p className="task-notes">{evidence.excerpt}</p>
                <p className="muted">
                  {evidence.synthetic ? "Synthetic" : "Observed"} ·{" "}
                  {evidence.source_state.replaceAll("_", " ")} ·{" "}
                  {stamp(evidence.created_at)} ·{" "}
                  {evidence.voice_source
                    ? "Signed operational source"
                    : `Offset ${evidence.offset}`}
                </p>
                {evidence.voice_source ? (
                  <VoiceBusinessSource
                    source={evidence.voice_source}
                    workspace={workspace}
                  />
                ) : evidence.communication_id ? (
                  <Link
                    href={`/communications/${encodeURIComponent(evidence.communication_id)}${suffix}`}
                  >
                    Open source message
                  </Link>
                ) : (
                  <p>Source evidence unavailable.</p>
                )}
                <p className="muted">Evidence reference: {evidence.id}</p>
              </li>
            ))}
          </ul>
        ) : (
          <p>No evidence references were returned.</p>
        )}
      </section>
      <section className="panel">
        <h2>Case history</h2>
        {record.history.length ? (
          <ol className="agent-record-list">
            {record.history.map((event) => (
              <li key={event.id}>
                <strong>{event.event.replaceAll("_", " ")}</strong>
                <p>
                  {stamp(event.created_at)} · Actor {event.actor_id}
                  {event.substantive_human
                    ? " · Substantive human handling"
                    : ""}
                </p>
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
