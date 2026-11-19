"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { WorkspaceView } from "@/lib/generated-api";
import { formatMoney } from "@/lib/money";
import { Button } from "@/components/ui/button";
import { ErrorState, stamp } from "@/features/receivables/shared";
import { AgentProgress } from "./progress";
import { AgentActivity } from "./activity";
import { agentApi, isActiveRun, type StartInvestigation } from "./api";
import "./agent.css";

export function AgentPanel({
  workspace,
  csrfToken,
  runId,
  initialInvoice,
}: {
  workspace: WorkspaceView;
  csrfToken: string;
  runId?: string;
  initialInvoice?: string;
}) {
  return runId ? (
    <InvestigationDetail
      key={`${workspace.id}-${runId}`}
      workspace={workspace}
      csrfToken={csrfToken}
      runId={runId}
    />
  ) : (
    <InvestigationHome
      key={`${workspace.id}-${initialInvoice ?? ""}`}
      workspace={workspace}
      csrfToken={csrfToken}
      initialInvoice={initialInvoice}
    />
  );
}
function InvestigationHome({
  workspace,
  csrfToken,
  initialInvoice,
}: {
  workspace: WorkspaceView;
  csrfToken: string;
  initialInvoice?: string;
}) {
  const list = useQuery({
    queryKey: ["agent-runs", workspace.id],
    queryFn: ({ signal }) => agentApi.list(workspace.id, signal),
    retry: false,
    refetchInterval: 4000,
  });
  return (
    <>
      <section className="panel agent-intro">
        <p className="eyebrow">Evidence before action</p>
        <h2>A clearer path to the next step</h2>
        <p>
          Investigate up to three invoices using their accounting, payment and
          communication evidence. Follow the saved tool activity and inspect the
          resulting findings.
        </p>
        <p className="muted">
          Closing this page does not cancel work. Proposed actions remain
          separate from verified business changes.
        </p>
      </section>
      {list.isPending ? (
        <p role="status">Loading investigations…</p>
      ) : list.isError ? (
        <ErrorState
          label="Investigations unavailable"
          error={list.error}
          retry={() => list.refetch()}
        />
      ) : (
        <>
          <NewInvestigation
            workspace={workspace}
            csrfToken={csrfToken}
            initialInvoice={initialInvoice}
          />
          <section className="panel">
            <div className="section-heading">
              <h2>Recent investigations</h2>
              <Button
                variant="outline"
                disabled={list.isFetching}
                onClick={() => list.refetch()}
              >
                Refresh investigations
              </Button>
            </div>
            {list.data.items.length === 0 ? (
              <p>No investigations have been started in this workspace.</p>
            ) : (
              <ul className="agent-run-list">
                {list.data.items.map((run) => (
                  <li key={run.id}>
                    <div>
                      <Link
                        href={`/agent-runs/${encodeURIComponent(run.id)}?workspace=${encodeURIComponent(workspace.id)}`}
                      >
                        {run.objective}
                      </Link>
                      <p className="muted">
                        {run.invoice_ids.length} selected invoice
                        {run.invoice_ids.length === 1 ? "" : "s"} ·{" "}
                        {stamp(run.created_at)}
                      </p>
                    </div>
                    <div className="agent-run-meta">
                      <span className="badge capitalize">
                        {run.status.replaceAll("_", " ")}
                      </span>
                      <span>
                        {run.authenticity === "FAKE"
                          ? "Simulated model"
                          : `Live model · ${run.model_id ?? "Identity unavailable"}`}
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </>
  );
}
function NewInvestigation({
  workspace,
  csrfToken,
  initialInvoice,
}: {
  workspace: WorkspaceView;
  csrfToken: string;
  initialInvoice?: string;
}) {
  const router = useRouter();
  const cache = useQueryClient();
  const [selection, setSelection] = useState<{ id: string; number: string }[]>(
    initialInvoice ? [{ id: initialInvoice, number: "" }] : [],
  );
  const [objective, setObjective] = useState("");
  const [search, setSearch] = useState("");
  const [applied, setApplied] = useState("");
  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pendingCommand = useRef<StartInvestigation | null>(null);
  const canStart = workspace.permissions.includes("investigate");
  const invoices = useQuery({
    queryKey: ["agent-invoice-selection", workspace.id, applied, page],
    queryFn: ({ signal }) =>
      api.invoices(
        workspace.id,
        { page, page_size: 20, search: applied || undefined },
        signal,
      ),
    retry: false,
  });
  const initial = useQuery({
    queryKey: ["invoice", workspace.id, initialInvoice],
    queryFn: ({ signal }) => api.invoice(workspace.id, initialInvoice!, signal),
    enabled: !!initialInvoice,
    retry: false,
  });
  const initialSelected =
    !!initialInvoice && selection.some((item) => item.id === initialInvoice);
  const selectionReady =
    selection.length >= 1 &&
    selection.length <= 3 &&
    (!initialSelected || initial.isSuccess);
  async function start() {
    if (!canStart || busy || !selectionReady || !objective.trim()) return;
    setBusy(true);
    setError("");
    const invoiceIds = selection.map((item) => item.id);
    const normalizedObjective = objective.trim();
    const previous = pendingCommand.current;
    const command =
      previous &&
      previous.objective === normalizedObjective &&
      previous.invoice_ids.join() === invoiceIds.join()
        ? previous
        : {
            invoice_ids: invoiceIds,
            objective: normalizedObjective,
            request_id: crypto.randomUUID(),
          };
    pendingCommand.current = command;
    try {
      const run = await agentApi.start(workspace.id, command, csrfToken);
      cache.setQueryData(["agent-run", workspace.id, run.id], run);
      await cache.invalidateQueries({ queryKey: ["agent-runs", workspace.id] });
      router.push(
        `/agent-runs/${encodeURIComponent(run.id)}?workspace=${encodeURIComponent(workspace.id)}`,
      );
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "The investigation could not be started. Check recent investigations before retrying.",
      );
      await cache.invalidateQueries({ queryKey: ["agent-runs", workspace.id] });
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel agent-composer" aria-label="New investigation">
      <div className="section-heading">
        <div>
          <h2>Start an investigation</h2>
          <p className="muted">
            Choose one to three invoices and give the investigator a specific
            objective.
          </p>
        </div>
        <span className="badge">Accounting stays read-only</span>
      </div>
      {!canStart && (
        <p className="agent-notice">
          Your current membership can inspect investigations. Investigation
          permission is required to start or cancel a run.
        </p>
      )}
      <form
        className="agent-search"
        onSubmit={(event) => {
          event.preventDefault();
          setApplied(search);
          setPage(1);
        }}
      >
        <label>
          Find invoices
          <input
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Invoice number or customer"
          />
        </label>
        <Button variant="outline" disabled={invoices.isFetching}>
          Search invoices
        </Button>
      </form>
      {invoices.isPending ? (
        <p role="status">Loading invoice choices…</p>
      ) : invoices.isError ? (
        <ErrorState
          label="Invoice choices unavailable"
          error={invoices.error}
          retry={() => invoices.refetch()}
        />
      ) : (
        <>
          <fieldset className="agent-invoices" disabled={busy || !canStart}>
            <legend>Select up to three invoices</legend>
            {invoices.data.items.length ? (
              invoices.data.items.map((invoice) => {
                const selected = selection.some(
                  (item) => item.id === invoice.id,
                );
                return (
                  <label
                    className={`agent-invoice-choice${selected ? " agent-selected" : ""}`}
                    key={invoice.id}
                  >
                    <input
                      type="checkbox"
                      aria-label={`Select ${invoice.number}`}
                      checked={selected}
                      disabled={!selected && selection.length >= 3}
                      onChange={() =>
                        setSelection(
                          selected
                            ? selection.filter((item) => item.id !== invoice.id)
                            : [
                                ...selection,
                                { id: invoice.id, number: invoice.number },
                              ],
                        )
                      }
                    />
                    <span>
                      <strong>{invoice.number}</strong>
                      <span>
                        {invoice.customer.name} · Due {invoice.due_date}
                      </span>
                      <small>
                        {invoice.source.synthetic ? "Synthetic" : "Observed"} ·{" "}
                        {invoice.freshness} source ·{" "}
                        {invoice.financial_status.replaceAll("_", " ")}
                      </small>
                    </span>
                    <strong>{formatMoney(invoice.outstanding)}</strong>
                  </label>
                );
              })
            ) : (
              <p>
                No invoices match your search. Import accounting data or change
                your search.
              </p>
            )}
          </fieldset>
          <nav className="pagination" aria-label="Invoice selection pages">
            <Button
              variant="outline"
              disabled={page <= 1 || invoices.isFetching}
              onClick={() => setPage(page - 1)}
            >
              Previous choices
            </Button>
            <span>
              Page {invoices.data.page} of{" "}
              {Math.max(
                1,
                Math.ceil(invoices.data.total / invoices.data.page_size),
              )}
            </span>
            <Button
              variant="outline"
              disabled={
                page * invoices.data.page_size >= invoices.data.total ||
                invoices.isFetching
              }
              onClick={() => setPage(page + 1)}
            >
              Next choices
            </Button>
          </nav>
        </>
      )}
      {workspace.is_demo && (
        <div className="space-y-2">
          <Button
            type="button"
            variant="outline"
            disabled={busy || !canStart}
            onClick={() =>
              setObjective(
                "Request an independently reviewed synthetic browser voice session",
              )
            }
          >
            Use synthetic voice-review objective
          </Button>
          <p className="muted">
            This requests a saved review only. A human must independently
            approve and explicitly start the browser conversation; capture stays
            disabled.{" "}
            <Link href={`/voice?workspace=${encodeURIComponent(workspace.id)}`}>
              Prepare the synthetic voice connection first
            </Link>
            . Select invoices from one customer.
          </p>
        </div>
      )}
      <div className="agent-selection" aria-live="polite">
        <p>{selection.length} of 3 selected</p>
        {selection.map((item) => (
          <span key={item.id}>
            <Link
              href={`/receivables/${encodeURIComponent(item.id)}?workspace=${encodeURIComponent(workspace.id)}`}
            >
              {item.number ||
                (initial.data?.invoice.id === item.id
                  ? initial.data.invoice.number
                  : `Invoice ${item.id.slice(0, 8)}`)}
            </Link>
            <button
              type="button"
              disabled={busy}
              aria-label={`Remove ${item.number || "selected invoice"}`}
              onClick={() =>
                setSelection(selection.filter((value) => value.id !== item.id))
              }
            >
              ×
            </button>
          </span>
        ))}
      </div>
      {initialSelected && initial.isPending && (
        <p role="status">Verifying the selected invoice…</p>
      )}
      {initialSelected && initial.isError && (
        <ErrorState
          label="Selected invoice unavailable"
          error={initial.error}
          retry={() => initial.refetch()}
        />
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void start();
        }}
      >
        <label className="agent-objective-label">
          Investigation objective
          <textarea
            aria-label="Investigation objective"
            value={objective}
            onChange={(event) => setObjective(event.target.value)}
            disabled={busy || !canStart}
            required
            maxLength={1000}
            rows={3}
            placeholder="For example: review payment evidence and recent communication for these invoices, and recommend the next safe step."
          />
          <span className="muted">{objective.length}/1,000 characters</span>
          <button
            type="button"
            disabled={busy || !canStart}
            onClick={() =>
              setObjective(
                "Prepare a synthetic customer email and request independent approval",
              )
            }
          >
            Use synthetic email review objective
          </button>
          <p className="muted">
            The simulated email workflow requires current company email setup
            and policy review. This shortcut sets an objective; it grants no
            sending authority.
          </p>
        </label>
        {error && (
          <p role="alert" aria-label="Start investigation failed">
            {error}
          </p>
        )}
        <div className="form-actions">
          <Button
            disabled={
              busy ||
              !canStart ||
              !selectionReady ||
              !objective.trim() ||
              invoices.isError
            }
            type="submit"
          >
            {busy ? "Starting investigation…" : "Start investigation"}
          </Button>
          <p className="muted">
            The server validates current permission and invoice scope.
          </p>
        </div>
      </form>
    </section>
  );
}
function InvestigationDetail({
  workspace,
  csrfToken,
  runId,
}: {
  workspace: WorkspaceView;
  csrfToken: string;
  runId: string;
}) {
  const cache = useQueryClient();
  const [cancelVersion, setCancelVersion] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const run = useQuery({
    queryKey: ["agent-run", workspace.id, runId],
    queryFn: ({ signal }) => agentApi.run(workspace.id, runId, signal),
    retry: false,
    refetchInterval: (query) =>
      query.state.data && !isActiveRun(query.state.data.status) ? false : 2000,
  });
  const steps = useQuery({
    queryKey: ["agent-steps", workspace.id, runId],
    queryFn: ({ signal }) => agentApi.steps(workspace.id, runId, signal),
    retry: false,
    refetchInterval: run.data && !isActiveRun(run.data.status) ? false : 2000,
  });
  const status = run.data?.status;
  useEffect(() => {
    if (!status) return;
    void cache.invalidateQueries({
      queryKey: ["agent-steps", workspace.id, runId],
    });
    if (!isActiveRun(status)) {
      for (const family of [
        "agent-runs",
        "review-queue",
        "tasks",
        "invoice",
        "receivables",
        "operations",
        "communications",
        "usage",
        "cases",
        "case",
        "promises",
        "promise",
        "task",
        "communication",
        "contact-stops",
      ])
        void cache.invalidateQueries({ queryKey: [family, workspace.id] });
    }
  }, [cache, workspace.id, runId, status]);
  async function refresh() {
    await Promise.all([run.refetch(), steps.refetch()]);
  }
  async function cancel() {
    if (cancelVersion === null || busy) return;
    setBusy(true);
    setError("");
    try {
      const current = await agentApi.cancel(
        workspace.id,
        runId,
        cancelVersion,
        csrfToken,
      );
      cache.setQueryData(["agent-run", workspace.id, runId], current);
      setCancelVersion(null);
      await steps.refetch();
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Cancellation failed. Refresh the investigation.",
      );
      setCancelVersion(null);
      await refresh();
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="agent-controls">
        <Link href={`/agents?workspace=${encodeURIComponent(workspace.id)}`}>
          ← All investigations
        </Link>
        <div>
          <Button
            variant="outline"
            disabled={run.isFetching || steps.isFetching}
            onClick={() => {
              setCancelVersion(null);
              void refresh();
            }}
          >
            Refresh progress
          </Button>
          {run.data &&
            !run.isError &&
            isActiveRun(run.data.status) &&
            workspace.permissions.includes("investigate") && (
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => {
                  setError("");
                  setCancelVersion(run.data.version);
                }}
              >
                Cancel investigation
              </Button>
            )}
        </div>
      </div>
      {run.data &&
      !run.isError &&
      steps.data &&
      !steps.isError &&
      isActiveRun(run.data.status) ? (
        <AgentProgress
          workspace={workspace.id}
          runId={run.data.id}
          watermark={steps.data.last_event_sequence}
        />
      ) : (
        <p className="muted agent-refresh-note">
          Saved progress loads from durable records. Closing this page leaves
          active work running.
        </p>
      )}
      {error && (
        <p role="alert" aria-label="Cancellation failed">
          {error}
        </p>
      )}
      {cancelVersion !== null && (
        <section
          className="panel agent-cancel"
          role="alertdialog"
          aria-labelledby="cancel-investigation-title"
          aria-describedby="cancel-investigation-description"
        >
          <h2 id="cancel-investigation-title">Cancel this investigation?</h2>
          <p id="cancel-investigation-description">
            Stop further work at the next server guard. Already committed
            results and usage remain in the history.
          </p>
          <div className="form-actions">
            <Button
              autoFocus
              variant="outline"
              disabled={busy}
              onClick={() => setCancelVersion(null)}
            >
              Keep investigating
            </Button>
            <Button disabled={busy} onClick={() => void cancel()}>
              {busy ? "Cancelling…" : "Confirm cancellation"}
            </Button>
          </div>
        </section>
      )}
      {run.isPending ? (
        <p role="status">Loading investigation…</p>
      ) : run.isError ? (
        <ErrorState
          label="Investigation unavailable"
          error={run.error}
          retry={() => run.refetch()}
        />
      ) : steps.isPending ? (
        <p role="status">Loading saved activity…</p>
      ) : steps.isError ? (
        <ErrorState
          label="Investigation activity unavailable"
          error={steps.error}
          retry={() => steps.refetch()}
        />
      ) : (
        <AgentActivity
          run={run.data}
          steps={steps.data.items}
          workspace={workspace.id}
        />
      )}
    </>
  );
}
