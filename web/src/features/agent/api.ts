// Validated API contract: features/agent/schemas.py. Root owns OpenAPI generation.
export type RunStatus =
  | "queued"
  | "running"
  | "awaiting_approval"
  | "completed"
  | "partially_completed"
  | "failed"
  | "cancelled";
export type InvestigationResult =
  import("@/lib/generated-api").InvestigationResult;
export type AgentRun = {
  id: string;
  status: RunStatus;
  version: number;
  invoice_ids: string[];
  objective: string;
  authenticity: "FAKE" | "LIVE";
  model_id: string | null;
  model_route: string;
  result: InvestigationResult | null;
  error_code: string | null;
  created_at: string;
  updated_at: string;
  last_event_sequence: number;
  execution: "UNAVAILABLE" | "PROPOSED" | "EXECUTED" | "PARTIAL";
  usage_ids: string[];
  input_tokens: number | null;
  output_tokens: number | null;
  requests: number;
  provider_attempted: boolean;
};
export type AgentStep = {
  id: string;
  run_id: string;
  step_sequence: number;
  kind: "model" | "read" | "action";
  status: "started" | "completed" | "failed";
  tool_name: string | null;
  description: string;
  invoice_id: string | null;
  input: Record<string, unknown>;
  output: Record<string, unknown> | null;
  error_code: string | null;
  usage_ids: string[];
  created_at: string;
  updated_at: string;
};
export type AgentHistory = { items: AgentStep[]; last_event_sequence: number };
export type AgentEvent = {
  id: string;
  run_id: string;
  event_sequence: number;
  kind: string;
  step_id: string | null;
  description: string;
  created_at: string;
};
export type AgentEvents = { items: AgentEvent[]; last_event_sequence: number };
export type StartInvestigation = {
  invoice_ids: string[];
  objective: string;
  request_id: string;
};
export const agentPath = (workspace: string, runId?: string) =>
  `/api/workspaces/${encodeURIComponent(workspace)}/agent-runs${runId ? `/${encodeURIComponent(runId)}` : ""}`;
export const isActiveRun = (status: RunStatus) =>
  ["queued", "running", "awaiting_approval", "awaiting_action"].includes(status);
async function request<T>(
  path: string,
  signal?: AbortSignal,
  command?: object,
  csrf?: string,
): Promise<T> {
  const response = await fetch(path, {
    method: command ? "POST" : "GET",
    credentials: "same-origin",
    cache: "no-store",
    signal,
    ...(command
      ? {
          headers: {
            "content-type": "application/json",
            "x-csrf-token": csrf ?? "",
          },
          body: JSON.stringify(command),
        }
      : {}),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail =
      typeof payload.detail === "object" && payload.detail
        ? payload.detail.code
        : payload.detail;
    const code = typeof payload.code === "string" ? payload.code : detail;
    const safeCode =
      typeof code === "string" && /^[A-Z][A-Z0-9_]{1,80}$/.test(code)
        ? ` (${code})`
        : "";
    throw new Error(
      (response.status === 401
        ? "Sign in required. Your session may have expired."
        : response.status === 403 || response.status === 404
          ? "This investigation is unavailable to your current membership."
          : response.status === 409
            ? "This investigation changed. Refresh its current state before trying again."
            : response.status === 422
              ? "Select one to three distinct invoices and enter an objective of at most 1,000 characters."
              : "The investigation service is unavailable. Refresh recent investigations before retrying.") +
        safeCode,
    );
  }
  return response.json();
}
export const agentApi = {
  list: (workspace: string, signal?: AbortSignal) =>
    request<{ items: AgentRun[] }>(agentPath(workspace), signal),
  run: (workspace: string, run: string, signal?: AbortSignal) =>
    request<AgentRun>(agentPath(workspace, run), signal),
  steps: (workspace: string, run: string, signal?: AbortSignal) =>
    request<AgentHistory>(`${agentPath(workspace, run)}/steps`, signal),
  events: (
    workspace: string,
    run: string,
    after: number,
    signal?: AbortSignal,
  ) =>
    request<AgentEvents>(
      `${agentPath(workspace, run)}/event-log?after=${after}`,
      signal,
    ),
  start: (workspace: string, command: StartInvestigation, csrf: string) =>
    request<AgentRun>(agentPath(workspace), undefined, command, csrf),
  cancel: (workspace: string, run: string, version: number, csrf: string) =>
    request<AgentRun>(
      `${agentPath(workspace, run)}/cancel`,
      undefined,
      { expected_version: version },
      csrf,
    ),
};
