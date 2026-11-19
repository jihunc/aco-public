import { readApi } from "@/lib/api";
import type {
  ChangeMember,
  MemberChangeView,
  MemberView,
} from "@/lib/generated-api";
export type MemberChange = Pick<ChangeMember, "role" | "active">;
export type ReviewedMember = { member: MemberView; change: MemberChange };
export const sameMemberReview = (a: MemberView, b: MemberView) =>
  a.user_id === b.user_id &&
  a.version === b.version &&
  a.role === b.role &&
  a.active === b.active;
const path = (workspace: string) =>
  `/api/workspaces/${encodeURIComponent(workspace)}/members`;
export const membersApi = {
  list: (workspace: string, signal?: AbortSignal) =>
    readApi<MemberView[]>(path(workspace), signal),
  change: async (
    workspace: string,
    reviewed: MemberView,
    change: MemberChange,
    csrf: string,
  ): Promise<MemberChangeView> => {
    const command: ChangeMember = {
      ...change,
      expected_version: reviewed.version,
    };
    const response = await fetch(
      `${path(workspace)}/${encodeURIComponent(reviewed.user_id)}`,
      {
        method: "PATCH",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "content-type": "application/json", "x-csrf-token": csrf },
        body: JSON.stringify(command),
      },
    );
    if (!response.ok) {
      const payload: unknown = await response.json().catch(() => null);
      const detail =
        payload && typeof payload === "object" && "detail" in payload
          ? payload.detail
          : null;
      throw Error(
        detail === "LAST_OWNER"
          ? "Keep at least one active Owner. Promote another member before changing the last Owner."
          : response.status === 409
            ? "Membership changed. Refresh and review the current membership again."
            : response.status === 403
              ? "Current Owner permission is required. Refresh your workspace."
              : "Membership save is not confirmed. Refresh before reviewing another change.",
      );
    }
    return response.json();
  },
};
