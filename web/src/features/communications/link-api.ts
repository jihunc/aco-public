import { readApi } from "@/lib/api";
import type {
  CommunicationLinkReview,
  LinkCommunication,
} from "@/lib/generated-api";
import { communicationMutation, type Communication } from "./api";
export type LinkReview = CommunicationLinkReview;
export function capturedLinkCommand(
  review: LinkReview,
  source: Communication,
  invoiceIds: string[],
  excerpt: string,
  offset: number,
): LinkCommunication {
  if (
    !review.can_link ||
    !review.source_sha256 ||
    review.communication_id !== source.id
  )
    throw new Error("Current retained-source review is required.");
  if (
    !invoiceIds.length ||
    invoiceIds.length > 3 ||
    new Set(invoiceIds).size !== invoiceIds.length
  )
    throw new Error("Choose one to three distinct invoice references.");
  const choices = invoiceIds.map((id) =>
    review.candidates.find((candidate) => candidate.invoice_id === id),
  );
  if (
    choices.some((choice) => !choice) ||
    new Set(choices.map((choice) => choice?.customer_id)).size !== 1
  )
    throw new Error(
      "Invoice references must share the reviewed customer scope.",
    );
  const body = Array.from(source.content.text_body ?? "");
  if (
    !Number.isInteger(offset) ||
    offset < 0 ||
    !excerpt ||
    Array.from(excerpt).length > 8000 ||
    body.slice(offset, offset + Array.from(excerpt).length).join("") !== excerpt
  )
    throw new Error(
      "The excerpt must match the original text at its character offset.",
    );
  const first = choices[0];
  if (!first) throw new Error("Choose a reviewed invoice reference.");
  return {
    customer_id: first.customer_id,
    invoice_ids: invoiceIds,
    expected_version: review.communication_version,
    source_sha256: review.source_sha256,
    evidence: { communication_id: source.id, excerpt, offset },
  };
}
export const communicationLinkApi = {
  review: (workspace: string, id: string, signal?: AbortSignal) =>
    readApi<LinkReview>(
      `/api/workspaces/${encodeURIComponent(workspace)}/communications/${encodeURIComponent(id)}/link-review`,
      signal,
    ),
  save: (
    workspace: string,
    id: string,
    command: LinkCommunication,
    csrf: string,
  ) =>
    communicationMutation<Communication>(
      workspace,
      `/communications/${encodeURIComponent(id)}/invoice-links`,
      command,
      csrf,
    ),
};
