"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { formatMoney } from "@/lib/money";
import { stamp, ErrorState, SourceBadges } from "./shared";
import { CaseCapturePanel } from "@/features/agent/case-commands";
import { PromiseCapturePanel } from "@/features/agent/promise-capture";
import { InvoicePromises } from "@/features/agent/promises";
import { InvoiceCases } from "@/features/agent/cases";
import type { WorkspaceView } from "@/lib/generated-api";
import { InvoiceReminderHistory } from "./reminders";
import { DemoPaymentPanel } from "./payment";
import { InvoiceContactStops } from "./contact-stop";
import { CommunicationTimeline } from "@/features/communications/timeline";
import { InvoiceTasks, InvoiceAudit } from "./activity";
import { NextSavedAction } from "./customer-contact";
export function InvoiceDetail({
  workspace,
  id,
  href,
  currentWorkspace,
  csrfToken,
}: {
  workspace: string;
  currentWorkspace?: WorkspaceView;
  csrfToken?: string;
  id: string;
  href: (path: string) => string;
}) {
  const query = useQuery({
    queryKey: ["invoice", workspace, id],
    queryFn: ({ signal }) => api.invoice(workspace, id, signal),
    retry: false,
  });
  if (query.isPending) return <p role="status">Loading invoice workspace…</p>;
  if (query.isError)
    return (
      <ErrorState
        label="Invoice unavailable"
        error={query.error}
        retry={() => query.refetch()}
      />
    );
  const invoice = query.data.invoice;
  return (
    <>
      <Link href={href("/receivables")}>← All receivables</Link>
      <section className="panel invoice-detail">
        <div className="section-heading">
          <div>
            <h2>{invoice.number}</h2>
            <Link href={href(`/customers/${invoice.customer.id}`)}>
              {invoice.customer.name}
            </Link>
          </div>
          <strong className="detail-balance">
            {formatMoney(invoice.outstanding)}
          </strong>
        </div>
        <SourceBadges invoice={invoice} />
        <p><Link href={`${href("/agents")}&invoice=${encodeURIComponent(id)}`}>Investigate this invoice</Link></p>
        <dl className="detail-grid">
          <div>
            <dt>Issue date</dt>
            <dd>{invoice.issue_date}</dd>
          </div>
          <div>
            <dt>Due date</dt>
            <dd>{invoice.due_date}</dd>
          </div>
          <div>
            <dt>Original amount</dt>
            <dd>{formatMoney(invoice.original)}</dd>
          </div>
          <div>
            <dt>Accounting source</dt>
            <dd>
              {invoice.accounting_provider} ·{" "}
              {invoice.source.synthetic ? "Synthetic" : "Observed"}
            </dd>
          </div>
          <div>
            <dt>Last source read</dt>
            <dd>{stamp(invoice.last_synced_at)}</dd>
          </div>
          <div>
            <dt>Initial revision observation</dt>
            <dd>{stamp(invoice.source.observed_at)}</dd>
          </div>
          <div>
            <dt>Financial effective time</dt>
            <dd>{stamp(invoice.source.source_at)}</dd>
          </div>
          <div>
            <dt>Source revision / status</dt>
            <dd>
              {invoice.source.revision} · {invoice.source_status}
            </dd>
          </div>
        </dl>
        <NextSavedAction action={invoice.next_action} />
        <p>Saved policy reminder history appears below. Source changes and current guards determine whether outreach remains eligible.</p>
        <p className="muted">
          Amounts and payment status are imported from accounting.{" "}
          {invoice.freshness !== "fresh"
            ? "This is last-known data; fresh verification is unavailable."
            : ""}
        </p>
      </section>
      {currentWorkspace && <DemoPaymentPanel key={`payment:${invoice.id}`} workspace={currentWorkspace} invoice={invoice} csrfToken={csrfToken ?? ""} />}
      {currentWorkspace && <CaseCapturePanel key={`case:${invoice.id}`} workspace={currentWorkspace} invoice={invoice} csrfToken={csrfToken ?? ""} />}
      {currentWorkspace && <PromiseCapturePanel key={`promise:${invoice.id}`} workspace={currentWorkspace} invoice={invoice} csrfToken={csrfToken ?? ""} />}
      {currentWorkspace && <InvoiceContactStops key={`contact-stop:${workspace}:${id}`} workspace={workspace} invoiceId={id} csrfToken={csrfToken ?? ""} canResolve={currentWorkspace.permissions.includes("resolve")} />}
      <InvoiceReminderHistory workspace={workspace} invoiceId={id} />
      <CommunicationTimeline workspace={workspace} invoiceId={id} />
      <div className="panel-grid">
        <InvoiceCases workspace={workspace} invoiceId={id} />
        <InvoicePromises workspace={workspace} invoiceId={id} />
        <InvoiceTasks key={`tasks:${workspace}:${id}`} workspace={workspace} invoiceId={id} />
        <InvoiceAudit key={`audit:${workspace}:${id}`} workspace={workspace} invoiceId={id} />
      </div>
    </>
  );
}
