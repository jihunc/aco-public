import { readApi } from "@/lib/api";
import type { EmailExecutionView, ProviderEmailReviewView } from "@/lib/generated-api";

export type EmailAttachment = { filename: string; content_type: string; size_bytes: number | null; reference: { artifact_id?: string | null } };
export type EmailContent = { sender: string | null; to: string[]; cc: string[]; bcc: string[]; subject: string;
  text_body: string | null; html_body: string | null; attachments: EmailAttachment[];
  message_id: string | null; in_reply_to: string | null; references: string[] };
export type Communication = { id: string; thread_id: string; origin_invoice_id: string | null; provider_connection_id: string;
  native_message_id: string | null; direction: string; channel: string; kind: string; synthetic: boolean;
  event_at: string; delivery_state: string; source_state: string; content: EmailContent;
  invoice_links: { invoice_id: string; provenance: string; evidence_excerpt: string; confidence: string }[];
  sources: { artifact_id: string; representation: string; sha256: string; storage_state: string; available: boolean }[];
  delivery_events: { state: string; occurred_at: string }[]; action_id: string | null };
export type CommunicationPage = { items: Communication[]; page: number; page_size: number; total: number };
export type Thread = { id: string; origin_invoice_id: string | null; customer_id: string | null; messages: Communication[] };
export type EmailExecution = EmailExecutionView;
export type ScheduledAction = { id: string; version: number; status: string; due_at: string; last_error: string | null;
  kind: string; invoice_id: string; executions: { status: string; started_at: string; finished_at: string | null }[] };
const base = (workspace: string) => `/api/workspaces/${encodeURIComponent(workspace)}`;
export const communicationsApi = {
  list: (workspace: string, filters: Record<string, string | number | undefined>, signal?: AbortSignal) => {
    const query = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => { if (value !== undefined && value !== "") query.set(key, String(value)); });
    return readApi<CommunicationPage>(`${base(workspace)}/communications?${query}`, signal);
  },
  thread: (workspace: string, id: string, signal?: AbortSignal) => readApi<Thread>(`${base(workspace)}/communication-threads/${id}`, signal),
  execution: (workspace: string, signal?: AbortSignal) => readApi<EmailExecution>(`${base(workspace)}/email-execution`, signal),
  providerReview: (workspace: string, signal?: AbortSignal) => readApi<ProviderEmailReviewView>(`${base(workspace)}/email-execution/provider`, signal),
  actions: (workspace: string, status: "queued" | "unknown_outcome", signal?: AbortSignal) =>
    readApi<ScheduledAction[]>(`${base(workspace)}/scheduled-actions?kind=policy_due&status=${status}`, signal),
};
export async function communicationMutation<T>(workspace: string, path: string, body: object, csrf: string, method: "POST" | "PUT" = "POST"): Promise<T> {
  const response = await fetch(`${base(workspace)}${path}`, { method, cache: "no-store", credentials: "same-origin",
    headers: { "content-type": "application/json", "x-csrf-token": csrf }, body: JSON.stringify(body) });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(typeof payload.detail === "string" ? payload.detail : `Request failed (${response.status}).`);
  }
  return response.json();
}
