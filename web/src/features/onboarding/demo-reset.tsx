"use client";
import Link from "next/link";
import { useState, useSyncExternalStore } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { WorkspaceView } from "@/lib/generated-api";
import { ErrorState, stamp } from "@/features/receivables/shared";
import {
  canReviewReset,
  captureReset,
  readResetReview,
  resetApi,
  type ResetReview,
  type SavedReset,
} from "./reset-api";

export function ResetSummary({
  result,
  workspace,
}: {
  result: SavedReset;
  workspace: string;
}) {
  return (
    <div
      className="space-y-2 min-w-0 break-words"
      aria-label="Saved synthetic reset result"
    >
      <p className="font-semibold">
        {result.source_restoration === "RESTORED"
          ? "Synthetic accounting seed restored"
          : "Source restoration pending; prior work fenced"}
      </p>
      {result.source_restoration === "RESTORED" && (
        <p>
          {result.restored_invoice_ids.length} seed invoices ·{" "}
          {result.restored_customer_ids.length} seed customers restored through
          accounting imports. {result.reversal_allocation_ids.length} residual
          cash allocations reversed with original payment links.
        </p>
      )}
      <p>
        Outbound paused · Policy automation off · Generation{" "}
        {result.previous_generation} → {result.generation}
      </p>
      <p>
        {result.cancelled_action_ids.length} pending actions cancelled ·{" "}
        {result.cancelled_run_ids.length} investigations cancelled.
      </p>
      <p>
        Uncertain effects remain: {result.unknown_action_ids.length} in-flight
        actions recorded as uncertain;{" "}
        {result.preserved_uncertain_action_ids.length} prior uncertain actions
        retained. Their unresolved budget exposure is retained.
      </p>
      <p>
        Cases, promises, tasks, collection holds, consent withdrawals and source
        history remain. Reset does not resolve issues or confirm commitments;
        current source evidence can still affect their state.
      </p>
      <p className="break-all">Saved reset request {result.request_id}</p>
      <div className="flex flex-wrap gap-4">
        <Link
          className="underline"
          href={`/receivables?workspace=${encodeURIComponent(workspace)}`}
        >
          Explore restored receivables →
        </Link>
        <Link
          className="underline"
          href={`/agents?workspace=${encodeURIComponent(workspace)}`}
        >
          Start a new investigation →
        </Link>
        <Link
          className="underline"
          href={`/review?workspace=${encodeURIComponent(workspace)}`}
        >
          Review retained tasks →
        </Link>
      </div>
      <details>
        <summary>Inspect saved reset references</summary>
        <p className="break-all">
          Source events:{" "}
          {result.source_event_ids.join(", ") ||
            "None recorded in this fence checkpoint"}
        </p>
        <ul>
          {result.restored_invoice_ids.map((id) => (
            <li key={id}>
              <Link
                className="underline break-all"
                href={`/receivables/${id}?workspace=${encodeURIComponent(workspace)}`}
              >
                Invoice {id}
              </Link>
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}
const reviewEvent = "aco:demo-reset-review";
function subscribe(changed: () => void) {
  window.addEventListener(reviewEvent, changed);
  window.addEventListener("storage", changed);
  return () => {
    window.removeEventListener(reviewEvent, changed);
    window.removeEventListener("storage", changed);
  };
}
const eligibility: Record<string, string> = {
  DEMO_ENVIRONMENT_REQUIRED:
    "Reset is available only in the local/demo environment.",
  DEMO_WORKSPACE_REQUIRED: "This workspace is not a demo workspace.",
  DEMO_SOURCE_NOT_SEEDED:
    "Import the canonical synthetic source before resetting.",
  SYNTHETIC_ACCOUNTING_REQUIRED:
    "All accounting source records must be explicitly synthetic.",
  DEMO_SOURCE_UNAVAILABLE: "The demo accounting connection is not ready.",
  CANONICAL_SEED_REQUIRED: "An unambiguous canonical demo seed is required.",
};
export function DemoResetPanel({
  workspace,
  csrfToken,
  disabled = false,
}: {
  workspace: WorkspaceView;
  csrfToken: string;
  disabled?: boolean;
}) {
  const cache = useQueryClient();
  const [evidence, setEvidence] = useState("");
  const [accepted, setAccepted] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [saved, setSaved] = useState<SavedReset | null>(null);
  const storageKey = `aco:demo-reset:${workspace.id}`;
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
  const review = readResetReview(raw);
  const query = useQuery({
    queryKey: ["demo-reset", workspace.id],
    queryFn: ({ signal }) => resetApi.read(workspace.id, signal),
    enabled: canReviewReset(workspace),
    retry: false,
  });
  if (!canReviewReset(workspace)) return null;
  const stale =
    !!review &&
    !!query.data &&
    query.data.generation !== review.command.expected_generation;
  const latest = query.data?.last_reset;
  const result =
    latest && (!saved || latest.generation >= saved.generation)
      ? latest
      : saved;
  function keep(next: ResetReview | null) {
    try {
      if (next) sessionStorage.setItem(storageKey, JSON.stringify(next));
      else sessionStorage.removeItem(storageKey);
      window.dispatchEvent(new Event(reviewEvent));
      return true;
    } catch {
      setError(
        "Browser review storage is unavailable. A new reset cannot be submitted until its captured request can be retained.",
      );
      return false;
    }
  }
  async function refresh() {
    if (busy) return;
    setError("");
    const fresh = await query.refetch();
    if (fresh.isSuccess) {
      keep(null);
      setAccepted(false);
      setEvidence("");
    }
  }
  async function submit() {
    if (!review || busy || disabled) return;
    const captured = { ...review, attempted: true };
    if (!keep(captured)) return;
    setBusy(true);
    setError("");
    try {
      const result = await resetApi.submit(
        workspace,
        csrfToken,
        captured.command,
      );
      setSaved(result);
      keep(null);
      setAccepted(false);
      setEvidence("");
      await cache.invalidateQueries({
        predicate: (item) =>
          item.queryKey[0] === "me" || item.queryKey.includes(workspace.id),
      });
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : "Reset outcome is unconfirmed. Retry the captured request.",
      );
    } finally {
      await query.refetch();
      setBusy(false);
    }
  }
  return (
    <section
      className="space-y-3 rounded border border-amber-300 p-4 min-w-0 break-words"
      aria-label="Synthetic demo reset"
    >
      <h3 className="font-semibold">Restart the synthetic accounting seed</h3>
      <p>
        This explicit action restores the original demo accounting source on the
        same invoice identities. It pauses outbound, disables policy automation
        and fences older work. It enables no provider or outreach.
      </p>
      <p>
        Prior cases, promises, tasks and collection holds remain. Consent
        withdrawals, deleted media and uncertain effects are preserved. Further
        review may be needed before repeating a story; history is never cleared.
      </p>
      {query.isPending ? (
        <p role="status">Reading current reset eligibility…</p>
      ) : query.isError ? (
        <ErrorState
          label="Reset state unavailable"
          error={query.error}
          retry={() => query.refetch()}
        />
      ) : (
        <>
          <p>
            Current generation {query.data.generation} ·{" "}
            {query.data.outbound_paused
              ? "Outbound paused"
              : "Tenant outbound resumed"}{" "}
            ·{" "}
            {query.data.automation_enabled
              ? "Policy automation enabled"
              : "Automation off"}
          </p>
          {!query.data.can_reset && (
            <ul>
              {query.data.eligibility_reasons.map((reason) => (
                <li key={reason}>
                  {eligibility[reason] ??
                    "Current source eligibility requires review."}
                </li>
              ))}
            </ul>
          )}
          {query.data.last_reset_at &&
            latest?.request_id === result?.request_id && (
              <p>Latest saved reset {stamp(query.data.last_reset_at)}</p>
            )}
          {result && <ResetSummary result={result} workspace={workspace.id} />}
          {!review && (
            <fieldset
              disabled={busy || disabled || !query.data.can_reset}
              className="space-y-3"
            >
              <label
                className="block"
                htmlFor={`reset-evidence-${workspace.id}`}
              >
                Reset review evidence
              </label>
              <textarea
                id={`reset-evidence-${workspace.id}`}
                className="w-full rounded border p-2"
                minLength={10}
                maxLength={1000}
                value={evidence}
                onChange={(event) => setEvidence(event.target.value)}
              />
              <label className="flex items-start gap-2">
                <input
                  type="checkbox"
                  checked={accepted}
                  onChange={(event) => setAccepted(event.target.checked)}
                />
                <span>
                  I reviewed that business history, holds and uncertain effects
                  remain, and outbound will be paused.
                </span>
              </label>
              <button
                type="button"
                className="rounded border px-3 py-2"
                disabled={!accepted || evidence.trim().length < 10}
                onClick={() => {
                  try {
                    if (keep(captureReset(query.data, evidence, accepted)))
                      setError("");
                  } catch (failure) {
                    setError(
                      failure instanceof Error
                        ? failure.message
                        : "Reset review unavailable.",
                    );
                  }
                }}
              >
                Capture reset review
              </button>
            </fieldset>
          )}
        </>
      )}
      {review && (
        <div
          className="space-y-2 rounded border p-3"
          aria-label="Captured reset review"
        >
          <p>
            Reviewed generation {review.command.expected_generation}. Captured{" "}
            {stamp(review.captured_at)}.
          </p>
          <p className="break-all">Request {review.command.request_id}</p>
          <p>{review.command.evidence}</p>
          {stale && (
            <p>
              Current workspace generation differs from this captured review.
              Refresh review before a new reset; retrying an unconfirmed request
              retains its exact original command.
            </p>
          )}
          <button
            type="button"
            className="rounded bg-amber-900 text-white px-3 py-2 max-w-full whitespace-normal"
            disabled={
              busy ||
              disabled ||
              !csrfToken ||
              (!review.attempted && (stale || !query.data?.can_reset))
            }
            onClick={() => void submit()}
          >
            {busy
              ? "Waiting for saved reset…"
              : review.attempted
                ? "Retry captured reset"
                : "Reset synthetic source"}
          </button>
        </div>
      )}
      <button
        type="button"
        disabled={busy || query.isFetching}
        onClick={() => void refresh()}
      >
        Refresh reset review
      </button>
      {busy && (
        <p role="status">
          Waiting for the committed source reset and refreshed workspace
          records…
        </p>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
