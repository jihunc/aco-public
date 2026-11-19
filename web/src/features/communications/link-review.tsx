"use client";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Communication } from "./api";
import {
  capturedLinkCommand,
  communicationLinkApi,
  type LinkReview,
} from "./link-api";

export function CommunicationLinkPanel({
  workspace,
  source,
}: {
  workspace: string;
  source: Communication;
}) {
  const cache = useQueryClient();
  const session = useQuery({
    queryKey: ["me"],
    queryFn: ({ signal }) => api.session(signal),
    retry: false,
  });
  const review = useQuery({
    queryKey: ["communication-link-review", workspace, source.id],
    queryFn: ({ signal }) =>
      communicationLinkApi.review(workspace, source.id, signal),
    retry: false,
  });
  const [captured, setCaptured] = useState<LinkReview | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [excerpt, setExcerpt] = useState("");
  const [offset, setOffset] = useState(0);
  const [confirmed, setConfirmed] = useState(false);
  const [pending, setPending] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  if (source.direction !== "incoming" || source.channel !== "email")
    return null;
  return (
    <section className="panel" aria-label="Review invoice links">
      <h2>Review invoice links</h2>
      <p>
        Add proven invoice references to this stored original. Sender identity
        remains unverified. Linking does not change financial records, contact
        permissions or the original thread.
      </p>
      {review.isPending ? (
        <p role="status">Loading current source scope…</p>
      ) : review.isError ? (
        <>
          <p role="alert">Link review unavailable: {review.error.message}</p>
          <button type="button" onClick={() => void review.refetch()}>
            Retry link review
          </button>
        </>
      ) : review.data && !review.data.can_link ? (
        <p>
          Review required:{" "}
          {review.data.reasons.join(", ") ||
            "No unambiguous source-backed invoice references are available."}
        </p>
      ) : null}
      {!captured && review.data?.can_link && (
        <button
          type="button"
          onClick={() => {
            setCaptured(structuredClone(review.data));
            setSelected([]);
            setExcerpt(
              Array.from(source.content.text_body ?? "")
                .slice(0, 8000)
                .join(""),
            );
            setOffset(0);
            setConfirmed(false);
            setError("");
            setNotice("");
          }}
        >
          Review current references
        </button>
      )}
      {captured && (
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            if (pending || !confirmed || !session.data?.csrf_token) return;
            setPending(true);
            setError("");
            try {
              const command = capturedLinkCommand(
                captured,
                source,
                selected,
                excerpt,
                offset,
              );
              await communicationLinkApi.save(
                workspace,
                source.id,
                command,
                session.data.csrf_token,
              );
              setCaptured(null);
              setSelected([]);
              setConfirmed(false);
              setNotice(
                "Invoice links saved. The original is retained once; sender authority remains unverified.",
              );
              await Promise.all(
                [
                  "communications",
                  "communication",
                  "communication-link-review",
                  "case-source-options",
                  "communication-thread",
                ].map((key) =>
                  cache.invalidateQueries({ queryKey: [key, workspace] }),
                ),
              );
            } catch (failure) {
              setError(
                failure instanceof Error
                  ? failure.message
                  : "Invoice linking failed.",
              );
            } finally {
              setPending(false);
            }
          }}
        >
          <p>
            Captured communication version {captured.communication_version}.
            Background refresh will not replace this review.
          </p>
          <fieldset disabled={pending}>
            <legend>Proven invoice references (maximum three)</legend>
            {captured.candidates.map((candidate) => (
              <label key={candidate.invoice_id} className="check-line">
                <input
                  type="checkbox"
                  checked={selected.includes(candidate.invoice_id)}
                  onChange={(event) => {
                    setConfirmed(false);
                    setSelected((ids) =>
                      event.target.checked
                        ? [...ids, candidate.invoice_id]
                        : ids.filter((id) => id !== candidate.invoice_id),
                    );
                  }}
                />
                {candidate.number} · {candidate.provenance.replaceAll("_", " ")}
              </label>
            ))}
          </fieldset>
          <label>
            Exact source excerpt
            <textarea
              value={excerpt}
              maxLength={16000}
              rows={5}
              disabled={pending}
              onChange={(event) => {
                setExcerpt(event.target.value);
                setConfirmed(false);
              }}
            />
          </label>
          <label>
            Character offset in original text
            <input
              type="number"
              min={0}
              step={1}
              value={offset}
              disabled={pending}
              onChange={(event) => {
                setOffset(Number(event.target.value));
                setConfirmed(false);
              }}
            />
          </label>
          <label className="check-line">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={pending}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            I reviewed the exact source and invoice references; this does not
            verify the sender.
          </label>
          <div className="action-row">
            <button
              type="submit"
              disabled={
                pending ||
                !confirmed ||
                !selected.length ||
                selected.length > 3 ||
                !session.data?.csrf_token
              }
            >
              {pending ? "Saving links…" : "Save reviewed invoice links"}
            </button>
            <button
              type="button"
              disabled={pending}
              onClick={() => {
                setCaptured(null);
                setConfirmed(false);
                setSelected([]);
                setError("");
                void review.refetch();
              }}
            >
              Cancel / refresh review
            </button>
          </div>
        </form>
      )}
      {error && (
        <p role="alert">
          Link not saved: {error}. Refresh the review explicitly before retrying
          a conflict.
        </p>
      )}
      {notice && <p role="status">{notice}</p>}
    </section>
  );
}
