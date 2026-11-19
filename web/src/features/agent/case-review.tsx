"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import type { CaseView, WorkspaceView } from "@/lib/generated-api";
import { agentApi, type StartInvestigation } from "./api";

export function caseReviewCommand(
  record: CaseView,
  requestId: string,
): StartInvestigation {
  const invoiceIds = [
    ...new Set(record.invoice_links.map((link) => link.invoice_id)),
  ];
  if (!invoiceIds.length || invoiceIds.length > 3)
    throw new Error(
      "A case review requires its complete one-to-three invoice scope.",
    );
  return {
    request_id: requestId,
    invoice_ids: invoiceIds,
    objective: `Request approval before creating an internal task for case ${record.id} version ${record.version}. Review the exact case and all selected invoice evidence. Do not resolve the case or change accounting.`,
  };
}

export function CaseTaskReview({
  record,
  workspace,
  csrfToken,
}: {
  record: CaseView;
  workspace: WorkspaceView;
  csrfToken: string;
}) {
  const router = useRouter();
  const [requestId] = useState(() => crypto.randomUUID());
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string>();
  if (!workspace.permissions.includes("investigate")) return null;
  const available =
    record.invoice_links.length > 0 && record.invoice_links.length <= 3;
  async function submit() {
    setPending(true);
    setError(undefined);
    try {
      const run = await agentApi.start(
        workspace.id,
        caseReviewCommand(record, requestId),
        csrfToken,
      );
      router.push(
        `/agent-runs/${encodeURIComponent(run.id)}?workspace=${encodeURIComponent(workspace.id)}`,
      );
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : "Case investigation could not be started.",
      );
    } finally {
      setPending(false);
    }
  }
  return (
    <section className="panel" aria-label="Case task review">
      <h3>Independent task review</h3>
      <p>
        Start an investigation for case version {record.version} and all{" "}
        {record.invoice_links.length} linked invoices. The saved proposal must
        be reviewed by another eligible person before its task can execute.
      </p>
      <p>
        This does not resolve an issue, change accounting or authorize outreach.
      </p>
      {!available && (
        <p>
          The full case scope exceeds the current investigation limit; no
          partial case will be selected.
        </p>
      )}
      {error && <p role="alert">{error}</p>}
      <button
        type="button"
        disabled={!available || pending}
        onClick={() => void submit()}
      >
        {pending
          ? "Starting case investigation…"
          : "Request independent task review"}
      </button>
    </section>
  );
}
