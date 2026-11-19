import type {
  ApprovalModificationCommand,
  VoiceApprovalProposal,
  AgentEmailApprovalProposal,
} from "@/lib/generated-api";
import { readApi } from "@/lib/api";
// Exact response/command contract: features/review/approval_dto.py.
export type OutboundApprovalProposal = {
  action_id: string;
  action_version: number;
  content: string;
  destination: string;
  channel: "email" | "voice";
  invoice_ids: string[];
  evidence_ids: string[];
  policy_version_id: string;
  cost_bound: string;
  currency: string;
  consent_mode: "no_capture" | "approved_capture";
};
export type InternalTaskApprovalProposal =
  import("@/lib/generated-api").InternalTaskApprovalProposal;

export type Approval = {
  can_modify?: boolean;
  modification_reasons?: string[];
  supersedes_id?: string | null;
  replacement_id?: string | null;
  editor_id?: string | null;
  edit_reason?: string | null;
  id: string;
  version: number;
  action_id: string;
  action_version: number;
  proposer_id: string;
  initiator_id: string;
  source_action_initiator_id: string;
  customer_id: string;
  material_hash: string;
  current_binding_hash: string;
  proposal:
    | AgentEmailApprovalProposal
    | OutboundApprovalProposal
    | InternalTaskApprovalProposal
    | VoiceApprovalProposal;
  effect: "outbound_message" | "internal_task" | "voice_session";
  source_run_id: string | null;
  source_step_id: string | null;
  agent_evidence: {
    step_id: string;
    run_id: string;
    tool_name: string;
    source_json: string;
  }[];
  changed_ids: string[];
  execution_status:
    "pending_approval" | "queued" | "executed" | "denied" | "stale" | "failed";
  invoice_scope: {
    invoice_id: string;
    number: string;
    customer_id: string;
    source_revision: string;
    outstanding: { minor_units: string; currency: string; exponent: number };
    financial_status: string;
    observed_at: string;
    synthetic: boolean;
    verified: boolean;
  };
  evidence: {
    id: string;
    invoice_id: string;
    source_revision: string;
    origin: string;
    source_json: string;
  }[];
  status: string;
  effective_status:
    "pending" | "approved" | "denied" | "expired" | "stale" | "consumed";
  approval_valid: boolean;
  new_approval_required: boolean;
  can_approve: boolean;
  can_deny: boolean;
  reasons: string[];
  granted_at: string | null;
  expires_at: string | null;
  consumed_at: string | null;
  created_at: string;
  updated_at: string;
  decisions: {
    id: string;
    approver_id: string;
    decision: string;
    reason: string;
    material_hash: string;
    created_at: string;
  }[];
  dispatch_status: "NOT_ENABLED" | "NOT_APPLICABLE" | "AWAITING_REVIEW" | "QUEUED" | "ACCEPTED" | "UNKNOWN" | "STOPPED";
};
export type DecisionCommand = {
  expected_version: number;
  reviewed_action_version: number;
  reviewed_material_hash: string;
  reviewed_binding_hash: string;
  decision: "approved" | "denied";
  reason: string;
  expires_in_seconds: number;
};
export const captureApproval = (record: Approval): Approval =>
  structuredClone(record);
export function sameApprovalSnapshot(a: Approval, b: Approval): boolean {
  return (
    a.id === b.id &&
    a.version === b.version &&
    a.action_version === b.action_version &&
    a.material_hash === b.material_hash &&
    a.current_binding_hash === b.current_binding_hash
  );
}
export function decisionCommand(
  record: Approval,
  decision: DecisionCommand["decision"],
  reason: string,
  seconds: number,
): DecisionCommand {
  return {
    expected_version: record.version,
    reviewed_action_version: record.action_version,
    reviewed_material_hash: record.material_hash,
    reviewed_binding_hash: record.current_binding_hash,
    decision,
    reason: reason.trim(),
    expires_in_seconds: seconds,
  };
}
export function modificationCommand(
  record: Approval,
  edit: ApprovalModificationCommand["edit"],
  reason: string,
): ApprovalModificationCommand {
  return {
    expected_version: record.version,
    reviewed_action_version: record.action_version,
    reviewed_material_hash: record.material_hash,
    reviewed_binding_hash: record.current_binding_hash,
    edit: structuredClone(edit),
    reason: reason.trim(),
  };
}
const path = (workspace: string) =>
  `/api/workspaces/${encodeURIComponent(workspace)}/approvals`;
export const approvalApi = {
  modify: async (
    workspace: string,
    id: string,
    command: ApprovalModificationCommand,
    csrf: string,
  ): Promise<Approval> => {
    const response = await fetch(
      `${path(workspace)}/${encodeURIComponent(id)}/modifications`,
      {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "content-type": "application/json", "x-csrf-token": csrf },
        body: JSON.stringify(command),
      },
    );
    if (!response.ok)
      throw new Error(
        response.status === 409
          ? "The proposal changed or cannot be modified. Review its current history before trying again."
          : response.status === 403
            ? "Your current role cannot modify this proposal."
            : response.status === 422
              ? "Enter changed proposal text and a reason within the displayed limits."
              : "The modification could not be confirmed. Refresh the original history before trying again.",
      );
    return response.json();
  },
  list: (workspace: string, signal?: AbortSignal) =>
    readApi<{ items: Approval[] }>(path(workspace), signal),
  get: (workspace: string, id: string, signal?: AbortSignal) =>
    readApi<Approval>(`${path(workspace)}/${encodeURIComponent(id)}`, signal),
  decide: async (
    workspace: string,
    id: string,
    command: DecisionCommand,
    csrf: string,
  ): Promise<Approval> => {
    const response = await fetch(
      `${path(workspace)}/${encodeURIComponent(id)}/decisions`,
      {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "content-type": "application/json", "x-csrf-token": csrf },
        body: JSON.stringify(command),
      },
    );
    if (!response.ok) {
      throw new Error(
        response.status === 409
          ? "This proposal changed. Review the current proposal before deciding again."
          : response.status === 403
            ? "Your current membership or reviewer eligibility does not allow this decision."
            : response.status === 401
              ? "Sign in again before reviewing this proposal."
              : response.status === 422
                ? "Enter a reason and an approval duration between 1 and 24 hours."
                : "The decision could not be saved. Refresh its history before trying again.",
      );
    }
    return response.json();
  },
};
