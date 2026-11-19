import type {
  ApproveEconomicsAssumptions,
  CostAllocationView,
  EconomicsAssumptionView,
  EconomicsView,
  SaveCostAllocation,
  UsageCostEvidence,
} from "@/lib/generated-api";
import type { ImpactWindow } from "./api";

export type EconomicsReview = {
  assumption: EconomicsAssumptionView;
  currentApprovalId: string | null;
};
export const captureEconomicsReview = (
  assumption: EconomicsAssumptionView,
  currentApprovalId: string | null,
): EconomicsReview => structuredClone({ assumption, currentApprovalId });
export function canApproveEconomics(
  review: EconomicsReview | null,
  current: EconomicsView | undefined,
  fresh: boolean,
): boolean {
  if (
    !review ||
    !current ||
    !fresh ||
    current.can_manage !== true ||
    review.currentApprovalId !== current.current_approval_id ||
    review.assumption.id !== current.latest_assumption_id
  )
    return false;
  const saved = current.assumptions.find(
    (item) => item.id === review.assumption.id,
  );
  return (
    !!saved &&
    !saved.approval_id &&
    saved.reviewed_hash === review.assumption.reviewed_hash &&
    JSON.stringify(saved.command) === JSON.stringify(review.assumption.command)
  );
}
export const approvalCommand = (
  review: EconomicsReview,
  requestId: string,
  evidence: string,
): ApproveEconomicsAssumptions => ({
  request_id: requestId,
  expected_current_approval_id: review.currentApprovalId,
  reviewed_hash: review.assumption.reviewed_hash,
  evidence,
});
export const captureAllocation = (
  event: UsageCostEvidence,
): UsageCostEvidence => structuredClone(event);
export function allocationCommand(
  event: UsageCostEvidence,
  requestId: string,
  evidence: string,
): SaveCostAllocation {
  if (!event.revision_id) throw Error("Original usage revision unavailable.");
  return {
    request_id: requestId,
    expected_version_id: event.allocation.id,
    expected_revision_id: event.revision_id,
    reviewed_source_hash: event.allocation.source_hash,
    leaves: structuredClone(event.allocation.supported_leaves),
    evidence,
  };
}
export function sameAllocation(
  reviewed: UsageCostEvidence,
  current: UsageCostEvidence | undefined,
): boolean {
  return (
    !!current &&
    reviewed.event_id === current.event_id &&
    reviewed.revision_id === current.revision_id &&
    reviewed.allocation.id === current.allocation.id &&
    reviewed.allocation.source_hash === current.allocation.source_hash
  );
}
function exact(value: unknown) {
  if (
    value != null &&
    (typeof value !== "string" ||
      !/^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(value))
  )
    throw Error(
      "Economics evidence requires exact decimal strings; refresh after service precision is corrected.",
    );
}
export function validateEconomics(view: EconomicsView): EconomicsView {
  for (const group of view.groups) {
    [
      group.reported_human_minutes,
      group.measured_human_cost,
      group.estimated_reported_human_cost,
      group.estimated_cost_per_resolution,
      group.known_provider_cost,
      ...[
        group.costs.provider,
        group.costs.human,
        group.costs.fixed,
        group.costs.complete_cost_per_resolution,
      ],
      group.capacity.estimated_time_saved_minutes,
      group.capacity.estimated_capacity_value,
      group.capacity.estimated_net_benefit,
      group.capacity.incremental_cost_denominator,
      group.capacity.estimated_roi_percent,
    ].forEach(exact);
  }
  for (const event of view.events) {
    exact(event.amount);
    for (const leaf of [
      ...event.allocation.leaves,
      ...event.allocation.supported_leaves,
    ])
      exact(leaf.weight);
  }
  for (const row of [...view.case_costs, ...view.invoice_costs])
    exact(row.known_amount);
  for (const row of view.assumptions)
    [
      row.command.baseline_minutes_per_issue,
      row.command.wage_per_hour,
      row.command.fixed_cost,
    ].forEach(exact);
  return view;
}
const base = (workspace: string) =>
  `/api/workspaces/${encodeURIComponent(workspace)}/impact/economics`;
function failure(status: number): Error {
  return Error(
    status === 409
      ? "The reviewed record or source changed. Refresh and explicitly review again; no automatic retry."
      : status === 401
        ? "Sign in again before continuing."
        : status === 403 || status === 404
          ? "Your current membership or source scope does not permit this operation."
          : status === 422
            ? "Check the exact decimal values, complete weights, evidence and reporting window."
            : "The result is not confirmed. Refresh saved records before trying again.",
  );
}
async function post<T>(url: string, command: object, csrf: string): Promise<T> {
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "content-type": "application/json", "x-csrf-token": csrf },
    body: JSON.stringify(command),
  });
  if (!response.ok) throw failure(response.status);
  return response.json();
}
export const economicsApi = {
  async read(
    workspace: string,
    window: ImpactWindow,
    signal?: AbortSignal,
  ): Promise<EconomicsView> {
    const response = await fetch(
      `${base(workspace)}?${new URLSearchParams(window)}`,
      { credentials: "same-origin", cache: "no-store", signal },
    );
    if (!response.ok) throw failure(response.status);
    return validateEconomics(await response.json());
  },
  save: (
    workspace: string,
    command: EconomicsAssumptionView["command"],
    csrf: string,
  ) =>
    post<EconomicsAssumptionView>(
      `${base(workspace)}/assumptions`,
      command,
      csrf,
    ),
  approve: (
    workspace: string,
    id: string,
    command: ApproveEconomicsAssumptions,
    csrf: string,
  ) =>
    post<EconomicsAssumptionView>(
      `${base(workspace)}/assumptions/${encodeURIComponent(id)}/approve`,
      command,
      csrf,
    ),
  allocate: (
    workspace: string,
    eventId: string,
    command: SaveCostAllocation,
    csrf: string,
  ) =>
    post<CostAllocationView>(
      `${base(workspace)}/usage/${encodeURIComponent(eventId)}/allocation`,
      command,
      csrf,
    ),
};
