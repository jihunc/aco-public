import Link from "next/link";
import { InvoiceRef } from "@/lib/invoice-numbers";
import { stamp } from "@/features/receivables/shared";
import type { AgentRun, AgentStep } from "./api";
const humanWorkKinds = [
  "request_human_review",
  "human_review",
  "verify_accounting",
  "schedule_followup",
  "follow_up",
];
const taskKinds = [
  "create_task",
  "update_collection_task",
  "request_task_approval",
  ...humanWorkKinds,
];
const statusLabels = {
  queued: "Queued",
  running: "Running",
  awaiting_approval: "Awaiting approval",
  awaiting_action: "Awaiting guarded email execution",
  completed: "Completed",
  partially_completed: "Partially completed",
  failed: "Failed",
  cancelled: "Cancelled",
  started: "In progress",
};
export function AgentActivity({
  run,
  steps,
  workspace,
}: {
  run: AgentRun;
  steps: AgentStep[];
  workspace: string;
}) {
  const invoiceHref = (id: string) =>
    `/receivables/${encodeURIComponent(id)}?workspace=${encodeURIComponent(workspace)}`;
  const result = run.result;
  return (
    <div className="agent-activity">
      <section className="panel">
        <div className="section-heading">
          <h2>Investigation</h2>
          <span className={`badge agent-status-${run.status}`}>
            {statusLabels[run.status]}
          </span>
        </div>
        <p className="agent-objective">{run.objective}</p>
        <div className="agent-meta">
          <span className="badge">
            {run.authenticity === "FAKE"
              ? "Simulated model"
              : `Live model · ${run.model_id ?? "Identity unavailable"}`}
          </span>
          <span>Started {stamp(run.created_at)}</span>
          <span>Updated {stamp(run.updated_at)}</span>
        </div>
        <p className="muted">
          {run.authenticity === "FAKE"
            ? "Deterministic model simulation using the selected business records."
            : "Model output is an interpretation. Accounting source records remain financial truth."}
        </p>
        <div className="communication-links">
          {run.invoice_ids.map((id) => (
            <Link key={id} href={invoiceHref(id)}>
              <InvoiceRef id={id} />
            </Link>
          ))}
        </div>
        {run.error_code && (
          <p role="alert">
            Investigation stopped: {run.error_code}. Inspect the persisted steps
            below.
          </p>
        )}
      </section>
      <section className="panel" aria-label="Investigation results">
        <h2>Findings &amp; outcomes</h2>
        {result ? (
          <>
            <p>{result.summary}</p>
            {result.invoice_findings.map((finding) => (
              <article className="agent-finding" key={finding.invoice_id}>
                <h3>
                  <Link href={invoiceHref(finding.invoice_id)}>
                    <InvoiceRef id={finding.invoice_id} />
                  </Link>
                </h3>
                <p>{finding.assessment}</p>
                <div
                  className="communication-links"
                  aria-label="Finding evidence"
                >
                  {finding.evidence_step_ids.map((id, index) =>
                    steps.some((step) => step.id === id) ? (
                      <a key={id} href={`#agent-step-${id}`}>
                        Evidence {index + 1}
                      </a>
                    ) : (
                      <span key={id}>Evidence {index + 1} · not loaded</span>
                    ),
                  )}
                </div>
              </article>
            ))}
            <h3>Actions &amp; proposals</h3>
            {result.proposals.length ? (
              <ul className="agent-proposals">
                {result.proposals.map((proposal, index) => (
                  <li key={`${proposal.invoice_id}-${index}`}>
                    <span className="badge">
                      {proposal.kind === "request_voice_call" &&
                      proposal.executed
                        ? "Voice review requested · call not started"
                        : proposal.executed
                          ? "Executed · committed"
                          : proposal.approval_status === "pending"
                            ? "Awaiting independent approval"
                            : proposal.approval_status === "denied"
                              ? "Denied · not executed"
                              : proposal.approval_status === "stale"
                                ? "Approval invalid · not executed"
                                : "Proposed · not executed"}
                    </span>
                    {proposal.approval_id &&
                      proposal.kind !== "request_voice_call" && (
                        <p className="muted">
                          Original AI recommendation. The approved proposal
                          history and committed record contain the executed
                          text, including any human modification.
                        </p>
                      )}
                    <p>
                      <strong>
                        {proposal.title ??
                          (proposal.kind === "request_voice_call"
                            ? "Request a reviewed browser conversation"
                            : proposal.kind === "request_collection_hold"
                              ? "Pause contact pending review"
                              : proposal.kind.replaceAll("_", " "))}
                      </strong>{" "}
                      ·{" "}
                      <Link href={invoiceHref(proposal.invoice_id)}>
                        <InvoiceRef id={proposal.invoice_id} />
                      </Link>
                    </p>
                    <p>{proposal.reason}</p>
                    {proposal.kind === "request_voice_call" &&
                      proposal.voice_session_id && (
                        <div>
                          <p>
                            Independent review and an explicit human start are
                            still required. Capture remains disabled. No phone
                            call has been placed.
                          </p>
                          <Link
                            href={`/voice/${encodeURIComponent(proposal.voice_session_id)}?workspace=${encodeURIComponent(workspace)}`}
                          >
                            Open saved voice session and current eligibility
                          </Link>
                        </div>
                      )}
                    {proposal.email_command && (
                      <section aria-label="Original email draft">
                        <p>
                          <strong>{proposal.email_command.subject}</strong>
                        </p>
                        <p className="task-notes">
                          {proposal.email_command.content}
                        </p>
                        <p className="muted">
                          Original saved draft. Independent review and current
                          worker checks govern sending; provider acceptance is
                          not delivery. Approved replacement text is in the
                          linked history and communication.
                        </p>
                      </section>
                    )}
                    {proposal.kind === "request_collection_hold" &&
                      proposal.executed && (
                        <div className="space-y-2">
                          <p>
                            Customer contact holds were saved pending human
                            review. Check current invoice restrictions for their
                            disposition. Sender authority remains unverified.
                            Existing disputes and promises remain independent.
                          </p>
                          <p>
                            <Link href={invoiceHref(proposal.invoice_id)}>
                              View invoice holds
                            </Link>
                            {" · "}
                            <Link
                              href={`/review?workspace=${encodeURIComponent(workspace)}`}
                            >
                              Open contact review queue
                            </Link>
                          </p>
                        </div>
                      )}
                    {proposal.approval_id && (
                      <Link
                        href={`/review?workspace=${encodeURIComponent(workspace)}&approval=${encodeURIComponent(proposal.approval_id)}`}
                      >
                        Open approval and decision history
                      </Link>
                    )}
                    {proposal.executed && (
                      <div className="agent-outcome">
                        {proposal.kind === "update_collection_task" && (
                          <p>
                            Task fields updated. Task completion does not
                            resolve a case or verify payment. Assignment,
                            urgency and deadlines are unchanged.
                          </p>
                        )}
                        {humanWorkKinds.includes(proposal.kind) && (
                          <p>
                            {["schedule_followup", "follow_up"].includes(
                              proposal.kind,
                            )
                              ? "Scheduled human follow-up task. Open the task for its due time and assignee."
                              : "Human review task assigned to the initiating team member."}
                          </p>
                        )}
                        <p>
                          Committed action:{" "}
                          {proposal.action_id ?? "Reference unavailable"}
                        </p>
                        {proposal.changed_ids?.length ? (
                          <>
                            <p>
                              {proposal.kind === "request_collection_hold"
                                ? "Saved hold references:"
                                : "Changed records:"}
                            </p>
                            <ul>
                              {proposal.changed_ids.map((id) => (
                                <li key={id}>
                                  {[
                                    ...taskKinds,
                                    "upsert_case",
                                    "record_promise",
                                    "request_email_send",
                                    "link_communication",
                                  ].includes(proposal.kind) ? (
                                    <Link
                                      href={`/${proposal.kind === "upsert_case" ? "cases" : proposal.kind === "record_promise" ? "promises" : ["request_email_send", "link_communication"].includes(proposal.kind) ? "communications" : "tasks"}/${encodeURIComponent(id)}?workspace=${encodeURIComponent(workspace)}`}
                                    >
                                      Open{" "}
                                      {proposal.kind === "upsert_case"
                                        ? "case"
                                        : proposal.kind === "record_promise"
                                          ? "promise"
                                          : [
                                                "request_email_send",
                                                "link_communication",
                                              ].includes(proposal.kind)
                                            ? "communication"
                                            : "task"}{" "}
                                      {id}
                                    </Link>
                                  ) : (
                                    id
                                  )}
                                </li>
                              ))}
                            </ul>
                            {taskKinds.includes(proposal.kind) && (
                              <Link
                                href={`/review?workspace=${encodeURIComponent(workspace)}`}
                              >
                                Open tasks in Review Queue
                              </Link>
                            )}
                          </>
                        ) : (
                          <p>
                            Record references unavailable. Inspect the recorded
                            action step.
                          </p>
                        )}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <p>No actions were proposed.</p>
            )}
            {!!result.limitations.length && (
              <>
                <h3>Limits of this investigation</h3>
                <ul>
                  {result.limitations.map((limit, index) => (
                    <li key={index}>{limit}</li>
                  ))}
                </ul>
              </>
            )}
          </>
        ) : (
          <p>
            No result has been committed.{" "}
            {run.status === "queued" || run.status === "running"
              ? "Results will appear after the worker saves them."
              : "Review the recorded status and steps."}
          </p>
        )}
        {run.execution === "UNAVAILABLE" && (
          <p className="agent-notice">
            Action execution is unavailable for this investigation. Proposals do
            not create cases, tasks or promises and do not send messages.
          </p>
        )}
      </section>
      <section className="panel" aria-label="Observable activity">
        <h2>Observable activity</h2>
        <p className="muted">
          Saved tool operations and outcomes. Source snapshots are last-known
          observations.
        </p>
        {steps.length === 0 ? (
          <p>No steps have been committed yet.</p>
        ) : (
          <ol className="agent-steps">
            {steps.map((step) => (
              <li id={`agent-step-${step.id}`} key={step.id} tabIndex={-1}>
                <div className="section-heading">
                  <h3>
                    {step.step_sequence}.{" "}
                    {step.tool_name ?? "Model investigation"}
                  </h3>
                  <span className={`badge agent-status-${step.status}`}>
                    {statusLabels[step.status]}
                  </span>
                </div>
                <p>{step.description}</p>
                <p className="muted">
                  {stamp(step.updated_at)}
                  {step.invoice_id && (
                    <>
                      {" "}
                      ·{" "}
                      <Link href={invoiceHref(step.invoice_id)}>
                        View invoice
                      </Link>
                    </>
                  )}
                </p>
                {step.error_code && <p>Tool error: {step.error_code}</p>}
                <details>
                  <summary>Inspect saved input and output</summary>
                  <h4>Validated input</h4>
                  <pre>{JSON.stringify(step.input, null, 2)}</pre>
                  <h4>Recorded output</h4>
                  {step.output ? (
                    <pre>{JSON.stringify(step.output, null, 2)}</pre>
                  ) : (
                    <p>No output was committed.</p>
                  )}
                </details>
                {!!step.usage_ids.length && (
                  <details>
                    <summary>Usage references</summary>
                    <ul>
                      {step.usage_ids.map((id) => (
                        <li key={id}>{id}</li>
                      ))}
                    </ul>
                  </details>
                )}
              </li>
            ))}
          </ol>
        )}
      </section>
      <section className="panel">
        <h2>Recorded usage</h2>
        <dl className="detail-grid">
          <div>
            <dt>Model requests</dt>
            <dd>{run.requests}</dd>
          </div>
          <div>
            <dt>Input tokens</dt>
            <dd>{run.input_tokens ?? "Unknown"}</dd>
          </div>
          <div>
            <dt>Output tokens</dt>
            <dd>{run.output_tokens ?? "Unknown"}</dd>
          </div>
          <div>
            <dt>Provider attempt</dt>
            <dd>
              {run.provider_attempted
                ? "Recorded"
                : "No provider attempt recorded"}
            </dd>
          </div>
        </dl>
        <p className="muted">
          Costs are available in the workspace usage ledger when measured;
          missing costs are unknown.
        </p>
        <Link href={`/overview?workspace=${encodeURIComponent(workspace)}`}>
          Open workspace usage
        </Link>
        {!!run.usage_ids.length && (
          <details>
            <summary>Run usage references</summary>
            <ul>
              {run.usage_ids.map((id) => (
                <li key={id}>{id}</li>
              ))}
            </ul>
          </details>
        )}
      </section>
    </div>
  );
}
