"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { InvoiceView, WorkspaceView } from "@/lib/generated-api";
import { formatMoney } from "@/lib/money";
import { ErrorState, stamp } from "@/features/receivables/shared";
import { agentApi, isActiveRun, type AgentRun } from "@/features/agent/api";
import {
  IngressCommandError,
  ingressApi,
  prepareSimulation,
  type IngressActivation,
  type IngressReceipt,
  type IngressScenario,
  type PreparedSimulation,
} from "./ingress-api";

const recordHref = (path: string, workspace: string) =>
  `${path}?workspace=${encodeURIComponent(workspace)}`;
const receiptLabels: Record<string, string> = {
  queued: "Queued",
  fetching: "Capturing source",
  processed: "Processing finished",
  review_required: "Human review required",
  failed: "Processing failed",
};
export function IngressReceiptRecord({
  receipt,
  workspace,
}: {
  receipt: IngressReceipt;
  workspace: string;
}) {
  return (
    <section
      className="panel"
      aria-label={
        receipt.synthetic
          ? "Synthetic inbound receipt"
          : "Provider inbound receipt"
      }
    >
      <h3>
        {receiptLabels[receipt.state] ?? receipt.state.replaceAll("_", " ")}
      </h3>
      <p className="muted">
        {receipt.synthetic ? "Synthetic sample" : "Provider notification"} ·
        Receipt {receipt.id} · Version {receipt.version}
      </p>
      <p>
        Sender identity remains unverified. No sensitive action authority is
        granted by this message.
      </p>
      {receipt.review_required && <p>This receipt requires human review.</p>}
      {receipt.error_code && (
        <p role="alert">
          Processing stopped: {receipt.error_code}. Review the source and
          current setup before retrying.
        </p>
      )}
      <div className="button-row">
        {receipt.communication_id && (
          <Link
            href={recordHref(
              `/communications/${encodeURIComponent(receipt.communication_id)}`,
              workspace,
            )}
          >
            Inspect captured conversation and source
          </Link>
        )}
        {receipt.agent_run_id && (
          <Link
            href={recordHref(
              `/agent-runs/${encodeURIComponent(receipt.agent_run_id)}`,
              workspace,
            )}
          >
            Open AI investigation
          </Link>
        )}
      </div>
      {!receipt.communication_id && (
        <p>The receipt has no conversation record yet.</p>
      )}
      {!receipt.agent_run_id && (
        <p>
          No investigation is recorded for this receipt. Unlinked replies and
          outbound echoes do not start one.
        </p>
      )}
    </section>
  );
}
export function InboundOutcomeRecord({
  run,
  workspace,
}: {
  run: AgentRun;
  workspace: string;
}) {
  return (
    <section className="panel" aria-label="Inbound investigation outcome">
      <h3>
        {run.authenticity === "FAKE"
          ? "Simulated model"
          : `Model: ${run.model_id ?? "Unavailable"}`}
      </h3>
      <p>Investigation {run.status.replaceAll("_", " ")}</p>
      {run.error_code && (
        <p role="alert">Investigation stopped: {run.error_code}</p>
      )}
      {run.result && (
        <>
          <p>{run.result.summary}</p>
          <ul>
            {run.result.proposals.map((proposal, index) => (
              <li key={index}>
                <strong>
                  {proposal.executed ? "Executed" : "Proposed, unexecuted"}:{" "}
                  {proposal.title ?? proposal.kind.replaceAll("_", " ")}
                </strong>
                <p>{proposal.reason}</p>
                {proposal.executed &&
                  proposal.kind === "request_collection_hold" && (
                    <p>
                      <Link
                        href={`/receivables/${encodeURIComponent(proposal.invoice_id)}?workspace=${encodeURIComponent(workspace)}`}
                      >
                        View current contact restrictions
                      </Link>
                    </p>
                  )}
                {proposal.executed &&
                  (proposal.changed_ids ?? []).map((id) => {
                    const path =
                      proposal.kind === "create_task"
                        ? "tasks"
                        : proposal.kind === "upsert_case"
                          ? "cases"
                          : proposal.kind === "record_promise"
                            ? "promises"
                            : null;
                    return path ? (
                      <p key={id}>
                        <Link
                          href={recordHref(
                            `/${path}/${encodeURIComponent(id)}`,
                            workspace,
                          )}
                        >
                          Open{" "}
                          {path === "tasks"
                            ? "task"
                            : path === "cases"
                              ? "case"
                              : "promise"}{" "}
                          {id}
                        </Link>
                      </p>
                    ) : null;
                  })}
              </li>
            ))}
          </ul>
        </>
      )}
      <Link
        href={recordHref(
          `/agent-runs/${encodeURIComponent(run.id)}`,
          workspace,
        )}
      >
        Read persisted investigation steps and evidence
      </Link>
    </section>
  );
}
function ReceiptProgress({
  workspace,
  receiptId,
}: {
  workspace: string;
  receiptId: string;
}) {
  const cache = useQueryClient();
  const receipt = useQuery({
    queryKey: ["ingress-receipt", workspace, receiptId],
    queryFn: ({ signal }) => ingressApi.receipt(workspace, receiptId, signal),
    retry: false,
    refetchInterval: (query) =>
      query.state.data &&
      ["queued", "fetching"].includes(query.state.data.state)
        ? 2000
        : false,
  });
  const runId = receipt.data?.agent_run_id;
  const run = useQuery({
    queryKey: ["agent-run", workspace, runId],
    enabled: !!runId,
    queryFn: ({ signal }) => agentApi.run(workspace, runId!, signal),
    retry: false,
    refetchInterval: (query) =>
      query.state.data && isActiveRun(query.state.data.status) ? 2000 : false,
  });
  const completedRunId =
    run.data && !isActiveRun(run.data.status) ? run.data.id : null;
  const completedRunVersion = completedRunId ? run.data?.version : null;
  useEffect(() => {
    if (!completedRunId) return;
    for (const key of [
      "tasks",
      "review-queue",
      "agent-runs",
      "communications",
      "invoices",
      "invoice",
      "cases",
      "promises",
    ]) {
      void cache.invalidateQueries({ queryKey: [key, workspace] });
    }
  }, [cache, workspace, completedRunId, completedRunVersion]); // Committed terminal records only.
  return (
    <>
      {receipt.isPending ? (
        <p role="status">Loading persisted inbound receipt…</p>
      ) : receipt.isError ? (
        <ErrorState
          label="Receipt unavailable"
          error={receipt.error}
          retry={() => receipt.refetch()}
        />
      ) : (
        <IngressReceiptRecord receipt={receipt.data} workspace={workspace} />
      )}
      {runId &&
        (run.isPending ? (
          <p role="status">Loading persisted investigation…</p>
        ) : run.isError ? (
          <ErrorState
            label="Investigation unavailable"
            error={run.error}
            retry={() => run.refetch()}
          />
        ) : (
          <InboundOutcomeRecord run={run.data} workspace={workspace} />
        ))}
      <button
        type="button"
        onClick={() => {
          void receipt.refetch();
          if (runId) void run.refetch();
        }}
      >
        Refresh receipt and investigation
      </button>
      <p className="muted">
        Progress comes from saved server records. You can reload this page;
        leaving it does not cancel processing.
      </p>
    </>
  );
}
export function SyntheticInboundPanel({
  workspace,
  csrfToken,
  receiptId,
}: {
  workspace: WorkspaceView;
  csrfToken: string;
  receiptId?: string;
}) {
  const router = useRouter();
  const cache = useQueryClient();
  const owner = workspace.permissions.includes("settings");
  const setup = useQuery({
    queryKey: ["ingress-setup", workspace.id],
    enabled: owner && workspace.is_demo,
    queryFn: ({ signal }) => ingressApi.setup(workspace.id, signal),
    retry: false,
  });
  const [email, setEmail] = useState("");
  const [storage, setStorage] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [reviewReset, setReviewReset] = useState(false);
  const [selectedActivation, setSelectedActivation] = useState("");
  const [reviewed, setReviewed] = useState<IngressActivation | null>(null);
  const [needsReview, setNeedsReview] = useState(false);
  const [scenario, setScenario] = useState<IngressScenario>("invoice_reply");
  const [selection, setSelection] = useState<InvoiceView[]>([]);
  const [search, setSearch] = useState("");
  const [applied, setApplied] = useState("");
  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const [error, setError] = useState("");
  // A single ready connection is the only valid choice; preselect it without extra state.
  const emailOptions = setup.data?.email_connections;
  const storageOptions = setup.data?.storage_connections;
  const syntheticActivations = setup.data?.activations.filter((a) => a.synthetic);
  const activationChoice =
    selectedActivation || (syntheticActivations?.length === 1 ? syntheticActivations[0].id : "");
  const emailChoice = email || (emailOptions?.length === 1 ? emailOptions[0].id : "");
  const storageChoice = storage || (storageOptions?.length === 1 ? storageOptions[0].id : "");
  const [createdReceipt, setCreatedReceipt] = useState<string | null>(null);
  const pending = useRef<PreparedSimulation | null>(null);
  const preparation = useRef<{ request_id: string; evidence: string } | null>(
    null,
  );
  const currentReceipt = receiptId ?? createdReceipt;
  const canCompose =
    owner && workspace.is_demo && !!setup.data?.enabled && !currentReceipt;
  const invoices = useQuery({
    queryKey: ["ingress-invoices", workspace.id, applied, page],
    enabled: canCompose,
    queryFn: ({ signal }) =>
      api.invoices(
        workspace.id,
        { page, page_size: 20, search: applied || undefined },
        signal,
      ),
    retry: false,
  });
  const currentReview =
    !needsReview &&
    !!reviewed &&
    reviewed.synthetic &&
    !reviewed.revoked_at &&
    Date.parse(reviewed.expires_at) > now &&
    !!setup.data?.activations.some(
      (a) => a.id === reviewed.id && a.version === reviewed.version,
    );
  const first = selection[0];
  async function prepareInbox() {
    if (!owner || busy || !setup.data?.can_prepare) return;
    setBusy(true);
    setError("");
    const command = preparation.current ?? {
      request_id: crypto.randomUUID(),
      evidence:
        "Owner explicitly requested a demo-only synthetic inbox and verified private source storage preparation; no outbound message or collection authority is granted.",
    };
    preparation.current = command;
    try {
      const actual = await ingressApi.prepare(workspace.id, command, csrfToken);
      cache.setQueryData(["ingress-setup", workspace.id], actual);
      await setup.refetch();
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Inbox preparation could not be confirmed. Refresh setup to inspect current connections.",
      );
      await setup.refetch();
    } finally {
      setBusy(false);
    }
  }
  async function activate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canCompose || busy || !acknowledged || !emailChoice || !storageChoice) return;
    setBusy(true);
    setError("");
    try {
      const actual = await ingressApi.activate(
        workspace.id,
        {
          email_connection_id: emailChoice,
          storage_connection_id: storageChoice,
          expires_in_seconds: 3600,
          evidence:
            "Owner explicitly reviewed this synthetic inbound sample and its private source capture; sender remains unverified and no sensitive action is authorized.",
        },
        csrfToken,
      );
      setReviewed({ ...actual });
      setNeedsReview(false);
      pending.current = null;
      setSelectedActivation(actual.id);
      setAcknowledged(false);
      await setup.refetch();
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Activation could not be confirmed. Refresh setup before trying again.",
      );
    } finally {
      setBusy(false);
    }
  }
  async function simulate() {
    if (
      !canCompose ||
      busy ||
      !reviewed ||
      !currentReview ||
      selection.length < 1 ||
      selection.length > 3
    )
      return;
    setBusy(true);
    setError("");
    const prepared = prepareSimulation(
      pending.current,
      reviewed,
      selection.map((i) => i.id),
      scenario,
    );
    pending.current = prepared;
    try {
      const receipt = await ingressApi.simulate(
        workspace.id,
        prepared.activation_id,
        prepared.command,
        csrfToken,
      );
      cache.setQueryData(
        ["ingress-receipt", workspace.id, receipt.id],
        receipt,
      );
      setCreatedReceipt(receipt.id);
      router.replace(
        `${recordHref("/settings/email", workspace.id)}&receipt=${encodeURIComponent(receipt.id)}`,
      );
    } catch (reason) {
      if (
        reason instanceof IngressCommandError &&
        [403, 404, 409].includes(reason.status)
      ) {
        setNeedsReview(true);
        await setup.refetch();
      }
      setError(
        reason instanceof Error
          ? reason.message
          : "The sample could not be confirmed. Retry the same selection to retain its request identity.",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel" aria-label="Synthetic inbound email">
      <h2>Try a synthetic inbound reply</h2>
      <p>
        Use verified demo invoices to capture a fixed synthetic message and
        inspect its saved source. A linked invoice reply can start a Simulated
        model investigation and create a review task.
      </p>
      <p className="muted">
        Demo only. Sender identity remains unverified. No external message is
        sent, no paid model is selected, and no sensitive action is authorized
        by this sample.
      </p>
      {currentReceipt && (
        <>
          <ReceiptProgress
            workspace={workspace.id}
            receiptId={currentReceipt}
          />
          {owner && (
            <button
              type="button"
              onClick={() => {
                setCreatedReceipt(null);
                pending.current = null;
                setReviewed(null);
                setSelection([]);
                router.replace(recordHref("/settings/email", workspace.id));
              }}
            >
              Start a new synthetic sample
            </button>
          )}
        </>
      )}
      {!currentReceipt &&
        (!workspace.is_demo ? (
          <p>Synthetic inbound samples are available in demo workspaces.</p>
        ) : !owner ? (
          <p>
            An Owner must review the current inbound activation to create a
            synthetic sample.
          </p>
        ) : setup.isPending ? (
          <p role="status">Loading current inbound setup…</p>
        ) : setup.isError ? (
          <ErrorState
            label="Inbound setup unavailable"
            error={setup.error}
            retry={() => setup.refetch()}
          />
        ) : (
          <>
            <button
              type="button"
              disabled={busy || setup.isFetching}
              onClick={() => void setup.refetch()}
            >
              Refresh inbound setup
            </button>
            {setup.data?.can_prepare && (
              <section aria-label="Prepare synthetic inbox">
                <h3>Prepare this demo inbox</h3>
                <p>
                  Check private source storage and prepare the synthetic email
                  connection. This performs a real storage check and records its
                  usage. It does not activate inbound capture or send mail.
                </p>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void prepareInbox()}
                >
                  {busy ? "Preparing…" : "Prepare synthetic inbox"}
                </button>
              </section>
            )}
            {!setup.data?.enabled ? (
              <p>
                Synthetic inbound is unavailable. The demo setting and eligible
                email and storage connections must be ready.
              </p>
            ) : (
              <>
                <section aria-label="Review inbound activation">
                  <h3>Review source capture</h3>
                  {setup.data.activations.length > 0 && (
                    <>
                      <label htmlFor="ingress-activation">
                        Current inbound activation
                      </label>
                      <select
                        id="ingress-activation"
                        disabled={busy}
                        value={activationChoice}
                        onChange={(e) => {
                          setSelectedActivation(e.target.value);
                          setReviewed(null);
                          pending.current = null;
                        }}
                      >
                        <option value="">Choose a current activation</option>
                        {setup.data.activations
                          .filter((a) => a.synthetic)
                          .map((a) => (
                            <option key={a.id} value={a.id}>
                              {a.id} · expires {stamp(a.expires_at)}
                            </option>
                          ))}
                      </select>
                      <button
                        type="button"
                        disabled={
                          busy ||
                          !activationChoice ||
                          !setup.data.activations.some(
                            (a) => a.id === activationChoice,
                          )
                        }
                        onClick={() => {
                          const actual = setup.data?.activations.find(
                            (a) => a.id === activationChoice,
                          );
                          if (actual) {
                            setReviewed({ ...actual });
                            setNeedsReview(false);
                            pending.current = null;
                          }
                        }}
                      >
                        Review selected activation
                      </button>
                    </>
                  )}
                  <details open={setup.data.activations.length === 0}>
                    <summary>
                      Create a one-hour synthetic inbound activation
                    </summary>
                    <form onSubmit={activate} className="task-form">
                      <label htmlFor="ingress-email">
                        Demo email connection
                      </label>
                      <select
                        id="ingress-email"
                        required
                        value={emailChoice}
                        disabled={busy}
                        onChange={(e) => {
                          setEmail(e.target.value);
                          setReviewReset(acknowledged);
                          setAcknowledged(false);
                        }}
                      >
                        <option value="">Choose a ready connection</option>
                        {setup.data.email_connections.map((c) => (
                          <option key={c.id} value={c.id}>
                            {c.provider} · {c.id} · {c.status}
                          </option>
                        ))}
                      </select>
                      <label htmlFor="ingress-storage">
                        Private source storage
                      </label>
                      <select
                        id="ingress-storage"
                        required
                        value={storageChoice}
                        disabled={busy}
                        onChange={(e) => {
                          setStorage(e.target.value);
                          setReviewReset(acknowledged);
                          setAcknowledged(false);
                        }}
                      >
                        <option value="">Choose a ready connection</option>
                        {setup.data.storage_connections.map((c) => (
                          <option key={c.id} value={c.id}>
                            {c.provider} · {c.id} · {c.status}
                          </option>
                        ))}
                      </select>
                      <label>
                        <input
                          type="checkbox"
                          disabled={busy}
                          checked={acknowledged}
                          onChange={(e) => {
                            setAcknowledged(e.target.checked);
                            setReviewReset(false);
                          }}
                        />
                        I reviewed this synthetic private capture. The sender
                        remains unverified and the sample grants no sensitive
                        action authority.
                      </label>
                      <button
                        type="submit"
                        disabled={busy || !emailChoice || !storageChoice || !acknowledged}
                      >
                        Enable synthetic inbound for one hour
                      </button>
                      {reviewReset && (
                        <p role="status" className="field-hint">
                          Selection changed — review again before enabling.
                        </p>
                      )}
                    </form>
                  </details>
                  {reviewed && (
                    <section aria-label="Reviewed inbound activation">
                      <p>
                        Reviewed activation {reviewed.id} · Version{" "}
                        {reviewed.version} · Expires{" "}
                        {stamp(reviewed.expires_at)}
                      </p>
                      {!currentReview && (
                        <p role="alert">
                          This captured activation is no longer current. Refresh
                          setup and explicitly review a current activation.
                        </p>
                      )}
                    </section>
                  )}
                </section>
                <h3>Select source invoices</h3>
                <p>
                  Choose one to three verified synthetic invoices for the same
                  customer and accounting connection.
                </p>
                <form
                  className="filter-grid"
                  onSubmit={(e) => {
                    e.preventDefault();
                    setApplied(search.trim());
                    setPage(1);
                  }}
                >
                  <label>
                    Find demo invoices
                    <input
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                    />
                  </label>
                  <button type="submit">Find invoices</button>
                </form>
                {selection.length > 0 && (
                  <ul aria-label="Selected inbound invoices">
                    {selection.map((i) => (
                      <li key={i.id}>
                        {i.number} · {i.customer.name}{" "}
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => {
                            setSelection(
                              selection.filter((item) => item.id !== i.id),
                            );
                            pending.current = null;
                          }}
                        >
                          Remove {i.number}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                {invoices.isPending ? (
                  <p role="status">Loading actual accounting invoices…</p>
                ) : invoices.isError ? (
                  <ErrorState
                    label="Invoice selection unavailable"
                    error={invoices.error}
                    retry={() => invoices.refetch()}
                  />
                ) : (
                  <>
                    <fieldset className="agent-invoices" disabled={busy}>
                      <legend>Verified synthetic source selection</legend>
                      {invoices.data.items.length === 0 && (
                        <p>
                          No invoices match. Import demo accounting data or
                          change the search.
                        </p>
                      )}
                      {invoices.data.items.map((invoice) => {
                        const selected = selection.some(
                          (i) => i.id === invoice.id,
                        );
                        const eligible =
                          invoice.source.synthetic &&
                          invoice.verified &&
                          invoice.accounting_provider === "demo";
                        const sameScope =
                          !first ||
                          (first.customer.id === invoice.customer.id &&
                            first.source.connection_id ===
                              invoice.source.connection_id);
                        const reason = !eligible
                          ? "Requires a verified demo source"
                          : !sameScope
                            ? "Choose the same customer and accounting connection"
                            : selection.length >= 3 && !selected
                              ? "Three invoices selected"
                              : "Verified synthetic source";
                        return (
                          <label
                            key={invoice.id}
                            className={`agent-invoice-choice${selected ? " agent-selected" : ""}`}
                          >
                            <input
                              type="checkbox"
                              aria-label={`Select inbound ${invoice.number}`}
                              checked={selected}
                              disabled={
                                !selected &&
                                (!eligible ||
                                  !sameScope ||
                                  selection.length >= 3)
                              }
                              onChange={() => {
                                setSelection(
                                  selected
                                    ? selection.filter(
                                        (i) => i.id !== invoice.id,
                                      )
                                    : [...selection, invoice],
                                );
                                pending.current = null;
                              }}
                            />
                            <span>
                              <strong>{invoice.number}</strong>
                              <span>{invoice.customer.name}</span>
                              <small>{reason}</small>
                            </span>
                            <strong>{formatMoney(invoice.outstanding)}</strong>
                          </label>
                        );
                      })}
                    </fieldset>
                    <nav
                      className="pagination"
                      aria-label="Inbound invoice pages"
                    >
                      <button
                        type="button"
                        disabled={busy || page <= 1 || invoices.isFetching}
                        onClick={() => setPage(page - 1)}
                      >
                        Previous
                      </button>
                      <span>
                        Page {page} · {invoices.data.total} invoices
                      </span>
                      <button
                        type="button"
                        disabled={
                          busy ||
                          invoices.isFetching ||
                          page * invoices.data.page_size >= invoices.data.total
                        }
                        onClick={() => setPage(page + 1)}
                      >
                        Next
                      </button>
                    </nav>
                  </>
                )}
                <label htmlFor="ingress-scenario">
                  Synthetic message scenario
                </label>
                <select
                  id="ingress-scenario"
                  disabled={busy}
                  value={scenario}
                  onChange={(e) => {
                    setScenario(e.target.value as IngressScenario);
                    pending.current = null;
                  }}
                >
                  <option value="invoice_reply">
                    Invoice reply · investigate linked invoices
                  </option>
                  <option value="ambiguous_promise_reply">
                    Synthetic promise with unresolved date
                  </option>
                  <option value="promise_reply">
                    Synthetic promise · AUD 40 in seven days
                  </option>
                  <option value="multi_promise_reply">
                    Synthetic multi-invoice promise · AUD 100 per invoice
                  </option>
                  <option value="multi_promise_amendment">
                    Synthetic revised promise · AUD 75 per invoice
                  </option>
                  <option value="installment_promise_reply">
                    Synthetic installment promise · two explicit payment dates
                  </option>
                  <option value="three_intent_reply">
                    Synthetic three-invoice promise, dispute and payment claim
                  </option>
                  <option value="contact_stop_reply">
                    Synthetic customer contact-stop request
                  </option>
                  <option value="case_reply">
                    Synthetic PO dispute · source-backed case
                  </option>
                  <option value="case_resolution">
                    Synthetic PO confirmation · documentary resolution
                  </option>
                  <option value="unverified_reference_reply">
                    Unknown sender with invoice references · human link review
                  </option>
                  <option value="unlinked_reply">
                    Unlinked reply · human review, no investigation
                  </option>
                  <option value="outbound_echo">
                    Outbound echo · preserve source, no investigation
                  </option>
                </select>
                <p>
                  {scenario === "invoice_reply"
                    ? "The server creates a fixed synthetic reply naming the selected invoices. The worker may create an internal review task; inspect its committed result below."
                    : scenario === "ambiguous_promise_reply"
                      ? "Fixed synthetic AUD40 promise for one invoice; no date or timezone is inferred."
                      : scenario === "promise_reply"
                        ? "Select one synthetic AUD invoice. The fixed reply states a synthetic AUD 40 commitment due seven days after capture in UTC. It is not a payment; authorized human confirmation is required."
                        : scenario === "multi_promise_reply"
                          ? "Select two or three verified synthetic AUD invoices for one customer. The fixed incoming statement explicitly allocates AUD 100 to each invoice, due seven days after capture in UTC; it is not a payment."
                          : scenario === "multi_promise_amendment"
                            ? "Select the same two or three synthetic AUD invoices. The fixed new statement proposes AUD 75 per invoice, due fourteen days after capture in UTC. Capturing it does not replace an active promise; separate human confirmation must review both versions."
                            : scenario === "installment_promise_reply"
                              ? "Select two or three verified synthetic AUD invoices for one customer. The fixed incoming statement commits AUD 50 per invoice in each of two installments, due seven and fourteen days after capture in UTC. Explicit human allocation/date review and confirmation are still required."
                              : scenario === "three_intent_reply"
                                ? "Select canonical INV-101, INV-107 and INV-113: one fixed stored source states an AUD 100.00 promise/date, amount dispute and unverified payment claim. The simulated model uses actual guarded reads; confirmation and accounting truth stay independent."
                                : scenario === "contact_stop_reply"
                                  ? "The server creates a fixed synthetic request to stop customer contact pending human review. Only this exact stored demo fixture is handled by the simulated model; sender remains unverified."
                                  : scenario === "case_reply"
                                    ? "The fixed synthetic source disputes the purchase order reference on the selected invoices. It does not establish a financial correction."
                                    : scenario === "case_resolution"
                                      ? "The fixed synthetic source confirms the PO reference. An authorized human must review documentary evidence; invoice balances remain source-controlled."
                                      : scenario ===
                                          "unverified_reference_reply"
                                        ? "The fixed unknown-sender source names selected invoices but remains unlinked. Open its source and review invoice links explicitly; no investigation starts automatically."
                                        : scenario === "unlinked_reply"
                                          ? "The server creates a fixed unknown-sender reply without invoice links. Human review is required."
                                          : "The server creates a fixed outbound echo. This scenario does not start an AI investigation."}
                </p>
                <button
                  type="button"
                  disabled={
                    busy ||
                    !currentReview ||
                    selection.length < 1 ||
                    selection.length > 3
                  }
                  onClick={() => void simulate()}
                >
                  {busy ? "Submitting…" : "Capture synthetic sample"}
                </button>
                {!busy && !currentReview && (
                  <p className="field-hint">Enable a current inbound activation first.</p>
                )}
                {!busy && currentReview && selection.length < 1 && (
                  <p className="field-hint">Select at least one invoice.</p>
                )}
                <p className="muted">
                  If confirmation fails, retry the same selection to reuse its
                  request identity. Changing the scenario, invoices, or reviewed
                  activation creates a new request.
                </p>
              </>
            )}
          </>
        ))}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
