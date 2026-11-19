"use client";
import Link from "next/link";
import { VoiceBusinessSource } from "@/features/conversations/voice-business-source";
import { useState, useSyncExternalStore } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ContactStopView } from "@/lib/generated-api";
import {
  captureContactStop,
  contactStopApi,
  ContactStopError,
  restoreContactStopCapture,
  type ContactStopCapture,
} from "./contact-stop-api";
const event = "aco-contact-stop-review";
function subscribe(notify: () => void) {
  window.addEventListener("storage", notify);
  window.addEventListener(event, notify);
  return () => {
    window.removeEventListener("storage", notify);
    window.removeEventListener(event, notify);
  };
}
export function ContactStopSummary({
  review,
  workspace,
}: {
  review: ContactStopView;
  workspace: string;
}) {
  const remaining = review.original_holds.filter(
    (h) => h.released_at === null,
  ).length;
  const href = (path: string) =>
    `${path}?workspace=${encodeURIComponent(workspace)}`;
  return (
    <div
      className="source-provenance"
      style={{ overflowWrap: "anywhere", minWidth: 0 }}
    >
      <p>
        {review.pending
          ? "Customer contact paused pending human review"
          : "This contact restriction was explicitly released"}
        .
      </p>
      <p>
        {remaining} of {review.original_holds.length} original holds remain
        pending.{" "}
        {!review.evidence_complete &&
          "Original evidence is incomplete; release is unavailable."}
      </p>
      {review.voice_source ? (
        <p>Normalized contact-stop request retained as operational facts.</p>
      ) : (
        <blockquote>{review.source_excerpt}</blockquote>
      )}
      {review.voice_source ? (
        <VoiceBusinessSource
          source={review.voice_source}
          workspace={workspace}
        />
      ) : review.source_communication_id ? (
        <p>
          <Link
            href={href(
              `/communications/${encodeURIComponent(review.source_communication_id)}`,
            )}
          >
            Read original incoming message
          </Link>
        </p>
      ) : (
        <p>Source evidence unavailable.</p>
      )}
      <p>
        Sender authority remains unverified. Release does not verify a recipient
        or resume automation. Separate disputes, promises and other restrictions
        remain independent.
      </p>
      <details>
        <summary>Original customer restriction references</summary>
        <p>
          Action {review.action_id} · version {review.action_version} · customer{" "}
          {review.customer_id}
        </p>
        <ul>
          {review.original_holds.map((h) => (
            <li key={h.id}>
              <Link
                href={href(`/receivables/${encodeURIComponent(h.invoice_id)}`)}
              >
                View affected invoice
              </Link>{" "}
              · hold {h.id} · version {h.version} ·{" "}
              {h.released_at ? `Released ${h.released_at}` : "Pending"}
              {h.release_evidence && <p>{h.release_evidence}</p>}
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}
export function InvoiceContactStops({
  workspace,
  invoiceId,
  csrfToken,
  canResolve,
}: {
  workspace: string;
  invoiceId: string;
  csrfToken: string;
  canResolve: boolean;
}) {
  const cache = useQueryClient();
  const key = `aco-contact-stop:${workspace}:${invoiceId}`;
  const raw = useSyncExternalStore(
    subscribe,
    () => window.localStorage.getItem(key),
    () => null,
  );
  const captured = restoreContactStopCapture(raw);
  const [reason, setReason] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const query = useQuery({
    queryKey: ["contact-stops", workspace, invoiceId],
    queryFn: ({ signal }) => contactStopApi.list(workspace, invoiceId, signal),
    retry: false,
  });
  function save(value: ContactStopCapture | null) {
    if (value) window.localStorage.setItem(key, JSON.stringify(value));
    else window.localStorage.removeItem(key);
    window.dispatchEvent(new Event(event));
  }
  const mutation = useMutation({
    mutationFn: (value: ContactStopCapture) =>
      contactStopApi.release(workspace, value, csrfToken),
    onSuccess: async () => {
      save(null);
      setConfirmed(true);
      setReason("");
      await Promise.all(
        [
          "contact-stops",
          "invoice",
          "receivables",
          "operations",
          "review-queue",
          "tasks",
        ].map((name) =>
          cache.invalidateQueries({ queryKey: [name, workspace] }),
        ),
      );
    },
  });
  const stale =
    mutation.error instanceof ContactStopError &&
    mutation.error.refreshRequired;
  function capture(review: ContactStopView) {
    try {
      save(captureContactStop(review, reason));
      setLocalError(null);
      setConfirmed(false);
      mutation.reset();
    } catch (error) {
      setLocalError(
        error instanceof Error
          ? error.message
          : "Review could not be captured.",
      );
    }
  }
  return (
    <section className="panel" aria-label="Customer contact restrictions">
      <h3>Customer contact restrictions</h3>
      {query.isPending && (
        <p role="status">Loading current customer restrictions…</p>
      )}
      {query.isError && (
        <div role="alert">
          <p>Current restrictions could not be loaded.</p>
          <button type="button" onClick={() => query.refetch()}>
            Retry loading restrictions
          </button>
        </div>
      )}
      {query.data?.items.length === 0 && (
        <p>No saved customer contact-stop restriction for this invoice.</p>
      )}
      {confirmed && (
        <p role="status">
          The reviewed customer restriction was released. Recipient review,
          other holds and automation settings remain unchanged.
        </p>
      )}
      {query.data?.items.map((review) => (
        <article key={review.action_id}>
          <ContactStopSummary review={review} workspace={workspace} />
          {canResolve && review.can_release && !captured && (
            <>
              <label htmlFor={`stop-reason-${review.action_id}`}>
                Human disposition reason
              </label>
              <textarea
                id={`stop-reason-${review.action_id}`}
                maxLength={1000}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              <button type="button" onClick={() => capture(review)}>
                Review release of this customer restriction
              </button>
            </>
          )}
          {!canResolve && review.pending && (
            <p>
              Current Owner or finance review permission is required to release
              this restriction.
            </p>
          )}
        </article>
      ))}
      {captured && (
        <section aria-label="Captured contact restriction review">
          <h4>Exact captured release review</h4>
          <ContactStopSummary review={captured.review} workspace={workspace} />
          <p>Disposition reason: {captured.command.reason}</p>
          <p style={{ overflowWrap: "anywhere" }}>
            Request reference: {captured.command.request_id}
          </p>
          <p>This saved review will not change when current data refreshes.</p>
          {canResolve && (
            <button
              type="button"
              disabled={mutation.isPending || stale || !csrfToken}
              onClick={() => mutation.mutate(captured)}
            >
              {mutation.isPending
                ? "Confirming release…"
                : mutation.isError
                  ? "Retry same captured release"
                  : "Confirm reviewed release"}
            </button>
          )}
          <button
            type="button"
            disabled={mutation.isPending}
            onClick={() => {
              save(null);
              mutation.reset();
              setLocalError(null);
              query.refetch();
            }}
          >
            Refresh and review current restrictions
          </button>
        </section>
      )}
      {(localError || mutation.error) && (
        <p role="alert">{localError ?? mutation.error?.message}</p>
      )}
    </section>
  );
}
