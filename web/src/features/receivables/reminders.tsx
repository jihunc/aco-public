"use client";
import { useQuery } from "@tanstack/react-query";
import { readApi } from "@/lib/api";
import type { ScheduledAction } from "@/features/communications/api";
import { ErrorState, stamp } from "./shared";
export function InvoiceReminderHistory({
  workspace,
  invoiceId,
}: {
  workspace: string;
  invoiceId: string;
}) {
  const query = useQuery({
    queryKey: ["invoice-reminders", workspace, invoiceId],
    queryFn: ({ signal }) =>
      readApi<ScheduledAction[]>(
        `/api/workspaces/${encodeURIComponent(workspace)}/scheduled-actions?kind=policy_due`,
        signal,
      ),
    retry: false,
  });
  return (
    <section className="panel" aria-label="Invoice reminder history">
      <h3>Recent reminder actions</h3>
      <p>
        Saved policy reminder history for this invoice. Only the server records
        an action as cancelled; an in-flight delivery cannot be recalled here.
      </p>
      {query.isPending ? (
        <p role="status">Loading saved reminder history…</p>
      ) : query.isError ? (
        <ErrorState
          label="Reminder history unavailable"
          error={query.error}
          retry={() => query.refetch()}
        />
      ) : (
        <>
          {query.data.filter((row) => row.invoice_id === invoiceId).length ===
          0 ? (
            <p>No recent reminder action is listed for this invoice.</p>
          ) : (
            <ul>
              {query.data
                .filter((row) => row.invoice_id === invoiceId)
                .map((row) => (
                  <li key={row.id}>
                    <strong>{row.status.replaceAll("_", " ")}</strong> · Due{" "}
                    {stamp(row.due_at)}
                    <p>
                      Action {row.id} · Version {row.version}
                    </p>
                    {row.last_error && <p>{row.last_error}</p>}
                  </li>
                ))}
            </ul>
          )}
        </>
      )}
      <button
        type="button"
        disabled={query.isFetching}
        onClick={() => void query.refetch()}
      >
        Refresh reminder history
      </button>
    </section>
  );
}
