"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ErrorState, stamp } from "@/features/receivables/shared";
import { formatMoney } from "@/lib/money";
import type { WorkspaceView } from "@/lib/generated-api";
import {
  approvalApi,
  captureApproval,
  decisionCommand,
  modificationCommand,
  sameApprovalSnapshot,
  type Approval,
} from "./approval-api";

export function approvalRestriction(reason: string): string {
  if (/SELF|INDEPENDENT|SAME_ACTOR/i.test(reason))
    return "Another reviewer must approve this proposal. You may still deny it if allowed.";
  if (/STALE|MATERIAL|BINDING|CHANGED/i.test(reason))
    return "The proposal or its supporting evidence changed. A fresh review is required.";
  if (/EXPIRED/i.test(reason)) return "The approval has expired.";
  if (/PERMISSION|ROLE|MEMBERSHIP/i.test(reason))
    return "Your current role cannot decide this proposal.";
  if (/CONSUMED/i.test(reason)) return "This approval has already been used.";
  if (reason === "COLLECTION_HELD")
    return "Collections are on hold for this invoice or customer.";
  if (reason === "RECIPIENT_RESTRICTED")
    return "The recipient has contact restrictions.";
  if (reason === "CAPTURE_APPROVAL_UNAVAILABLE")
    return "Recording or transcript capture cannot be approved here.";
  if (reason === "INVOICE_NOT_OUTSTANDING")
    return "This invoice has no outstanding balance.";
  if (reason === "ACTION_NOT_REVIEWABLE")
    return "This action is no longer open for review.";
  return reason.replaceAll("_", " ").toLowerCase();
}
export function ApprovalRecord({
  record,
  workspace,
  canReview,
  onReview,
  onModify,
}: {
  record: Approval;
  workspace: string;
  canReview: boolean;
  onReview: () => void;
  onModify?: () => void;
}) {
  const proposal = record.proposal;
  const internal = "effect" in proposal && proposal.effect === "internal_task";
  const voice = "effect" in proposal && proposal.effect === "voice_session";
  const agentEmail = "effect" in proposal && proposal.effect === "agent_email";
  const outbound = "channel" in proposal;
  const outcome = {
    pending_approval: "Awaiting independent approval",
    queued: "Approved · awaiting guarded execution",
    executed: "Internal task created",
    denied: "Denied · no task created",
    stale: "Approval no longer valid · no task created",
    failed: "Execution stopped · inspect the run",
  };
  return (
    <article
      className="panel"
      id={`approval-${record.id}`}
      data-testid="approval-record"
      aria-label={`Approval ${record.id}`}
    >
      <div className="section-heading">
        <div>
          <h3>
            {internal
              ? "Internal task proposal"
              : voice
                ? "Offline voice session proposal"
                : outbound
                  ? proposal.channel === "email"
                    ? "Email proposal"
                    : "Call proposal"
                  : "Proposal kind unavailable"}
          </h3>
          <p>
            <span className="badge">{record.effective_status}</span>
            {internal
              ? ` · ${outcome[record.execution_status]}`
              : voice
                ? ` · ${proposal.evidence_mode} · Live voice BLOCKED`
                : outbound
                  ? ` · To ${proposal.destination}`
                  : " · Proposal kind unavailable"}
          </p>
        </div>
        {canReview && record.can_modify && onModify && (
          <button type="button" onClick={onModify}>
            Modify proposal
          </button>
        )}
        {canReview && (record.can_approve || record.can_deny) && (
          <button type="button" onClick={onReview}>
            Review proposal
          </button>
        )}
      </div>
      {(record.supersedes_id || record.replacement_id) && (
        <section aria-label="Proposal modification history">
          {record.supersedes_id && (
            <p>
              <Link
                href={`/review?workspace=${encodeURIComponent(workspace)}&approval=${encodeURIComponent(record.supersedes_id)}`}
              >
                Original proposal and decisions
              </Link>
              {" · "}Material editor {record.editor_id}. {record.edit_reason}
            </p>
          )}
          {record.replacement_id && (
            <p>
              This proposal was replaced and cannot authorize execution.{" "}
              <Link
                href={`/review?workspace=${encodeURIComponent(workspace)}&approval=${encodeURIComponent(record.replacement_id)}`}
              >
                Open replacement proposal
              </Link>
            </p>
          )}
        </section>
      )}
      {internal && <h4>{proposal.title}</h4>}
      {internal && proposal.case_binding && (
        <p>
          <Link
            href={`/cases/${encodeURIComponent(proposal.case_binding.case_id)}?workspace=${encodeURIComponent(workspace)}`}
          >
            Open bound case
          </Link>
          {" · "}Reviewed case version {proposal.case_binding.case_version}.
          This task proposal does not resolve the case.
        </p>
      )}
      {agentEmail && (
        <section aria-label="Reviewed email dispatch">
          <p>
            <strong>{proposal.subject}</strong>
          </p>
          <p>
            {record.dispatch_status === "ACCEPTED"
              ? "Provider accepted · delivery not confirmed"
              : record.dispatch_status === "UNKNOWN"
                ? "Outcome unknown · reconciliation required; no automatic resend"
                : record.dispatch_status === "QUEUED"
                  ? "Awaiting guarded email execution"
                  : record.dispatch_status === "STOPPED"
                    ? "Dispatch stopped · inspect the investigation"
                    : "Awaiting independent review"}
          </p>
          {proposal.cost_basis === "SHARED_GRANT_AUTHORIZATION_CEILING" &&
          proposal.live_cost_scope ? (
            <section
              aria-label="Reviewed email authorization ceiling"
              className="muted"
            >
              <p>
                Shared-grant authorization ceiling: {proposal.currency}{" "}
                {proposal.cost_bound}. This is not a predicted per-email fee or
                actual billing. The grant is shared by sends and optional
                provider reads; current remaining allowance is checked before
                every provider request.
              </p>
              <p>
                {proposal.live_cost_scope.max_requests} provider requests ·{" "}
                {proposal.live_cost_scope.max_messages} outbound message(s) ·
                USD {proposal.live_cost_scope.request_usd_ceiling} per-request
                ceiling.
              </p>
              <p>
                Reviewed grant {proposal.live_cost_scope.grant_id}, version{" "}
                {proposal.live_cost_scope.grant_version}. Ceiling reference:{" "}
                {proposal.live_cost_scope.price_source_ref}
                {" · "}
                {stamp(proposal.live_cost_scope.price_source_at)}. Actual
                provider billing remains separately recorded and may be unknown.
              </p>
            </section>
          ) : (
            <p className="muted">
              {proposal.cost_basis}: this offline scope permits no paid provider
              call. Actual provider usage and unknown billing remain separately
              recorded.
            </p>
          )}
        </section>
      )}
      <p className="task-notes">
        {internal
          ? proposal.notes
          : voice
            ? proposal.objective
            : outbound
              ? proposal.content
              : "Proposal content unavailable"}
      </p>
      {voice && (
        <section aria-label="Voice session restrictions">
          <p>
            Capture: {proposal.capture} · Participant: {proposal.participant}
          </p>
          {proposal.capture === "SYNTHETIC_ARTIFACTS" && (
            <>
              <p>
                FAKE fixed transcript and silent audio only; real provider
                capture remains disabled.
              </p>
              <p>
                Consent {proposal.consent_id} · Hash {proposal.consent_hash}
              </p>
              <ul>
                {Object.entries(proposal.capture_scopes ?? {}).map(
                  ([key, allowed]) => (
                    <li key={key}>
                      {key.replaceAll("_", " ")}:{" "}
                      {allowed ? "Permitted" : "Not permitted"}
                    </li>
                  ),
                )}
              </ul>
            </>
          )}
          <p>
            Permitted tools:{" "}
            {(proposal.allowed_tools ?? []).join(", ") || "Unavailable"}
          </p>
          <Link
            href={`/voice/${encodeURIComponent(proposal.session_id)}?workspace=${encodeURIComponent(workspace)}`}
          >
            Open saved voice session
          </Link>
        </section>
      )}
      <dl className="detail-grid">
        {outbound && (
          <>
            <div>
              <dt>
                {agentEmail &&
                proposal.cost_basis === "SHARED_GRANT_AUTHORIZATION_CEILING"
                  ? "Shared authorization ceiling"
                  : "Maximum proposed cost"}
              </dt>
              <dd>
                {proposal.currency} {proposal.cost_bound}
              </dd>
            </div>
            <div>
              <dt>Capture consent</dt>
              <dd>
                {proposal.consent_mode === "no_capture"
                  ? "No recording or transcript capture"
                  : "Approved capture"}
              </dd>
            </div>
          </>
        )}
        <div>
          <dt>Approval expires</dt>
          <dd>
            {record.expires_at
              ? stamp(record.expires_at)
              : "No approval granted"}
          </dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd>{stamp(record.created_at)}</dd>
        </div>
      </dl>
      <div className="communication-links">
        {proposal.invoice_ids.map((id) => (
          <Link
            key={id}
            href={`/receivables/${id}?workspace=${encodeURIComponent(workspace)}`}
          >
            Invoice {id}
          </Link>
        ))}
      </div>
      <section aria-label="Invoice scope">
        <h4>{record.invoice_scope.number}</h4>
        <p>
          {record.invoice_scope.synthetic
            ? "Synthetic accounting data"
            : "Accounting source"}{" "}
          ·{" "}
          {record.invoice_scope.verified
            ? "Verified source"
            : "Last-known snapshot"}
        </p>
        <p>
          Outstanding: {formatMoney(record.invoice_scope.outstanding)} ·
          Financial status: {record.invoice_scope.financial_status}
        </p>
        <p className="muted">
          Observed {stamp(record.invoice_scope.observed_at)} · Revision{" "}
          {record.invoice_scope.source_revision}
        </p>
      </section>
      {record.evidence.map((source) => (
        <details key={source.id}>
          <summary>
            Inspect accounting source · {source.origin} · Revision{" "}
            {source.source_revision}
          </summary>
          <pre className="communication-body">{source.source_json}</pre>
        </details>
      ))}
      {internal || agentEmail ? (
        <>
          {(record.agent_evidence ?? []).map((source) => (
            <details key={source.step_id}>
              <summary>
                Inspect investigation evidence · {source.tool_name}
              </summary>
              <pre className="communication-body">{source.source_json}</pre>
            </details>
          ))}
          <div className="communication-links">
            {record.source_run_id && (
              <Link
                href={`/agent-runs/${encodeURIComponent(record.source_run_id)}?workspace=${encodeURIComponent(workspace)}`}
              >
                Open investigation
              </Link>
            )}
            {record.execution_status === "executed" &&
              record.changed_ids.map((id) => (
                <Link
                  key={id}
                  href={`/${agentEmail ? "communications" : "tasks"}/${encodeURIComponent(id)}?workspace=${encodeURIComponent(workspace)}`}
                >
                  {agentEmail
                    ? "Open accepted communication"
                    : "Open created task"}
                </Link>
              ))}
          </div>
        </>
      ) : outbound ? (
        <p className="task-notes">
          Accounting source references: {proposal.evidence_ids.join(", ")}
        </p>
      ) : null}
      {record.reasons.length > 0 && record.effective_status !== "consumed" && (
        <ul>
          {record.reasons.map((reason) => (
            <li key={reason}>{approvalRestriction(reason)}</li>
          ))}
        </ul>
      )}
      {!canReview && (
        <p className="muted">
          An authorized reviewer must decide this proposal.
        </p>
      )}
      <p className="muted">
        {internal
          ? "This approval permits only the displayed internal task. It sends no message and places no call."
          : voice
            ? "Approval alone does not start a session. Live voice remains blocked; the saved offline session requires current server checks."
            : agentEmail
              ? "Independent approval queues this exact email for current source, policy, recipient, capture and single-use worker checks. Provider acceptance is not delivery."
              : "Sending is not enabled. A saved decision does not send a message or place a call."}
      </p>
      <details>
        <summary>Review history and reference</summary>
        <p className="task-notes">
          Approval {record.id} · Action {record.action_id}
          {outbound && <> · Policy {proposal.policy_version_id}</>}
        </p>
        {record.decisions.length === 0 ? (
          <p>No decisions recorded.</p>
        ) : (
          <ol>
            {record.decisions.map((item) => (
              <li key={item.id}>
                <strong>{item.decision}</strong> · {stamp(item.created_at)} ·
                Reviewer {item.approver_id}
                <p className="task-notes">{item.reason}</p>
              </li>
            ))}
          </ol>
        )}
      </details>
    </article>
  );
}
export function ApprovalsPanel({
  workspace,
  csrfToken,
  approvalId,
}: {
  workspace: WorkspaceView;
  csrfToken: string;
  approvalId?: string;
}) {
  const cache = useQueryClient();
  const key = ["approvals", workspace.id];
  const query = useQuery({
    queryKey: [...key, approvalId ?? "all"],
    queryFn: async ({ signal }) =>
      approvalId
        ? { items: [await approvalApi.get(workspace.id, approvalId, signal)] }
        : approvalApi.list(workspace.id, signal),
    retry: false,
    refetchInterval: (state) =>
      state.state.data?.items.some((item) =>
        ["pending", "approved"].includes(item.effective_status),
      )
        ? 2000
        : false,
  });
  const executed =
    query.data?.items
      .filter((item) => item.execution_status === "executed")
      .map((item) => item.id)
      .join(",") ?? "";
  useEffect(() => {
    if (!executed) return;
    for (const family of ["tasks", "review-queue", "agent-run", "agent-runs"])
      void cache.invalidateQueries({ queryKey: [family, workspace.id] });
  }, [executed, cache, workspace.id]);
  const [editSnapshot, setEditSnapshot] = useState<Approval | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editText, setEditText] = useState("");
  const [editReason, setEditReason] = useState("");
  const [replacementId, setReplacementId] = useState("");
  const [snapshot, setSnapshot] = useState<Approval | null>(null);
  const [reason, setReason] = useState("");
  const [hours, setHours] = useState(24);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const canReview = workspace.permissions.includes("approve_external");
  const current = query.data?.items.find((item) => item.id === snapshot?.id);
  const changed =
    !!snapshot && (!current || !sameApprovalSnapshot(snapshot, current));
  const editCurrent = query.data?.items.find(
    (item) => item.id === editSnapshot?.id,
  );
  const editChanged =
    !!editSnapshot &&
    (!editCurrent || !sameApprovalSnapshot(editSnapshot, editCurrent));
  const editingTask = editSnapshot?.effect === "internal_task";
  function modify(record: Approval) {
    if (!record.can_modify) return;
    const proposal = record.proposal;
    if (!("title" in proposal) && !("content" in proposal)) return;
    setSnapshot(null);
    setEditSnapshot(captureApproval(record));
    setEditTitle("title" in proposal ? proposal.title : "");
    setEditText(
      "notes" in proposal
        ? proposal.notes
        : "content" in proposal
          ? proposal.content
          : "",
    );
    setEditReason("");
    setError("");
    setNotice("");
    setReplacementId("");
  }
  async function saveModification() {
    if (
      !editSnapshot ||
      busy ||
      query.isError ||
      query.isFetching ||
      editChanged ||
      !canReview ||
      !editCurrent?.can_modify ||
      !editReason.trim() ||
      (editingTask ? !editTitle.trim() : !editText.trim())
    )
      return;
    setBusy(true);
    setError("");
    try {
      const saved = await approvalApi.modify(
        workspace.id,
        editSnapshot.id,
        modificationCommand(
          editSnapshot,
          editingTask
            ? { kind: "internal_task", title: editTitle, notes: editText }
            : { kind: "outbound_content", content: editText },
          editReason,
        ),
        csrfToken,
      );
      setEditSnapshot(null);
      setReplacementId(saved.id);
      setNotice(
        "Modified proposal saved. A fresh independent approval is required; the old proposal cannot execute.",
      );
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : "The modification could not be confirmed. Refresh its history.",
      );
    } finally {
      await cache.invalidateQueries({ queryKey: key });
      setBusy(false);
    }
  }
  function review(record: Approval) {
    setEditSnapshot(null);
    setSnapshot(captureApproval(record));
    setReason("");
    setError("");
    setNotice("");
    setHours(24);
  }
  async function decide(decision: "approved" | "denied") {
    if (
      !snapshot ||
      busy ||
      query.isError ||
      query.isFetching ||
      changed ||
      !canReview ||
      !(decision === "approved" ? current?.can_approve : current?.can_deny) ||
      !reason.trim() ||
      hours < 1 ||
      hours > 24 ||
      !Number.isInteger(hours)
    )
      return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await approvalApi.decide(
        workspace.id,
        snapshot.id,
        decisionCommand(snapshot, decision, reason, hours * 3600),
        csrfToken,
      );
      setSnapshot(null);
      setReason("");
      setNotice(
        decision === "approved"
          ? snapshot.effect === "internal_task"
            ? "Approval recorded. The investigation will revalidate it before creating the internal task."
            : snapshot.effect === "voice_session"
              ? "Approval recorded. The saved offline session still requires current server checks before starting."
              : "effect" in snapshot.proposal &&
                  snapshot.proposal.effect === "agent_email"
                ? "Approval recorded. The canonical worker will recheck this exact email before any submission."
                : "Approval recorded. Sending remains disabled."
          : "Denial recorded.",
      );
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : "The decision could not be saved.",
      );
    } finally {
      await Promise.all([
        cache.invalidateQueries({ queryKey: key }),
        cache.invalidateQueries({ queryKey: ["agent-run", workspace.id] }),
        cache.invalidateQueries({ queryKey: ["agent-runs", workspace.id] }),
      ]);
      setBusy(false);
    }
  }
  return (
    <section aria-label="Human approvals">
      <div className="panel section-heading">
        <div>
          <h2>Human approvals</h2>
          <p>
            Review the exact proposed effect and source evidence before
            deciding.
          </p>
        </div>
        <button
          type="button"
          disabled={query.isFetching || busy}
          onClick={() => void query.refetch()}
        >
          Refresh approvals
        </button>
        {approvalId && (
          <Link href={`/review?workspace=${encodeURIComponent(workspace.id)}`}>
            All approvals and tasks
          </Link>
        )}
      </div>
      {query.isPending && <p role="status">Loading approvals…</p>}
      {query.isError && (
        <ErrorState
          label="Approvals unavailable"
          error={query.error}
          retry={() => query.refetch()}
        />
      )}
      {notice && <p role="status">{notice}</p>}
      {replacementId && (
        <Link
          href={`/review?workspace=${encodeURIComponent(workspace.id)}&approval=${encodeURIComponent(replacementId)}`}
        >
          Review saved replacement
        </Link>
      )}
      {editSnapshot && !query.isError && (
        <section
          className="panel task-form-panel"
          aria-label="Modify captured proposal"
        >
          <h3>Modify captured proposal</h3>
          <p>
            Only proposal text changes. Source evidence, invoice scope,
            destination, cost limits and capture permissions stay as reviewed.
            Every material editor is excluded from approving the replacement.
          </p>
          <ApprovalRecord
            record={editSnapshot}
            workspace={workspace.id}
            canReview={false}
            onReview={() => {}}
          />
          {editChanged && (
            <p role="alert">
              The proposal changed. Your text is preserved here; close this
              editor and capture the current proposal before saving.
            </p>
          )}
          <form
            className="task-form"
            onSubmit={(event) => {
              event.preventDefault();
              void saveModification();
            }}
          >
            {editingTask && (
              <label>
                Revised task title
                <input
                  value={editTitle}
                  required
                  maxLength={240}
                  disabled={busy}
                  onChange={(event) => setEditTitle(event.target.value)}
                />
              </label>
            )}
            <label className="task-form-notes">
              {editingTask ? "Revised task notes" : "Revised message content"}
              <textarea
                value={editText}
                required={!editingTask}
                maxLength={editingTask ? 4000 : 20000}
                disabled={busy}
                onChange={(event) => setEditText(event.target.value)}
              />
            </label>
            <label className="task-form-notes">
              Modification reason
              <textarea
                value={editReason}
                required
                maxLength={2000}
                disabled={busy}
                onChange={(event) => setEditReason(event.target.value)}
              />
            </label>
            <div className="task-form-actions">
              <button
                type="submit"
                disabled={
                  busy ||
                  query.isFetching ||
                  editChanged ||
                  !canReview ||
                  !editCurrent?.can_modify ||
                  !editReason.trim() ||
                  (editingTask ? !editTitle.trim() : !editText.trim())
                }
              >
                Save replacement for independent approval
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setEditSnapshot(null);
                  setError("");
                }}
              >
                Cancel modification
              </button>
            </div>
          </form>
        </section>
      )}
      {error && <p role="alert">{error}</p>}
      {snapshot && !query.isError && (
        <section
          className="panel task-form-panel"
          aria-label="Review captured proposal"
        >
          <h3>Review captured proposal</h3>
          <p>This decision applies to the proposal displayed below.</p>
          <ApprovalRecord
            record={snapshot}
            workspace={workspace.id}
            canReview={false}
            onReview={() => {}}
          />
          {changed && (
            <p role="alert">
              This proposal changed or is no longer available. Your captured
              review has been kept. Close it and review the current proposal
              before deciding.
            </p>
          )}
          <form
            className="task-form"
            onSubmit={(event) => event.preventDefault()}
          >
            <label className="task-form-notes">
              Decision reason
              <textarea
                value={reason}
                maxLength={2000}
                required
                disabled={busy}
                onChange={(e) => setReason(e.target.value)}
              />
            </label>
            <label>
              Approval valid for (hours)
              <input
                type="number"
                min={1}
                max={24}
                step={1}
                value={hours}
                disabled={busy}
                onChange={(e) => setHours(Number(e.target.value))}
              />
            </label>
            <div className="task-form-actions">
              {snapshot.can_approve && (
                <button
                  type="button"
                  disabled={
                    busy ||
                    query.isError ||
                    query.isFetching ||
                    changed ||
                    !canReview ||
                    !current?.can_approve ||
                    !reason.trim() ||
                    hours < 1 ||
                    hours > 24 ||
                    !Number.isInteger(hours)
                  }
                  onClick={() => void decide("approved")}
                >
                  Approve proposal
                </button>
              )}
              {snapshot.can_deny && (
                <button
                  type="button"
                  disabled={
                    busy ||
                    query.isError ||
                    query.isFetching ||
                    changed ||
                    !canReview ||
                    !current?.can_deny ||
                    !reason.trim() ||
                    hours < 1 ||
                    hours > 24 ||
                    !Number.isInteger(hours)
                  }
                  onClick={() => void decide("denied")}
                >
                  Deny proposal
                </button>
              )}
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setSnapshot(null);
                  setError("");
                }}
              >
                Close review
              </button>
            </div>
          </form>
        </section>
      )}
      {!query.isError && query.data && query.data.items.length === 0 && (
        <section className="panel">
          <p>No approval requests in this workspace.</p>
        </section>
      )}
      {!query.isError &&
        query.data?.items.map((record) => (
          <ApprovalRecord
            key={record.id}
            record={record}
            workspace={workspace.id}
            canReview={canReview && !busy}
            onReview={() => review(record)}
            onModify={() => modify(record)}
          />
        ))}
    </section>
  );
}
