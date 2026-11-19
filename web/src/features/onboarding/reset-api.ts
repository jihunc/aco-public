import type {
  ResetRequest,
  ResetResult,
  ResetState,
  WorkspaceView,
} from "@/lib/generated-api";

export type SavedReset = Required<ResetResult>;
export type SavedResetState = Required<ResetState> & {
  last_reset: SavedReset | null;
};
export type ResetReview = {
  command: ResetRequest;
  attempted: boolean;
  captured_at: string;
};
const uuid =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const object = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const generation = (value: unknown): value is number =>
  Number.isSafeInteger(value) && Number(value) >= 1;
const references = (value: unknown): value is string[] =>
  Array.isArray(value) &&
  value.every((id: unknown) => typeof id === "string" && uuid.test(id)) &&
  new Set(value).size === value.length;
const unconfirmed = () =>
  new Error(
    "Reset outcome is unconfirmed. Retry the same captured request to confirm its saved result; do not create a new reset merely to retry.",
  );
function savedReset(value: unknown): SavedReset {
  if (
    !object(value) ||
    typeof value.request_id !== "string" ||
    !uuid.test(value.request_id) ||
    !generation(value.previous_generation) ||
    !generation(value.generation) ||
    value.generation !== value.previous_generation + 1 ||
    value.outbound_paused !== true ||
    value.automation_enabled !== false ||
    !["PENDING", "RESTORED"].includes(String(value.source_restoration)) ||
    ![
      "cancelled_action_ids",
      "unknown_action_ids",
      "preserved_uncertain_action_ids",
      "cancelled_run_ids",
      "released_reservation_ids",
      "restored_invoice_ids",
      "restored_customer_ids",
      "reversal_allocation_ids",
      "source_event_ids",
    ].every((key) => references(value[key]))
  )
    throw unconfirmed();
  if (
    value.source_restoration === "RESTORED" &&
    (!(value.restored_invoice_ids as string[]).length ||
      !(value.restored_customer_ids as string[]).length)
  )
    throw unconfirmed();
  return value as SavedReset;
}
function validCommand(value: unknown): value is ResetRequest {
  return (
    object(value) &&
    typeof value.request_id === "string" &&
    uuid.test(value.request_id) &&
    generation(value.expected_generation) &&
    typeof value.evidence === "string" &&
    value.evidence.trim().length >= 10 &&
    value.evidence.length <= 1000
  );
}
export function canReviewReset(
  workspace: Pick<WorkspaceView, "role" | "is_demo" | "permissions">,
) {
  return (
    workspace.role === "owner" &&
    workspace.is_demo &&
    workspace.permissions.includes("demo_reset")
  );
}
export function captureReset(
  state: SavedResetState,
  evidence: string,
  reviewed: boolean,
): ResetReview {
  if (!state.can_reset || !generation(state.generation))
    throw new Error(
      "Synthetic reset is currently unavailable; read current eligibility.",
    );
  if (!reviewed || evidence.trim().length < 10 || evidence.length > 1000)
    throw new Error(
      "Review retained history and provide reset evidence before capture.",
    );
  return {
    command: {
      request_id: crypto.randomUUID(),
      expected_generation: state.generation,
      evidence: evidence.trim(),
    },
    attempted: false,
    captured_at: new Date().toISOString(),
  };
}
export function readResetReview(raw: string | null): ResetReview | null {
  try {
    const value: unknown = raw ? JSON.parse(raw) : null;
    return object(value) &&
      validCommand(value.command) &&
      typeof value.attempted === "boolean" &&
      typeof value.captured_at === "string" &&
      Number.isFinite(Date.parse(value.captured_at))
      ? (value as ResetReview)
      : null;
  } catch {
    return null;
  }
}
async function failure(response: Response): Promise<Error> {
  if (response.status === 401)
    return new Error("Sign in again before reviewing a reset.");
  if (response.status === 403 || response.status === 404)
    return new Error(
      "Current Owner permission and a permitted demo workspace are required.",
    );
  const payload: unknown = await response.json().catch(() => null);
  const detail = object(payload) ? payload.detail : null;
  if (detail === "DEMO_GENERATION_CONFLICT")
    return new Error(
      "Workspace changed. Refresh your reset review explicitly; its captured generation has not been replaced.",
    );
  if (detail === "DEMO_RESET_REQUEST_CONFLICT")
    return new Error(
      "This request reference belongs to a different captured reset. Refresh review before capturing a new request.",
    );
  if (detail === "DEMO_RESET_SOURCE_REVIEW_REQUIRED")
    return new Error(
      "Synthetic source history changed or is incomplete. No reset was committed; review the retained source history.",
    );
  if (detail === "DEMO_SOURCE_NOT_SEEDED")
    return new Error("Import the canonical demo source before resetting.");
  if (response.status === 503)
    return new Error(
      "Synthetic source reset is unavailable in this environment.",
    );
  return unconfirmed();
}
const path = (workspace: string) =>
  `/api/workspaces/${encodeURIComponent(workspace)}/demo/reset`;
export const resetApi = {
  async read(
    workspace: string,
    signal?: AbortSignal,
  ): Promise<SavedResetState> {
    const response = await fetch(path(workspace), {
      credentials: "same-origin",
      cache: "no-store",
      signal,
    });
    if (!response.ok) throw await failure(response);
    const value: unknown = await response.json();
    if (
      !object(value) ||
      !generation(value.generation) ||
      typeof value.can_reset !== "boolean" ||
      typeof value.outbound_paused !== "boolean" ||
      typeof value.automation_enabled !== "boolean" ||
      !Array.isArray(value.eligibility_reasons) ||
      !value.eligibility_reasons.every((v: unknown) => typeof v === "string") ||
      !(
        value.last_reset_at === null ||
        (typeof value.last_reset_at === "string" &&
          Number.isFinite(Date.parse(value.last_reset_at)))
      )
    )
      throw new Error(
        "Current reset state could not be confirmed. Reload it before reviewing a reset.",
      );
    return {
      ...value,
      last_reset:
        value.last_reset === null ? null : savedReset(value.last_reset),
    } as SavedResetState;
  },
  async submit(
    workspace: WorkspaceView,
    csrf: string,
    command: ResetRequest,
  ): Promise<SavedReset> {
    if (!canReviewReset(workspace))
      throw new Error("Current Owner demo reset permission is required.");
    if (!csrf)
      throw new Error(
        "Sign in again: the current CSRF session is unavailable.",
      );
    if (!validCommand(command))
      throw new Error("Capture a valid reset review before submitting.");
    let response: Response;
    try {
      response = await fetch(path(workspace.id), {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "content-type": "application/json", "x-csrf-token": csrf },
        body: JSON.stringify(command),
      });
    } catch {
      throw unconfirmed();
    }
    if (!response.ok) throw await failure(response);
    const result = savedReset(await response.json().catch(() => null));
    if (
      result.request_id !== command.request_id ||
      result.previous_generation !== command.expected_generation
    )
      throw unconfirmed();
    return result;
  },
};
