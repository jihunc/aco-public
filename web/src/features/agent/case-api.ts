import type { Communication } from "@/features/communications/api";
import type {
  CaseMutation,
  DispositionReference,
  DispositionReferences,
  CaseView,
  CommunicationEvidence,
  InvoiceIssueTransition,
  InvoiceView,
  TransitionCase,
  UpsertCase,
} from "@/lib/generated-api";
export type CaseCapture = {
  invoice: InvoiceView;
  source: Communication;
  excerpt: string;
  invoiceIds: string[];
  issueType: UpsertCase["issue_type"];
  claimKey: string;
  substance: string;
};
export function exactCaseEvidence(
  source: Communication,
  excerpt: string,
  invoiceIds: string[],
): CommunicationEvidence {
  const body = source.content.text_body ?? "";
  const index = body.indexOf(excerpt);
  if (
    source.direction !== "incoming" ||
    source.source_state !== "stored" ||
    !excerpt.trim() ||
    index < 0 ||
    !invoiceIds.length ||
    invoiceIds.some(
      (id) => !source.invoice_links.some((link) => link.invoice_id === id),
    )
  )
    throw new Error(
      "Choose an exact span from a stored incoming message linked to every selected invoice.",
    );
  return {
    communication_id: source.id,
    excerpt,
    offset: Array.from(body.slice(0, index)).length,
  };
}
async function mutate<T>(
  workspace: string,
  path: string,
  body: object,
  csrf: string,
  method: "POST" | "PATCH" = "POST",
): Promise<T> {
  if (!csrf) throw new Error("Sign in again before changing this case.");
  const response = await fetch(
    `/api/workspaces/${encodeURIComponent(workspace)}/cases${path}`,
    {
      method,
      credentials: "same-origin",
      cache: "no-store",
      headers: { "content-type": "application/json", "x-csrf-token": csrf },
      body: JSON.stringify(body),
    },
  );
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    const message =
      response.status === 409
        ? "The case, linked invoice issues or evidence changed. Refresh and review before trying again."
        : response.status === 403 || response.status === 404
          ? "Current membership or source scope does not permit this change."
          : response.status === 422
            ? "Review the evidence, issue disposition and required references."
            : "The outcome could not be confirmed. Refresh case history before retrying.";
    throw new Error(
      message +
        (typeof detail.detail === "string" && /^[A-Z_]+$/.test(detail.detail)
          ? ` (${detail.detail})`
          : ""),
    );
  }
  return response.json();
}
export const caseApi = {
  async dispositionReferences(
    workspace: string,
    caseId: string,
    invoiceId: string,
  ): Promise<DispositionReferences> {
    const response = await fetch(
      `/api/workspaces/${encodeURIComponent(workspace)}/cases/${encodeURIComponent(caseId)}/disposition-references?${new URLSearchParams({ invoice_id: invoiceId })}`,
      { credentials: "same-origin", cache: "no-store" },
    );
    if (!response.ok)
      throw new Error(
        "Current verified disposition references are unavailable. Review your membership and case scope before retrying.",
      );
    return response.json();
  },
  async create(
    workspace: string,
    command: CaseCapture,
    csrf: string,
  ): Promise<CaseMutation> {
    const {
      invoice,
      source,
      excerpt,
      invoiceIds,
      issueType,
      claimKey,
      substance,
    } = command;
    if (
      !claimKey.trim() ||
      !substance.trim() ||
      !invoiceIds.includes(invoice.id) ||
      new Set(invoiceIds).size !== invoiceIds.length
    )
      throw new Error(
        "Specify the claim, issue and exact affected invoice scope.",
      );
    const evidence = exactCaseEvidence(source, excerpt, invoiceIds);
    const body: UpsertCase = {
      customer_id: invoice.customer.id,
      issue_type: issueType,
      claim_key: claimKey.trim(),
      substance: substance.trim(),
      invoice_ids: invoiceIds,
      evidence,
      matching: "new",
    };
    return mutate(workspace, "", body, csrf);
  },
  async transition(
    workspace: string,
    record: Pick<CaseView, "id" | "version">,
    status: TransitionCase["status"],
    reason: string,
    csrf: string,
    extra: Pick<
      TransitionCase,
      "closure_reason" | "surviving_case_id" | "evidence"
    > = {},
  ): Promise<CaseView> {
    if (!reason.trim())
      throw new Error("Record the reason for this status change.");
    return mutate(
      workspace,
      `/${encodeURIComponent(record.id)}`,
      {
        case_id: record.id,
        expected_version: record.version,
        status,
        reason: reason.trim(),
        ...extra,
      },
      csrf,
      "PATCH",
    );
  },
  async resolveInvoice(
    workspace: string,
    record: Pick<CaseView, "id" | "version" | "invoice_links">,
    invoiceId: string,
    evidence: InvoiceIssueTransition["evidence"],
    kind: InvoiceIssueTransition["resolution_kind"],
    action: string,
    referenceId: string | null,
    csrf: string,
    captured?: DispositionReference,
  ): Promise<CaseView> {
    if (
      !record.invoice_links.some((link) => link.invoice_id === invoiceId) ||
      !action.trim()
    )
      throw new Error(
        "Select a current case invoice and document its actual disposition.",
      );
    return mutate(
      workspace,
      `/${encodeURIComponent(record.id)}/invoice-resolution`,
      {
        case_id: record.id,
        invoice_id: invoiceId,
        expected_version: record.version,
        evidence,
        resolution_kind: kind,
        resolution_action: action.trim(),
        resolution_reference_id: referenceId,
        ...(captured
          ? {
              expected_reference_version: captured.version,
              expected_reference_hash: captured.material_hash,
            }
          : {}),
      },
      csrf,
    );
  },
  async linkEvidence(
    workspace: string,
    record: Pick<CaseView, "id" | "version">,
    evidence: CommunicationEvidence,
    csrf: string,
  ): Promise<unknown> {
    return mutate(
      workspace,
      `/${encodeURIComponent(record.id)}/evidence`,
      { case_id: record.id, expected_version: record.version, evidence },
      csrf,
    );
  },
};
