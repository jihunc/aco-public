"use client";
import Link from "next/link";
import { DemoResetPanel } from "./demo-reset";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { WorkspaceView } from "@/lib/generated-api";
import { ErrorState, stamp } from "@/features/receivables/shared";
import {
  canImportDemo,
  demoApi,
  type DemoImportedRecords,
  type DemoImportReceipt,
} from "./demo-api";
export function DemoAccountingResult({
  data,
  workspace,
}: {
  data: DemoImportedRecords;
  workspace: string;
}) {
  return (
    <div aria-label="Saved imported source records">
      <p>
        <strong>
          {data.invoices.total} imported invoices · {data.customers.total}{" "}
          imported customers
        </strong>
      </p>
      <p>
        Invoice records read {stamp(data.invoices.as_of_at)}; customer records
        read {stamp(data.customers.as_of_at)}. Counts come from saved workspace
        records, including any existing imports.
      </p>
      <p className="flex flex-wrap gap-4">
        <Link
          className="underline"
          href={`/receivables?workspace=${encodeURIComponent(workspace)}`}
        >
          Explore imported receivables →
        </Link>
        <Link
          className="underline"
          href={`/customers?workspace=${encodeURIComponent(workspace)}`}
        >
          Explore imported customers →
        </Link>
      </p>
    </div>
  );
}
export function DemoAccountingSetup({
  workspace,
  csrfToken,
  disabled = false,
}: {
  workspace: WorkspaceView;
  csrfToken: string;
  disabled?: boolean;
}) {
  const cache = useQueryClient();
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [receipt, setReceipt] = useState<DemoImportReceipt | null>(null);
  const query = useQuery({
    queryKey: ["onboarding-accounting", workspace.id],
    queryFn: ({ signal }) => demoApi.read(workspace.id, signal),
    retry: false,
  });
  async function importSource() {
    if (busy || disabled) return;
    setBusy(true);
    setError("");
    setReceipt(null);
    try {
      const saved = await demoApi.import(workspace, csrfToken);
      setReceipt(saved);
      await Promise.all(
        [
          "me",
          "operations",
          "receivables",
          "customers",
          "invoice",
          "customer",
          "impact",
        ].map((key) =>
          cache.invalidateQueries({
            queryKey: key === "me" ? [key] : [key, workspace.id],
          }),
        ),
      );
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : "The import outcome is unconfirmed. Read current saved records before retrying.",
      );
    } finally {
      await query.refetch();
      setBusy(false);
    }
  }
  return (
    <section
      className="space-y-3 rounded border p-4 min-w-0 break-words"
      aria-label="Synthetic accounting setup"
    >
      <h3 className="font-semibold">Explore synthetic accounting data</h3>
      <p>
        A fresh demo import uses the Demo Accounting Provider. It creates no
        real customer payment and enables no outreach or paid provider.
      </p>
      <p>
        Import is explicit. Existing source records and later source changes are
        preserved; this action never resets the workspace.
      </p>
      {query.isPending ? (
        <p role="status">Reading imported source records…</p>
      ) : query.isError ? (
        <ErrorState
          label="Imported records unavailable"
          error={query.error}
          retry={() => query.refetch()}
        />
      ) : (
        <DemoAccountingResult data={query.data} workspace={workspace.id} />
      )}
      {canImportDemo(workspace) ? (
        <button
          type="button"
          className="rounded bg-slate-900 px-4 py-2 text-white max-w-full whitespace-normal"
          disabled={busy || disabled || !csrfToken}
          onClick={() => void importSource()}
        >
          {busy ? "Importing synthetic source…" : "Import synthetic demo data"}
        </button>
      ) : (
        <p>
          The current Owner of a demo workspace can explicitly import its
          synthetic accounting source.
        </p>
      )}
      {busy && (
        <p role="status">
          Waiting for the source command and refreshed saved records…
        </p>
      )}
      {receipt && (
        <p role="status">
          Synthetic source setup confirmed. Inspect the refreshed saved records
          above; reload them if their current counts are unavailable.
        </p>
      )}
      {error && <p role="alert">{error}</p>}
      <button
        type="button"
        disabled={busy || query.isFetching}
        onClick={() => void query.refetch()}
      >
        Reload imported records
      </button>
      {receipt && (
        <details>
          <summary>Inspect synthetic source reference</summary>
          <p>
            Accounting connection {receipt.provider_connection_id} · Synthetic
          </p>
        </details>
      )}
      <DemoResetPanel
        workspace={workspace}
        csrfToken={csrfToken}
        disabled={disabled || busy}
      />
    </section>
  );
}
