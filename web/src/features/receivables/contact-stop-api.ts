import { readApi } from "@/lib/api";
import type {
  ContactStopPage,
  ContactStopView,
  ReleaseContactStop,
} from "@/lib/generated-api";
export type ContactStopCapture = {
  review: ContactStopView;
  command: ReleaseContactStop;
};
export function captureContactStop(
  review: ContactStopView,
  reason: string,
  requestId = crypto.randomUUID(),
): ContactStopCapture {
  reason = reason.trim();
  if (!review.can_release || !review.pending || !review.evidence_complete)
    throw new Error(
      "Current permission and complete pending review are required.",
    );
  if (Array.from(reason).length < 10 || Array.from(reason).length > 1000)
    throw new Error("Record a substantive review reason (10–1000 characters).");
  return {
    review: structuredClone(review),
    command: {
      request_id: requestId,
      expected_action_version: review.action_version,
      reviewed_holds: review.original_holds.map((h) => ({
        hold_id: h.id,
        expected_version: h.version,
      })),
      reason,
    },
  };
}
export function restoreContactStopCapture(
  raw: string | null,
): ContactStopCapture | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as ContactStopCapture;
    const { review, command } = value;
    if (
      typeof review.action_id !== "string" ||
      (typeof review.source_communication_id !== "string" &&
        !(
          review.source_communication_id === null &&
          review.voice_source &&
          typeof review.voice_source.id === "string" &&
          typeof review.voice_source.facts_hash === "string"
        )) ||
      typeof review.source_excerpt !== "string" ||
      typeof review.pending !== "boolean" ||
      typeof review.evidence_complete !== "boolean" ||
      typeof review.can_release !== "boolean" ||
      !Array.isArray(review.original_holds) ||
      !review.original_holds.length ||
      !review.original_holds.every(
        (h) =>
          typeof h.id === "string" &&
          typeof h.invoice_id === "string" &&
          Number.isSafeInteger(h.version),
      ) ||
      typeof command.request_id !== "string" ||
      typeof command.reason !== "string" ||
      !Number.isSafeInteger(command.expected_action_version) ||
      !Array.isArray(command.reviewed_holds) ||
      !command.reviewed_holds.every(
        (h) =>
          typeof h.hold_id === "string" &&
          Number.isSafeInteger(h.expected_version),
      )
    )
      return null;
    return value;
  } catch {
    return null;
  }
}
export class ContactStopError extends Error {
  constructor(
    message: string,
    readonly refreshRequired = false,
  ) {
    super(message);
  }
}
const base = (workspace: string) =>
  `/api/workspaces/${encodeURIComponent(workspace)}`;
export const contactStopApi = {
  list: (workspace: string, invoice: string, signal?: AbortSignal) =>
    readApi<ContactStopPage>(
      `${base(workspace)}/invoices/${encodeURIComponent(invoice)}/contact-stops`,
      signal,
    ),
  async release(
    workspace: string,
    captured: ContactStopCapture,
    csrf: string,
  ): Promise<ContactStopView> {
    if (!csrf)
      throw new ContactStopError(
        "Sign in again before releasing a restriction.",
      );
    const response = await fetch(
      `${base(workspace)}/contact-stops/${encodeURIComponent(captured.review.action_id)}/release`,
      {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "content-type": "application/json", "x-csrf-token": csrf },
        body: JSON.stringify(captured.command),
      },
    ).catch(() => {
      throw new ContactStopError(
        "Release could not be confirmed. Retry this same captured review to confirm its outcome.",
      );
    });
    if (!response.ok)
      throw new ContactStopError(
        response.status === 409
          ? "The original holds or review changed. Explicitly refresh and review before release."
          : response.status === 403 || response.status === 404
            ? "Current membership does not permit this release."
            : "Release could not be confirmed. Retry this same captured review to confirm its outcome.",
        response.status === 409,
      );
    return response.json();
  },
};
