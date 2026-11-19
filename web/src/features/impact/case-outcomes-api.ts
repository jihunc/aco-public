import type { CaseOutcomesView } from "@/lib/generated-api";
import type { ImpactWindow } from "./api";
export type CaseOutcomeWindow = ImpactWindow & { as_of?: string };
function exact(value: unknown) {
  if (
    value != null &&
    (typeof value !== "string" ||
      !/^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(value))
  )
    throw new Error(
      "Case outcome evidence requires exact decimal strings; reload after service precision is corrected.",
    );
}
export const caseOutcomesApi = {
  async read(
    workspace: string,
    window: CaseOutcomeWindow,
    signal?: AbortSignal,
  ): Promise<CaseOutcomesView> {
    const response = await fetch(
      `/api/workspaces/${encodeURIComponent(workspace)}/impact/case-outcomes?${new URLSearchParams(window)}`,
      {
        credentials: "same-origin",
        cache: "no-store",
        signal,
      },
    );
    if (!response.ok)
      throw new Error(
        response.status === 401
          ? "Sign in again to read recorded case outcomes."
          : response.status === 403 || response.status === 404
            ? "Case outcomes are unavailable to your current workspace membership."
            : response.status === 422
              ? "Narrow the reporting window or choose a timezone-aware as-of time within recorded history and no later than now."
              : "Recorded case history could not be loaded. Try reading it again.",
      );
    const value = (await response.json()) as CaseOutcomesView;
    for (const totals of [value, ...value.groups]) {
      [
        totals.autonomous_resolution_percent,
        totals.human_intervention_percent,
        totals.mean_resolution_seconds,
        totals.mean_open_age_seconds,
        totals.reported_human_minutes,
        totals.known_reported_human_minutes,
      ].forEach(exact);
    }
    exact(value.reported_human_minutes);
    for (const row of value.cases) {
      exact(row.resolution_seconds);
      exact(row.backlog_age_seconds);
      exact(row.reported_human_minutes);
      exact(row.known_reported_human_minutes);
    }
    return value;
  },
};
