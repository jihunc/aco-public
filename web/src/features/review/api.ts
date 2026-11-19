import { readApi } from "@/lib/api";

export type Task = {
  id: string; invoice_id: string | null; source_action_id: string | null;
  creator_id: string; assignee_id: string | null; title: string; notes: string;
  status: "open" | "in_progress" | "done"; priority: number;
  priority_rank: string; due_at: string | null; deleted_at: string | null;
  version: number; origin: "human" | "generated"; deadline_locked: boolean;
};
export type Queue = { version: number; items: Task[] };
export type Assignee = { user_id: string; email: string };
export const reviewPath = (workspace: string) => `/api/workspaces/${encodeURIComponent(workspace)}`;
export const reviewApi = {
  queue: (workspace: string, signal?: AbortSignal) => readApi<Queue>(`${reviewPath(workspace)}/review-queue`, signal),
  tasks: (workspace: string, invoiceId?: string, signal?: AbortSignal) => readApi<{ items: Task[] }>(`${reviewPath(workspace)}/tasks${invoiceId ? `?invoice_id=${encodeURIComponent(invoiceId)}` : ""}`, signal),
  assignees: (workspace: string, signal?: AbortSignal) => readApi<Assignee[]>(`${reviewPath(workspace)}/task-assignees`, signal),
};
export async function reviewMutate<T>(path: string, method: string, body: object, csrf: string): Promise<T> {
  const response = await fetch(path, { method, credentials: "same-origin", cache: "no-store",
    headers: { "content-type": "application/json", "x-csrf-token": csrf }, body: JSON.stringify(body) });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const code = typeof payload.detail === "string" ? payload.detail : "REQUEST_FAILED";
    throw new Error(response.status === 409 ? "This review changed. Refresh and try again." :
      response.status === 403 ? "Your current membership does not allow this change." :
      response.status === 422 ? "Check the task fields and selected workspace." : code);
  }
  return response.json();
}
