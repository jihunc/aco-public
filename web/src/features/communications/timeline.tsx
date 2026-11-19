"use client";
import { useState } from "react";
import { InvoiceRef } from "@/lib/invoice-numbers";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ErrorState, stamp } from "@/features/receivables/shared";
import { communicationsApi, type Communication } from "./api";

function Message({
  row,
  workspace,
}: {
  row: Communication;
  workspace: string;
}) {
  const [open, setOpen] = useState(false);
  const thread = useQuery({
    queryKey: ["communication-thread", workspace, row.thread_id],
    queryFn: ({ signal }) =>
      communicationsApi.thread(workspace, row.thread_id, signal),
    enabled: open,
    retry: false,
  });
  return (
    <article className="panel communication-card">
      <div className="section-heading">
        <div>
          <p className="eyebrow">
            {row.direction === "incoming" ? "Email received" : "Email sent"} ·{" "}
            {row.synthetic ? "Synthetic" : "Observed"}
          </p>
          <h3>{row.content.subject || "(No subject)"}</h3>
        </div>
        <time dateTime={row.event_at}>{stamp(row.event_at)}</time>
      </div>
      <p>
        <strong>From:</strong> {row.content.sender || "Unavailable"} ·{" "}
        <strong>To:</strong> {row.content.to.join(", ") || "Unavailable"}
      </p>
      <p>
        <strong>Delivery:</strong>{" "}
        {row.delivery_state === "accepted"
          ? "Accepted by provider; delivery unconfirmed"
          : row.delivery_state.replaceAll("_", " ")}
      </p>
      {row.kind === "CUSTOMER_REPLY" && (
        <p>
          <strong>Customer reply:</strong> received message evidence
        </p>
      )}
      <p>
        <strong>Source:</strong> {row.source_state.replaceAll("_", " ")}
      </p>
      {row.invoice_links.length === 0 && (
        <p>
          No invoice is linked to this message. Its sender remains unverified.
        </p>
      )}
      <div className="communication-links">
        {row.invoice_links.map((link, index) => (
          <span key={`${link.invoice_id}-${index}`}>
            <Link
              href={`/receivables/${link.invoice_id}?workspace=${encodeURIComponent(workspace)}`}
            >
              <InvoiceRef id={link.invoice_id} />
            </Link>
            {row.origin_invoice_id &&
              link.invoice_id !== row.origin_invoice_id && (
                <span> · Mentioned in another invoice&apos;s thread</span>
              )}
            <span> · {link.provenance.replaceAll("_", " ")}</span>
          </span>
        ))}
      </div>
      {row.direction === "incoming" && (
        <p>
          <Link
            href={`/communications/${row.id}?workspace=${encodeURIComponent(workspace)}`}
          >
            Review invoice links
          </Link>
        </p>
      )}
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "Hide message" : "Open message"}
      </button>
      {open && (
        <div className="communication-detail">
          <p>
            <strong>Message ID:</strong>{" "}
            {row.content.message_id || "Unavailable"}
          </p>
          <p>
            <strong>In reply to:</strong> {row.content.in_reply_to || "None"}
          </p>
          <p>
            <strong>References:</strong>{" "}
            {row.content.references.join(", ") || "None"}
          </p>
          {row.content.cc.length > 0 && (
            <p>
              <strong>CC:</strong> {row.content.cc.join(", ")}
            </p>
          )}
          <pre className="communication-body">
            {row.content.text_body ??
              row.content.html_body ??
              "Message body unavailable."}
          </pre>
          {row.content.html_body && !row.content.text_body && (
            <p className="muted">HTML source shown as text for safety.</p>
          )}
          {row.content.attachments.length > 0 && (
            <section>
              <h4>Attachments</h4>
              <ul>
                {row.content.attachments.map((attachment, index) => (
                  <li key={`${attachment.filename}-${index}`}>
                    {attachment.filename} · {attachment.content_type} ·{" "}
                    {attachment.size_bytes ?? "size unavailable"} bytes
                  </li>
                ))}
              </ul>
            </section>
          )}
          <h4>Original source</h4>
          {row.sources.length === 0 ? (
            <p>Original source unavailable.</p>
          ) : (
            <ul>
              {row.sources.map((source) => (
                <li key={source.artifact_id}>
                  {source.representation.replaceAll("_", " ")} ·{" "}
                  {source.storage_state}
                  {source.available ? (
                    <a
                      href={`/api/workspaces/${encodeURIComponent(workspace)}/artifacts/${source.artifact_id}/content`}
                    >
                      Download authenticated source
                    </a>
                  ) : (
                    <span> · Source unavailable</span>
                  )}
                </li>
              ))}
            </ul>
          )}
          {row.delivery_events.length > 0 && (
            <section>
              <h4>Delivery history</h4>
              <ul>
                {row.delivery_events.map((event, index) => (
                  <li key={`${event.state}-${index}`}>
                    {event.state.replaceAll("_", " ")} ·{" "}
                    {stamp(event.occurred_at)}
                  </li>
                ))}
              </ul>
            </section>
          )}
          {thread.isPending && <p role="status">Loading source thread…</p>}
          {thread.isError && (
            <p role="alert">Thread unavailable: {thread.error.message}</p>
          )}
          {thread.data && (
            <p>
              Source thread contains {thread.data.messages.length} stored
              message{thread.data.messages.length === 1 ? "" : "s"}. Origin
              invoice:{" "}
              {thread.data.origin_invoice_id?.slice(0, 8) ?? "unlinked"}.
              {!thread.data.customer_id && " Customer link unknown."}
            </p>
          )}
        </div>
      )}
    </article>
  );
}

