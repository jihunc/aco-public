import Link from "next/link";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import type { InvoiceView } from "@/lib/generated-api";
export const stamp = (value: string | null | undefined) =>
  value
    ? new Intl.DateTimeFormat("en-AU", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: "UTC",
      }).format(new Date(value)) + " UTC"
    : "Unavailable";
export function ErrorState({
  label,
  error,
  retry,
}: {
  label: string;
  error: Error;
  retry: () => unknown;
}) {
  return (
    <section className="panel error-panel">
      <p role="alert">
        <strong>{label}.</strong> {error.message}
      </p>
      <Button
        onClick={() => {
          retry();
        }}
      >
        Try again
      </Button>
      <Link href="/login">Sign in</Link>
    </section>
  );
}
export function Unavailable({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section className="panel">
      <h2>{title}</h2>
      <p>{children}</p>
      <span className="badge">Unavailable</span>
    </section>
  );
}
export function SourceBadges({ invoice }: { invoice: InvoiceView }) {
  return (
    <div className="badges">
      <span className="badge">Financial: {invoice.financial_status}</span>
      <span className="badge">
        Collection: {invoice.collection_lifecycle ?? "unavailable"}
      </span>
      {invoice.hold_reasons?.map((h) => (
        <span className="badge" key={h}>
          Hold: {h}
        </span>
      ))}
      {invoice.hold_reasons === null && (
        <span className="badge">Collection holds unavailable</span>
      )}
      <span className={`badge freshness-${invoice.freshness}`}>
        {invoice.freshness}
      </span>
    </div>
  );
}
