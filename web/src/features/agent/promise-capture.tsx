"use client";
import { useId, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { InvoiceView, WorkspaceView } from "@/lib/generated-api";
import { api } from "@/lib/api";
import { formatMoney } from "@/lib/money";
import { communicationsApi } from "@/features/communications/api";
import { ErrorState, stamp } from "@/features/receivables/shared";
import {
  PromiseInstallmentInputs,
  type InstallmentDraft,
} from "./promise-installments";
import {
  preparePromiseCapture,
  promiseApi,
  type PaymentPromise,
  type PreparedPromiseCapture,
} from "./business-api";

export function PromiseCaptureReview({
  review,
}: {
  review: PreparedPromiseCapture;
}) {
  return (
    <section className="agent-notice" aria-label="Reviewed promise capture">
      <h3>
        {review.replaces
          ? "Proposed amendment terms"
          : "Exact commitment terms"}
      </h3>
      <p>
        {formatMoney(review.amount)} · {review.dueDate} · {review.timezone}
      </p>
      <ul>
        {review.allocations.map((part) => (
          <li key={part.invoice_id}>
            {part.number} · {formatMoney(part.amount)} · Invoice{" "}
            {part.invoice_id}
          </li>
        ))}
      </ul>
      {!!review.installments?.length && (
        <section aria-label="Reviewed installment plan">
          <h4>Explicit installment plan</h4>
          <ol>
            {review.installments.map((row, index) => (
              <li key={`${index}:${row.dueDate}`}>
                {row.dueDate} · {formatMoney(row.amount)}
                <ul>
                  {review.allocations.map((part) => {
                    const portion = row.allocations.find(
                      (item) => item.invoice_id === part.invoice_id,
                    );
                    return (
                      <li key={part.invoice_id}>
                        {part.number} ·{" "}
                        {portion
                          ? formatMoney(portion.amount)
                          : "No portion in this installment"}
                      </li>
                    );
                  })}
                </ul>
              </li>
            ))}
          </ol>
        </section>
      )}
      <p className="task-notes">{review.evidence.excerpt}</p>
      <p>
        Source {review.evidence.communication_id} · Code-point offset{" "}
        {review.evidence.offset}.
      </p>
      {review.replaces && (
        <p>
          Proposes replacing promise {review.replaces.id}, reviewed version{" "}
          {review.replaces.version}. The original remains{" "}
          {review.replaces.status === "NEEDS_CONFIRMATION"
            ? "pending"
            : "active"}{" "}
          until a separate authorized confirmation checks both current versions.
        </p>
      )}
      <p>
        Capture records a commitment for review. It is not a payment or
        automatic confirmation.
      </p>
    </section>
  );
}
export function PromiseCapturePanel({
  workspace,
  invoice,
  csrfToken,
  replaces,
}: {
  workspace: WorkspaceView;
  invoice: InvoiceView;
  csrfToken: string;
  replaces?: PaymentPromise;
}) {
  const router = useRouter(),
    cache = useQueryClient(),
    sourceField = useId();
  const [messageId, setMessageId] = useState(""),
    [excerpt, setExcerpt] = useState(""),
    [decimal, setDecimal] = useState(""),
    [dueDate, setDueDate] = useState(""),
    [timezone, setTimezone] = useState("");
  const [selected, setSelected] = useState<string[]>(
    replaces?.invoice_links.length
      ? replaces.invoice_links.map((link) => link.invoice_id)
      : [invoice.id],
  );
  const [allocations, setAllocations] = useState<Record<string, string>>({});
  const [review, setReview] = useState<PreparedPromiseCapture | null>(null);
  const [installments, setInstallments] = useState<InstallmentDraft[]>([]);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const permitted = workspace.permissions.includes("task_write");
  const requiredIds = replaces?.invoice_links.length
    ? replaces.invoice_links.map((link) => link.invoice_id)
    : undefined;
  const clarifying = replaces?.status === "NEEDS_CONFIRMATION";
  const messages = useQuery({
    queryKey: [
      "promise-source-options",
      workspace.id,
      replaces ? `customer:${invoice.customer.id}` : invoice.id,
    ],
    queryFn: ({ signal }) =>
      communicationsApi.list(
        workspace.id,
        {
          ...(replaces
            ? { customer_id: invoice.customer.id }
            : { invoice_id: invoice.id }),
          direction: "incoming",
          page_size: 100,
        },
        signal,
      ),
    enabled: permitted,
    retry: false,
  });
  const available =
    messages.data?.items.filter(
      (row) =>
        row.source_state === "stored" &&
        row.content.text_body &&
        (!replaces ||
          (row.id !== replaces.source_communication_id &&
            (requiredIds ?? []).every((id) =>
              row.invoice_links.some((link) => link.invoice_id === id),
            ))),
    ) ?? [];
  const source = available.find((row) => row.id === messageId);
  const sourceInvoices = useQuery({
    queryKey: ["promise-source-invoices", workspace.id, source?.id],
    enabled: !!source && permitted,
    queryFn: async ({ signal }) =>
      Promise.all(
        source!.invoice_links.map(
          async (link) =>
            (await api.invoice(workspace.id, link.invoice_id, signal)).invoice,
        ),
      ),
    retry: false,
  });
  const eligibleInvoices =
    sourceInvoices.data?.filter(
      (row) =>
        row.customer.id === invoice.customer.id &&
        row.original.currency === invoice.original.currency &&
        row.original.exponent === invoice.original.exponent &&
        (!requiredIds || requiredIds.includes(row.id)),
    ) ?? [];
  if (!permitted) return null;
  const locked = busy || !!review;
  async function submit() {
    if (!review || busy) return;
    setBusy(true);
    setError("");
    try {
      const saved = await promiseApi.capturePrepared(
        workspace.id,
        review,
        csrfToken,
      );
      await Promise.all(
        ["promises", "invoice", "customer"].map((key) =>
          cache.invalidateQueries({ queryKey: [key, workspace.id] }),
        ),
      );
      router.push(
        `/promises/${encodeURIComponent(saved.id)}?workspace=${encodeURIComponent(workspace.id)}`,
      );
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : "Capture outcome unconfirmed. Retry these exact reviewed terms or inspect current saved promises.",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="panel promise-controls promise-capture"
      aria-label={
        clarifying
          ? "Clarify pending promise"
          : replaces
            ? "Record source-backed amendment"
            : "Record source-backed promise"
      }
    >
      <h2>
        {clarifying
          ? "Clarify the pending commitment"
          : replaces
            ? "Propose a revised payment commitment"
            : "Record a payment promise"}
      </h2>
      <p>
        Use the exact incoming statement and explicit invoice allocations.
        Recording a commitment never marks invoices paid or automatically
        confirms source authority.
      </p>
      {replaces && (
        <p>
          The original promise {replaces.id} remains {replaces.status}. Revised
          terms must retain its known customer, currency, invoice scope and
          terms. Clarification fills unknown terms from another exact retained
          source. Separate confirmation reviews the captured predecessor
          lineage; capture never clears its holds.
        </p>
      )}
      {messages.isPending ? (
        <p role="status">Loading linked incoming source messages…</p>
      ) : messages.isError ? (
        <ErrorState
          label="Incoming source messages unavailable"
          error={messages.error}
          retry={() => messages.refetch()}
        />
      ) : !available.length ? (
        <p>
          No eligible stored incoming statement is linked to this{" "}
          {replaces
            ? "complete original invoice scope. Capture a new source statement before proposing an amendment."
            : "invoice. Open Email settings to capture an explicitly synthetic sample in an enabled demo, or wait for verified inbound evidence."}
        </p>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (!source || locked) return;
            setError("");
            try {
              const chosen = eligibleInvoices.filter((row) =>
                selected.includes(row.id),
              );
              if (chosen.length !== selected.length)
                throw new Error(
                  "Read the current selected source invoices before reviewing terms.",
                );
              setReview(
                preparePromiseCapture({
                  invoice,
                  source,
                  excerpt,
                  decimal,
                  dueDate,
                  timezone,
                  invoices: chosen,
                  allocations:
                    selected.length > 1
                      ? selected.map((id) => ({
                          invoice_id: id,
                          decimal: allocations[id] ?? "",
                        }))
                      : [],
                  replaces,
                  installments: installments.map((row) => ({
                    dueDate: row.dueDate,
                    decimal: row.decimal,
                    allocations:
                      selected.length > 1
                        ? selected.map((id) => ({
                            invoice_id: id,
                            decimal: row.portions[id] ?? "",
                          }))
                        : [],
                  })),
                }),
              );
            } catch (failure) {
              setError(
                failure instanceof Error
                  ? failure.message
                  : "Review source scope and exact amounts.",
              );
            }
          }}
        >
          <label htmlFor={sourceField}>Promise source message</label>
          <select
            id={sourceField}
            required
            value={messageId}
            disabled={locked}
            onChange={(event) => {
              setMessageId(event.target.value);
              setExcerpt(
                available.find((row) => row.id === event.target.value)?.content
                  .text_body ?? "",
              );
              setSelected(requiredIds ?? [invoice.id]);
              setAllocations({});
              setInstallments([]);
              setDecimal("");
              setDueDate("");
              setTimezone("");
            }}
          >
            <option value="">Choose an incoming message</option>
            {available.map((row) => (
              <option key={row.id} value={row.id}>
                {row.content.subject || "(No subject)"} · {stamp(row.event_at)}
              </option>
            ))}
          </select>
          {source && (
            <p>
              <a
                href={`/communications/${encodeURIComponent(source.id)}?workspace=${encodeURIComponent(workspace.id)}`}
              >
                Inspect original source message
              </a>{" "}
              · {source.synthetic ? "Synthetic source" : "Observed source"}
            </p>
          )}
          <label>
            Exact commitment excerpt
            <textarea
              required
              maxLength={8000}
              value={excerpt}
              disabled={locked}
              onFocus={(event) => event.currentTarget.select()}
              onChange={(event) => setExcerpt(event.target.value)}
            />
            <span className="field-hint">Prefilled with the whole message. Select the exact sentence; typing replaces it.</span>
          </label>
          {source &&
            (sourceInvoices.isPending ? (
              <p role="status">Reading actual source-linked invoices…</p>
            ) : sourceInvoices.isError ? (
              <ErrorState
                label="Source invoice scope unavailable"
                error={sourceInvoices.error}
                retry={() => sourceInvoices.refetch()}
              />
            ) : (
              <fieldset disabled={locked}>
                <legend>Exact invoices and explicit allocation</legend>
                {eligibleInvoices.map((row) => (
                  <div key={row.id}>
                    <label>
                      <input
                        type="checkbox"
                        checked={selected.includes(row.id)}
                        disabled={!!requiredIds || row.id === invoice.id}
                        onChange={(event) =>
                          setSelected((ids) =>
                            event.target.checked
                              ? [...ids, row.id]
                              : ids.filter((id) => id !== row.id),
                          )
                        }
                      />
                      Include {row.number}
                    </label>
                    <p>
                      Source invoice {row.id} · {row.original.currency},
                      exponent {row.original.exponent}
                    </p>
                    {selected.includes(row.id) && selected.length > 1 && (
                      <label>
                        Allocation for {row.number} ({row.original.currency})
                        <input
                          required
                          inputMode="decimal"
                          value={allocations[row.id] ?? ""}
                          onChange={(event) =>
                            setAllocations({
                              ...allocations,
                              [row.id]: event.target.value,
                            })
                          }
                        />
                      </label>
                    )}
                  </div>
                ))}
                <p>
                  Multi-invoice amounts require an explicit source-backed
                  allocation for every selected invoice. No equal split is
                  inferred. Other customer/currency links are not eligible for
                  this scope.
                </p>
              </fieldset>
            ))}
          <label>
            Promised amount ({invoice.original.currency})
            <input
              required
              inputMode="decimal"
              value={decimal}
              disabled={locked}
              onChange={(event) => setDecimal(event.target.value)}
            />
          </label>
          <label>
            Promised payment date
            <input
              required
              type="date"
              value={dueDate}
              disabled={locked}
              onChange={(event) => setDueDate(event.target.value)}
            />
          </label>
          <label>
            Recipient timezone
            <input
              required
              placeholder="For example Australia/Melbourne"
              value={timezone}
              disabled={locked}
              onChange={(event) => setTimezone(event.target.value)}
            />
          </label>
          <PromiseInstallmentInputs
            invoices={eligibleInvoices.filter((row) =>
              selected.includes(row.id),
            )}
            rows={installments}
            onChange={setInstallments}
            disabled={locked}
          />
          <p className="muted">
            Enter only the total, allocations, date and timezone stated or
            documented by this source. Review is separate from confirmation and
            payment verification.
          </p>
          {!review && (
            <button
              type="submit"
              disabled={
                busy ||
                !source ||
                !csrfToken ||
                sourceInvoices.isPending ||
                sourceInvoices.isError
              }
            >
              Review promise terms
            </button>
          )}
        </form>
      )}
      {review && (
        <>
          <PromiseCaptureReview review={review} />
          <button
            type="button"
            disabled={busy || !csrfToken}
            onClick={() => void submit()}
          >
            {busy
              ? "Recording commitment…"
              : replaces
                ? clarifying
                  ? "Record proposed clarification"
                  : "Record proposed amendment"
                : "Record payment promise"}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              setReview(null);
              setError("");
            }}
          >
            Discard reviewed capture
          </button>
        </>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
export function PromiseAmendmentPanel({
  workspace,
  record,
  csrfToken,
}: {
  workspace: WorkspaceView;
  record: PaymentPromise;
  csrfToken: string;
}) {
  const anchor = record.invoice_links[0]?.invoice_id;
  const allowed =
    ["ACTIVE", "NEEDS_CONFIRMATION"].includes(record.status) &&
    workspace.permissions.includes("task_write");
  const query = useQuery({
    queryKey: ["invoice", workspace.id, anchor],
    queryFn: ({ signal }) => api.invoice(workspace.id, anchor!, signal),
    enabled: allowed && !!anchor,
    retry: false,
  });
  if (!allowed) return null;
  if (!anchor)
    return (
      <UnscopedPromiseClarification
        workspace={workspace}
        record={record}
        csrfToken={csrfToken}
      />
    );
  return query.isPending ? (
    <p role="status">Reading original amendment scope…</p>
  ) : query.isError ? (
    <ErrorState
      label="Original invoice scope unavailable"
      error={query.error}
      retry={() => query.refetch()}
    />
  ) : (
    <PromiseCapturePanel
      key={`${record.id}:${record.version}`}
      workspace={workspace}
      invoice={query.data.invoice}
      csrfToken={csrfToken}
      replaces={record}
    />
  );
}

function UnscopedPromiseClarification({
  workspace,
  record,
  csrfToken,
}: {
  workspace: WorkspaceView;
  record: PaymentPromise;
  csrfToken: string;
}) {
  const [selected, setSelected] = useState("");
  const query = useQuery({
    queryKey: [
      "clarification-customer-invoices",
      workspace.id,
      record.customer_id,
    ],
    queryFn: ({ signal }) =>
      api.invoices(
        workspace.id,
        { customer_id: record.customer_id, page_size: 100 },
        signal,
      ),
    retry: false,
  });
  const invoice = query.data?.items.find((row) => row.id === selected);
  return (
    <section className="panel" aria-label="Resolve unknown promise scope">
      <h2>Clarify invoice scope from a new source</h2>
      <p>
        The original has no supported invoice allocation. Select a current
        customer invoice, then a retained incoming statement explicitly linked
        to the clarified scope. No amount or allocation is inferred.
      </p>
      {query.isPending ? (
        <p role="status">Reading current customer invoices…</p>
      ) : query.isError ? (
        <ErrorState
          label="Customer invoices unavailable"
          error={query.error}
          retry={() => query.refetch()}
        />
      ) : (
        <label>
          Clarified invoice reference
          <select
            value={selected}
            onChange={(event) => setSelected(event.target.value)}
          >
            <option value="">Choose a source-supported invoice</option>
            {query.data.items
              .filter(
                (row) =>
                  !record.amount ||
                  (row.original.currency === record.amount.currency &&
                    row.original.exponent === record.amount.exponent),
              )
              .map((row) => (
                <option key={row.id} value={row.id}>
                  {row.number} · {row.original.currency}
                </option>
              ))}
          </select>
        </label>
      )}
      {invoice && (
        <PromiseCapturePanel
          key={invoice.id}
          workspace={workspace}
          invoice={invoice}
          replaces={record}
          csrfToken={csrfToken}
        />
      )}
    </section>
  );
}