export function CommunicationTimeline({
  workspace,
  invoiceId,
  customerId,
}: {
  workspace: string;
  invoiceId?: string;
  customerId?: string;
}) {
  const [page, setPage] = useState(1);
  const [direction, setDirection] = useState("");
  const query = useQuery({
    queryKey: [
      "communications",
      workspace,
      invoiceId,
      customerId,
      page,
      direction,
    ],
    queryFn: ({ signal }) =>
      communicationsApi.list(
        workspace,
        {
          invoice_id: invoiceId,
          customer_id: customerId,
          direction,
          page,
          page_size: 30,
        },
        signal,
      ),
    retry: false,
  });
  if (query.isPending) return <p role="status">Loading communications…</p>;
  if (query.isError)
    return (
      <ErrorState
        label="Communications unavailable"
        error={query.error}
        retry={() => query.refetch()}
      />
    );
  const ordered = [...query.data.items].sort(
    (a, b) => a.event_at.localeCompare(b.event_at) || a.id.localeCompare(b.id),
  );
  return (
    <section
      aria-label="Communication history"
      className="communication-timeline"
    >
      <div className="section-heading">
        <h2>
          {invoiceId
            ? "Invoice communications"
            : customerId
              ? "Customer conversations"
              : "Conversations"}
        </h2>
        <label>
          Direction{" "}
          <select
            value={direction}
            onChange={(e) => {
              setPage(1);
              setDirection(e.target.value);
            }}
          >
            <option value="">All</option>
            <option value="incoming">Received</option>
            <option value="outgoing">Sent</option>
          </select>
        </label>
      </div>
      <p className="muted">
        {query.data.total} stored communication
        {query.data.total === 1 ? "" : "s"}. Provider acceptance is separate
        from delivery or reply.
      </p>
      {ordered.length === 0 ? (
        <div className="panel">
          <p>No communications yet.</p>
        </div>
      ) : (
        ordered.map((row) => (
          <Message key={row.id} row={row} workspace={workspace} />
        ))
      )}
      {query.data.total > query.data.page_size && (
        <nav className="pagination" aria-label="Communication pages">
          <button
            type="button"
            onClick={() => setPage((p) => p - 1)}
            disabled={page <= 1}
          >
            Previous page
          </button>
          <span>
            Page {page} of {Math.ceil(query.data.total / query.data.page_size)}
          </span>
          <button
            type="button"
            onClick={() => setPage((p) => p + 1)}
            disabled={page * query.data.page_size >= query.data.total}
          >
            Next page
          </button>
        </nav>
      )}
    </section>
  );
}

export { Message as CommunicationRecord };
