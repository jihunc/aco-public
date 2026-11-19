"use client";
import Link from "next/link";
import { useState, useSyncExternalStore, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { InvoiceView, WorkspaceView } from "@/lib/generated-api";
import { formatMoney } from "@/lib/money";
import {
  paymentApi,
  preparePayment,
  SourcePaymentError,
  type PaymentReconciliation,
  type SourcePayment,
} from "./payment-api";

type Attempt = {
  command: SourcePayment;
  phase: "pending" | "rejected" | "confirmed";
  result: PaymentReconciliation | null;
};
const paymentEvent = "aco-source-payment-reference";
function subscribe(notify: () => void) {
  window.addEventListener("storage", notify);
  window.addEventListener(paymentEvent, notify);
  return () => {
    window.removeEventListener("storage", notify);
    window.removeEventListener(paymentEvent, notify);
  };
}
function readAttempt(raw: string | null): Attempt | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Attempt;
    if (
      !["pending", "rejected", "confirmed"].includes(value.phase) ||
      typeof value.command.operation_id !== "string" ||
      typeof value.command.amount.minor_units !== "string"
    )
      return null;
    return value;
  } catch {
    return null;
  }
}
export function PaymentResult({
  result,
  workspace,
}: {
  result: PaymentReconciliation;
  workspace: string;
}) {
  return (
    <section aria-label="Source payment result">
      <h3>
        {result.synthetic
          ? "Synthetic source payment recorded"
          : "Source payment result"}
      </h3>
      <p>
        Saved event {result.event_id} · Source revision {result.source_revision}
      </p>
      <p>
        At reconciliation: financial{" "}
        {result.financial_status.replaceAll("_", " ")} · Collection{" "}
        {result.collection_lifecycle.replaceAll("_", " ")}.
      </p>
      <p>
        Current balances and lifecycle are shown from the refreshed accounting
        read above. Case and promise histories remain independent.
      </p>
      <div className="communication-links">
        <Link href={`/overview?workspace=${encodeURIComponent(workspace)}`}>
          View refreshed Overview
        </Link>
        <Link href={`/review?workspace=${encodeURIComponent(workspace)}`}>
          Open Review Queue
        </Link>
      </div>
    </section>
  );
}
export function DemoPaymentPanel({
  workspace,
  invoice,
  csrfToken,
}: {
  workspace: WorkspaceView;
  invoice: InvoiceView;
  csrfToken: string;
}) {
  const cache = useQueryClient();
  const storageKey = `aco:demo-source-payment:${workspace.id}:${invoice.id}`;
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
  const attempt = readAttempt(raw);
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const allowed =
    workspace.permissions.includes("demo_payment") && workspace.is_demo;
  const eligible =
    allowed &&
    invoice.source.synthetic &&
    invoice.accounting_provider === "demo" &&
    invoice.verified &&
    ["open", "partially_paid"].includes(invoice.financial_status) &&
    BigInt(invoice.outstanding.minor_units) > 0n;
  const unresolved = attempt && attempt.phase !== "confirmed";
  function save(value: Attempt | null) {
    // Keep the operation reference before making a request, including across reloads.
    // This is a retry aid only; the server reauthorizes every actual source command.
    if (value) sessionStorage.setItem(storageKey, JSON.stringify(value));
    else sessionStorage.removeItem(storageKey);
    window.dispatchEvent(new Event(paymentEvent));
  }
  async function refresh() {
    await Promise.all(
      [
        "invoice",
        "receivables",
        "operations",
        "customer",
        "review-queue",
        "tasks",
        "scheduled-actions",
        "invoice-reminders",
        "cases",
        "promises",
        "usage",
      ].map((key) =>
        cache.invalidateQueries({ queryKey: [key, workspace.id] }),
      ),
    );
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || !allowed || (!eligible && !unresolved)) return;
    setBusy(true);
    setError("");
    let captured: SourcePayment | null = null;
    try {
      captured = unresolved
        ? attempt.command
        : preparePayment(null, invoice, amount);
      save({ command: captured, phase: "pending", result: null });
      const result = await paymentApi.simulate(
        workspace.id,
        captured,
        csrfToken,
      );
      save({ command: captured, phase: "confirmed", result });
      await refresh();
    } catch (reason) {
      if (captured && reason instanceof SourcePaymentError && reason.rejected) {
        save({ command: captured, phase: "rejected", result: null });
        void cache.invalidateQueries({ queryKey: ["me"] });
      }
      setError(
        reason instanceof Error
          ? reason.message
          : "Payment outcome unavailable. Keep this operation reference and inspect current accounting data.",
      );
    } finally {
      setBusy(false);
    }
  }
  if (
    !workspace.is_demo ||
    !invoice.source.synthetic ||
    invoice.accounting_provider !== "demo"
  )
    return null;
  return (
    <section className="panel" aria-label="Synthetic source payment">
      <h2>Simulate a payment in demo accounting</h2>
      <p>
        This records synthetic cash in the demo accounting source, then imports
        its verified result. Current source status and balances stay read only
        in ACO.
      </p>
      <p className="muted">
        No real ledger or external collection message is changed. Invoice
        payment does not by itself resolve an unrelated case or fulfil a
        promise.
      </p>
      {attempt?.phase === "confirmed" && attempt.result && (
        <PaymentResult result={attempt.result} workspace={workspace.id} />
      )}
      {!allowed ? (
        <p>
          Your current role can inspect source evidence. An Owner or Finance
          manager must record a demo source payment.
        </p>
      ) : (
        <>
          {unresolved && (
            <p role="status">
              {attempt.phase === "rejected"
                ? "Rejected captured operation"
                : "Unconfirmed captured operation"}
              : {attempt.command.operation_id} ·{" "}
              {formatMoney(attempt.command.amount)}. Retrying keeps this exact
              operation and amount.
            </p>
          )}
          {!eligible && !unresolved && (
            <p>
              A new demo payment requires a verified, positive open or partially
              paid source balance.
            </p>
          )}
          {(eligible || unresolved) && (
            <form className="task-form" onSubmit={submit}>
              <label>
                Demo payment amount ({invoice.outstanding.currency})
                <input
                  inputMode="decimal"
                  type="text"
                  maxLength={80}
                  required={!unresolved}
                  value={
                    unresolved ? formatDecimal(attempt.command.amount) : amount
                  }
                  disabled={
                    busy || !!unresolved || attempt?.phase === "confirmed"
                  }
                  onChange={(e) => setAmount(e.target.value)}
                  placeholder={`Up to ${formatMoney(invoice.outstanding)}`}
                />
              </label>
              <p>
                {invoice.outstanding.currency} · {invoice.outstanding.exponent}{" "}
                decimal places · Current source balance{" "}
                {formatMoney(invoice.outstanding)}
              </p>
              <button
                type="submit"
                disabled={
                  busy ||
                  (!unresolved &&
                    (!amount.trim() || attempt?.phase === "confirmed"))
                }
              >
                {busy
                  ? "Recording source payment…"
                  : unresolved
                    ? "Retry captured source payment"
                    : "Record synthetic source payment"}
              </button>
            </form>
          )}
          {attempt &&
            (attempt.phase === "rejected" || attempt.phase === "confirmed") && (
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  try {
                    save(null);
                    setAmount("");
                    setError("");
                  } catch {
                    setError(
                      "Browser session storage is unavailable. Keep the captured operation reference before retrying.",
                    );
                  }
                }}
              >
                {attempt.phase === "confirmed"
                  ? "Review another synthetic payment"
                  : "Review a new amount after rejection"}
              </button>
            )}
        </>
      )}
      <button type="button" disabled={busy} onClick={() => void refresh()}>
        Refresh persisted source and queue
      </button>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
function formatDecimal(value: {
  minor_units: string;
  exponent: number;
}): string {
  const digits = value.minor_units.padStart(value.exponent + 1, "0");
  return value.exponent
    ? `${digits.slice(0, -value.exponent)}.${digits.slice(-value.exponent)}`
    : digits;
}
