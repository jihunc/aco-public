"use client";
import { useId, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  CaseView,
  DispositionReference,
  InvoiceView,
  TransitionCase,
  UpsertCase,
  WorkspaceView,
} from "@/lib/generated-api";
import {
  communicationsApi,
  type Communication,
} from "@/features/communications/api";
import { caseApi, exactCaseEvidence } from "./case-api";
import { DispositionReferencePicker } from "./disposition-reference";
import { stamp } from "@/features/receivables/shared";

function SourcePicker({
  workspace,
  invoiceId,
  customerId,
  scopeIds,
  onSelect,
}: {
  workspace: string;
  invoiceId?: string;
  customerId?: string;
  scopeIds?: string[];
  onSelect: (source: Communication | null) => void;
}) {
  const field = useId();
  const [selected, setSelected] = useState("");
  const messages = useQuery({
    queryKey: ["case-source-options", workspace, invoiceId, customerId],
    queryFn: ({ signal }) =>
      communicationsApi.list(
        workspace,
        {
          invoice_id: invoiceId,
          customer_id: customerId,
          direction: "incoming",
          page_size: 100,
        },
        signal,
      ),
    retry: false,
  });
  const available =
    messages.data?.items.filter(
      (row) =>
        row.source_state === "stored" &&
        row.content.text_body &&
        (!scopeIds ||
          row.invoice_links.some((link) => scopeIds.includes(link.invoice_id))),
    ) ?? [];
  return (
    <div>
      <label htmlFor={field}>Case source message</label>
      <select
        id={field}
        required
        value={selected}
        onChange={(event) => {
          setSelected(event.target.value);
          onSelect(
            available.find((row) => row.id === event.target.value) ?? null,
          );
        }}
      >
        <option value="">Choose stored incoming evidence</option>
        {available.map((row) => (
          <option value={row.id} key={row.id}>
            {row.content.subject || "(No subject)"} · {stamp(row.event_at)}
          </option>
        ))}
      </select>
      {messages.isPending ? (
        <p role="status">Loading case source messages…</p>
      ) : messages.isError ? (
        <>
          <p role="alert">Source messages unavailable.</p>
          <button type="button" onClick={() => void messages.refetch()}>
            Retry case source
          </button>
        </>
      ) : !available.length ? (
        <p>
          No stored incoming source is linked. Capture an authorized message
          first.
        </p>
      ) : null}
    </div>
  );
}
export function CaseCapturePanel({
  workspace,
  invoice,
  csrfToken,
}: {
  workspace: WorkspaceView;
  invoice: InvoiceView;
  csrfToken: string;
}) {
  const issueField = useId();
  const router = useRouter();
  const cache = useQueryClient();
  const [source, setSource] = useState<Communication | null>(null);
  const [excerpt, setExcerpt] = useState("");
  const [ids, setIds] = useState<string[]>([invoice.id]);
  const [issue, setIssue] = useState<UpsertCase["issue_type"]>("wrong_po");
  const [claim, setClaim] = useState("");
  const [substance, setSubstance] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!workspace.permissions.includes("task_write")) return null;
  return (
    <section
      className="panel promise-controls case-controls"
      aria-label="Record source-backed case"
    >
      <h2>Record a collection issue</h2>
      <p>
        Create an evidence-backed case for the exact affected invoices. Case
        resolution never changes accounting balances.
      </p>
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          if (!source || busy) return;
          setBusy(true);
          setError("");
          try {
            const result = await caseApi.create(
              workspace.id,
              {
                invoice,
                source,
                excerpt,
                invoiceIds: ids,
                issueType: issue,
                claimKey: claim,
                substance,
              },
              csrfToken,
            );
            await cache.invalidateQueries({
              queryKey: ["cases", workspace.id],
            });
            await cache.invalidateQueries({
              queryKey: ["case-outcomes", workspace.id],
            });
            router.push(
              `/cases/${encodeURIComponent(result.case.id)}?workspace=${encodeURIComponent(workspace.id)}`,
            );
          } catch (failure) {
            setError(
              failure instanceof Error
                ? failure.message
                : "Case result could not be confirmed. Refresh current records.",
            );
          } finally {
            setBusy(false);
          }
        }}
      >
        <SourcePicker
          workspace={workspace.id}
          invoiceId={invoice.id}
          onSelect={(item) => {
            setSource(item);
            setExcerpt(item?.content.text_body ?? "");
            setIds([invoice.id]);
          }}
        />
        <label>
          Exact case evidence excerpt
          <textarea
            required
            maxLength={8000}
            value={excerpt}
            onFocus={(event) => event.currentTarget.select()}
              onChange={(event) => setExcerpt(event.target.value)}
          />
            <span className="field-hint">Prefilled with the whole message. Select the exact sentence; typing replaces it.</span>
        </label>
        {source && (
          <fieldset>
            <legend>Affected invoices in this source</legend>
            {source.invoice_links.map((link) => (
              <label key={link.invoice_id}>
                <input
                  type="checkbox"
                  checked={ids.includes(link.invoice_id)}
                  disabled={link.invoice_id === invoice.id}
                  onChange={(event) =>
                    setIds((previous) =>
                      event.target.checked
                        ? [...previous, link.invoice_id]
                        : previous.filter((id) => id !== link.invoice_id),
                    )
                  }
                />
                Include{" "}
                {link.invoice_id === invoice.id
                  ? invoice.number
                  : `invoice ${link.invoice_id}`}
              </label>
            ))}
          </fieldset>
        )}
        <label htmlFor={issueField}>Collection issue type</label>
        <select
          id={issueField}
          value={issue}
          onChange={(event) =>
            setIssue(event.target.value as UpsertCase["issue_type"])
          }
        >
          <option value="wrong_po">Purchase order mismatch</option>
          <option value="wrong_amount">Amount dispute</option>
          <option value="payment_not_found">Payment not found</option>
          <option value="refund">Refund request</option>
          <option value="contact_correction">Contact correction</option>
          <option value="stop_contact">Stop contact</option>
          <option value="other">Other issue</option>
        </select>
        <label>
          Claim identifier
          <input
            required
            maxLength={240}
            value={claim}
            onChange={(event) => setClaim(event.target.value)}
            placeholder="For example PO-2026-11-review"
          />
        </label>
        <label>
          Issue description
          <textarea
            required
            maxLength={2000}
            value={substance}
            onChange={(event) => setSubstance(event.target.value)}
          />
        </label>
        {error && <p role="alert">{error}</p>}
        <button type="submit" disabled={busy || !source || !csrfToken}>
          {busy ? "Recording case…" : "Record collection case"}
        </button>
      </form>
    </section>
  );
}
const next: Record<CaseView["status"], CaseView["status"][]> = {
  OPEN: ["INVESTIGATING"],
  INVESTIGATING: [
    "WAITING_CUSTOMER",
    "WAITING_INTERNAL",
    "AWAITING_APPROVAL",
    "RESOLVED",
  ],
  WAITING_CUSTOMER: ["INVESTIGATING"],
  WAITING_INTERNAL: ["INVESTIGATING"],
  AWAITING_APPROVAL: ["INVESTIGATING"],
  RESOLVED: ["CLOSED"],
  CLOSED: [],
};
export function CaseControls({
  workspace,
  record,
  csrfToken,
  onSaved,
}: {
  workspace: WorkspaceView;
  record: CaseView;
  csrfToken: string;
  onSaved: () => Promise<unknown>;
}) {
  const field = useId();
  const cache = useQueryClient();
  const [status, setStatus] = useState<CaseView["status"]>(
    next[record.status][0] ?? record.status,
  );
  const [reason, setReason] = useState("");
  const [evidenceId, setEvidenceId] = useState("");
  const [invoiceId, setInvoiceId] = useState(
    record.invoice_links.find((link) => link.issue_state !== "RESOLVED")
      ?.invoice_id ?? "",
  );
  const [action, setAction] = useState("");
  const [reference, setReference] = useState("");
  const [captured, setCaptured] = useState<{
    invoiceId: string;
    caseVersion: number;
    reference: DispositionReference;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [source, setSource] = useState<Communication | null>(null);
  const [excerpt, setExcerpt] = useState("");
  const [closure, setClosure] = useState<
    NonNullable<TransitionCase["closure_reason"]>
  >(record.status === "RESOLVED" ? "resolved" : "withdrawn");
  const [survivor, setSurvivor] = useState("");
  const canWrite = workspace.permissions.includes("task_write");
  const canResolve = workspace.permissions.includes("resolve");
  if (!canWrite && !canResolve) return null;
  const savedEvidence = record.evidence.find((item) => item.id === evidenceId);
  const evidence: NonNullable<TransitionCase["evidence"]> | null =
    savedEvidence?.communication_id
      ? {
          communication_id: savedEvidence.communication_id,
          excerpt: savedEvidence.excerpt,
          offset: savedEvidence.offset,
        }
      : savedEvidence?.voice_source
        ? {
            kind: "voice_operational",
            voice_evidence_id: savedEvidence.voice_source.id,
            facts_hash: savedEvidence.voice_source.facts_hash,
          }
        : null;

  async function perform(work: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await work();
      await onSaved();
      await Promise.all(
        [
          ["case", workspace.id, record.id],
          ["cases", workspace.id],
          ["invoice", workspace.id],
          ["impact", workspace.id],
          ["case-outcomes", workspace.id],
        ].map((queryKey) => cache.invalidateQueries({ queryKey })),
      );
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : "Result unconfirmed. Refresh case history.",
      );
      await onSaved();
    } finally {
      setBusy(false);
    }
  }
  const choices = [
    ...next[record.status],
    ...(canResolve &&
    record.status !== "CLOSED" &&
    !next[record.status].includes("CLOSED")
      ? ["CLOSED" as const]
      : []),
  ].filter((item) => canResolve || !["RESOLVED", "CLOSED"].includes(item));
  const specialist = ["refund", "contact_correction", "stop_contact"].includes(
    record.issue_type,
  );
  const currentReference =
    captured?.invoiceId === invoiceId && captured.caseVersion === record.version
      ? captured.reference
      : null;
  const resolutionEvidence =
    evidence &&
    (!("voice_evidence_id" in evidence) ||
      specialist ||
      record.issue_type === "payment_not_found");
  const resolutionSupported = [
    "refund",
    "contact_correction",
    "stop_contact",
    "wrong_amount",
    "wrong_po",
    "payment_not_found",
  ].includes(record.issue_type);
  return (
    <section
      className="panel promise-controls case-controls"
      aria-label="Case handling controls"
    >
      <h2>Handle this case</h2>
      <p>
        Every change uses current version {record.version} and server-side
        authorization. Resolving an issue does not mark an invoice paid.
      </p>
      {error && <p role="alert">{error}</p>}
      <label htmlFor={`${field}-evidence`}> Recorded case evidence</label>
      <select
        id={`${field}-evidence`}
        value={evidenceId}
        onChange={(event) => setEvidenceId(event.target.value)}
      >
        <option value="">Choose reviewed source evidence</option>
        {record.evidence
          .filter((item) => item.communication_id !== null || item.voice_source)
          .map((item) => (
            <option key={item.id} value={item.id}>
              {item.voice_source
                ? "Signed call issue origin"
                : item.excerpt.slice(0, 100)}{" "}
              · {item.id.slice(0, 8)}
            </option>
          ))}
      </select>
      {savedEvidence && <p className="task-notes">{savedEvidence.excerpt}</p>}
      {record.evidence.some((item) => item.voice_source) && (
        <p>
          Call business facts identify the unverified issue. Specialist
          resolution requires an independent verified disposition; call facts
          alone do not prove a payment or source correction. Administrative
          closure does not claim successful resolution.
        </p>
      )}
      {canWrite && choices.length > 0 && (
        <form
          aria-label="Change case status"
          onSubmit={(event) => {
            event.preventDefault();
            void perform(() =>
              caseApi.transition(
                workspace.id,
                record,
                status,
                reason,
                csrfToken,
                status === "CLOSED"
                  ? {
                      closure_reason: closure,
                      surviving_case_id: survivor || null,
                      evidence,
                    }
                  : {},
              ),
            );
          }}
        >
          <label htmlFor={`${field}-status`}> Next case status</label>
          <select
            id={`${field}-status`}
            value={status}
            onChange={(event) =>
              setStatus(event.target.value as CaseView["status"])
            }
          >
            {choices.map((item) => (
              <option key={item} value={item}>
                {item.replaceAll("_", " ")}
              </option>
            ))}
          </select>
          {status === "CLOSED" && (
            <>
              <label htmlFor={`${field}-closure`}>
                {" "}
                Case closure disposition
              </label>
              <select
                id={`${field}-closure`}
                value={closure}
                onChange={(event) =>
                  setClosure(event.target.value as typeof closure)
                }
              >
                <option value="resolved">Resolved issues</option>
                <option value="withdrawn">Withdrawn</option>
                <option value="out_of_scope">Out of scope</option>
                <option value="invalid_claim">Invalid claim</option>
                <option value="duplicate_linked">
                  Duplicate linked to surviving case
                </option>
              </select>
            </>
          )}
          {closure === "duplicate_linked" && status === "CLOSED" && (
            <label>
              Surviving case reference
              <input
                required
                value={survivor}
                onChange={(event) => setSurvivor(event.target.value)}
              />
            </label>
          )}
          <label>
            Case status reason
            <textarea
              required
              maxLength={2000}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          <button disabled={busy || !csrfToken} type="submit">
            Save case status
          </button>
        </form>
      )}
      {canResolve &&
        record.status === "INVESTIGATING" &&
        (resolutionSupported ? (
          <form
            aria-label="Record invoice issue resolution"
            onSubmit={(event) => {
              event.preventDefault();
              if (
                !evidence ||
                !resolutionEvidence ||
                (specialist && !currentReference)
              )
                return;
              void perform(() =>
                caseApi.resolveInvoice(
                  workspace.id,
                  record,
                  invoiceId,
                  evidence,
                  currentReference?.resolution_kind ??
                    (record.issue_type === "payment_not_found"
                      ? "payment_allocation"
                      : "documentary_confirmation"),
                  action,
                  currentReference?.id ?? (reference || null),
                  csrfToken,
                  currentReference ?? undefined,
                ),
              );
            }}
          >
            <h3>Record a verified invoice disposition</h3>
            <label htmlFor={`${field}-invoice`}> Case invoice to resolve</label>
            <select
              id={`${field}-invoice`}
              required
              value={invoiceId}
              onChange={(event) => {
                setCaptured(null);
                setInvoiceId(event.target.value);
              }}
            >
              <option value="">Choose an unresolved invoice</option>
              {record.invoice_links
                .filter((link) => link.issue_state !== "RESOLVED")
                .map((link) => (
                  <option value={link.invoice_id} key={link.invoice_id}>
                    {link.invoice_id}
                  </option>
                ))}
            </select>
            {specialist && (
              <DispositionReferencePicker
                key={`${record.version}:${invoiceId}`}
                workspace={workspace.id}
                caseId={record.id}
                caseVersion={record.version}
                invoiceId={invoiceId}
                onSelect={(value) =>
                  setCaptured(
                    value
                      ? {
                          invoiceId,
                          caseVersion: record.version,
                          reference: value,
                        }
                      : null,
                  )
                }
              />
            )}
            {record.issue_type === "payment_not_found" && (
              <label>
                Verified accounting allocation reference
                <input
                  required
                  value={reference}
                  onChange={(event) => setReference(event.target.value)}
                />
              </label>
            )}
            <label>
              Documented resolution action
              <textarea
                required
                maxLength={2000}
                value={action}
                onChange={(event) => setAction(event.target.value)}
              />
            </label>
            <button
              type="submit"
              disabled={
                busy ||
                !resolutionEvidence ||
                !invoiceId ||
                !csrfToken ||
                (specialist && !currentReference)
              }
            >
              Resolve selected invoice issue
            </button>
            <p>
              Source evidence must support this disposition. Payment-not-found
              issues additionally require an actual unadjusted accounting
              allocation. Specialist dispositions require the exact reviewed
              current reference. Resolving a stop-contact issue retains its
              contact restrictions.
            </p>
          </form>
        ) : (
          <p>
            This issue requires specialist review; no automatic financial or
            contact disposition is available.
          </p>
        ))}
      {canWrite && record.invoice_links[0] && (
        <form
          aria-label="Attach new case evidence"
          onSubmit={(event) => {
            event.preventDefault();
            if (!source) return;
            void perform(() =>
              caseApi.linkEvidence(
                workspace.id,
                record,
                exactCaseEvidence(
                  source,
                  excerpt,
                  source.invoice_links
                    .filter((link) =>
                      record.invoice_links.some(
                        (item) => item.invoice_id === link.invoice_id,
                      ),
                    )
                    .map((link) => link.invoice_id),
                ),
                csrfToken,
              ),
            );
          }}
        >
          <h3>Attach incoming evidence</h3>
          <p>
            New evidence reopens only the affected invoice issues on a resolved
            or closed case.
          </p>
          <SourcePicker
            workspace={workspace.id}
            customerId={record.customer_id}
            scopeIds={record.invoice_links.map((link) => link.invoice_id)}
            onSelect={(item) => {
              setSource(item);
              setExcerpt(item?.content.text_body ?? "");
            }}
          />
          <label>
            New case evidence excerpt
            <textarea
              required
              maxLength={8000}
              value={excerpt}
              onFocus={(event) => event.currentTarget.select()}
              onChange={(event) => setExcerpt(event.target.value)}
            />
            <span className="field-hint">Prefilled with the whole message. Select the exact sentence; typing replaces it.</span>
          </label>
          <button disabled={busy || !source || !csrfToken} type="submit">
            Attach source evidence
          </button>
        </form>
      )}
    </section>
  );
}
