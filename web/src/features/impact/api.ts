// Exact read contract: features/impact/dto.py. Root owns generated client composition.
import type {
  MoneyJson,
  OperationsView,
  UsageSummary,
} from "@/lib/generated-api";
export type ImpactWindow = { since: string; until: string };
export type CashGroup = {
  currency: string;
  exponent: number;
  synthetic: boolean;
  cash: MoneyJson | null;
  outflows: MoneyJson | null;
  net_cash: MoneyJson | null;
  allocation_count: number;
  non_cash_count: number;
  excluded_count: number;
  completeness: "partial" | "unavailable";
  freshness: "fresh" | "stale" | "unknown";
  last_synced_at: string | null;
  issues: { allocation_id: string; invoice_id: string; reason: string }[];
  evidence: {
    allocation_id: string;
    invoice_id: string;
    provider_connection_id: string;
    source_revision: string;
    kind: string;
    minor_units: string;
    effective_at: string;
    observed_at: string;
    imported_at: string;
    parent_id: string | null;
    included: boolean;
  }[];
};
export type ActivityKind =
  "agent_runs" | "scheduled_actions" | "cases" | "usage_events";
export type ActivityGroup = {
  kind: ActivityKind;
  synthetic: boolean;
  count: number;
  statuses: Record<string, number>;
};
export type ActivityRecord = {
  kind: ActivityKind;
  id: string;
  invoice_id: string | null;
  status: string;
  synthetic: boolean;
  created_at: string;
};
export type ImpactView = ImpactWindow & {
  tenant_id: string;
  as_of_at: string;
  source_warnings: string[];
  cash_basis: string;
  activity_basis: string;
  usage_basis: string;
  operations: OperationsView;
  cash: CashGroup[];
  activity: ActivityGroup[];
  recent_activity: ActivityRecord[];
  usage: UsageSummary;
  unavailable: { metric: string; reason: string }[];
};
function exactMoney(value: MoneyJson | null) {
  if (
    value &&
    (typeof value.minor_units !== "string" ||
      !/^-?\d+$/.test(value.minor_units))
  )
    throw new Error(
      "Source money did not preserve exact decimal-string precision. Refresh after the service is corrected.",
    );
}
export const impactApi = {
  async read(
    workspace: string,
    window: ImpactWindow,
    signal?: AbortSignal,
  ): Promise<ImpactView> {
    const response = await fetch(
      `/api/workspaces/${encodeURIComponent(workspace)}/impact?${new URLSearchParams(window)}`,
      { credentials: "same-origin", cache: "no-store", signal },
    );
    if (!response.ok)
      throw new Error(
        response.status === 401
          ? "Sign in again to read this report."
          : response.status === 403 || response.status === 404
            ? "This report is unavailable to your current workspace membership."
            : response.status === 422
              ? "Use an explicit UTC start before the end. The reporting window cannot end in the future."
              : "The report could not load current source evidence. Try again.",
      );
    const value = (await response.json()) as ImpactView;
    for (const group of value.cash) {
      exactMoney(group.cash);
      exactMoney(group.outflows);
      exactMoney(group.net_cash);
    }
    for (const group of value.operations.currencies) {
      exactMoney(group.outstanding);
      exactMoney(group.overdue);
      exactMoney(group.excluded_balance);
    }
    return value;
  },
};
