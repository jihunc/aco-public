// Response contracts: communications/ingress_dto.py. Never accept raw message content.
import { readApi } from "@/lib/api";
import type { IngressActivationView, IngressReceiptView, SimulateIngress as GeneratedSimulation } from "@/lib/generated-api";

export type IngressActivation = IngressActivationView;
export type IngressConnection = {
  id: string;
  provider: string;
  status: string;
};
export type IngressSetup = {
  can_prepare: boolean;
  enabled: boolean;
  email_connections: IngressConnection[];
  storage_connections: IngressConnection[];
  activations: IngressActivation[];
};
export type IngressReceipt = IngressReceiptView;
export type IngressScenario = GeneratedSimulation["scenario"];
export type SimulateIngress = GeneratedSimulation;
export type PreparedSimulation = {
  activation_id: string;
  command: SimulateIngress;
};
export function prepareSimulation(
  previous: PreparedSimulation | null,
  activation: IngressActivation,
  invoiceIds: string[],
  scenario: IngressScenario,
  newId: () => string = () => crypto.randomUUID(),
): PreparedSimulation {
  if (!activation.synthetic) throw new Error("Synthetic samples require a synthetic activation.");
  if (
    previous &&
    previous.activation_id === activation.id &&
    previous.command.expected_activation_version === activation.version &&
    previous.command.scenario === scenario &&
    previous.command.invoice_ids.join() === invoiceIds.join()
  )
    return previous;
  return {
    activation_id: activation.id,
    command: {
      request_id: newId(),
      expected_activation_version: activation.version,
      invoice_ids: [...invoiceIds],
      scenario,
    },
  };
}
const base = (workspace: string) =>
  `/api/workspaces/${encodeURIComponent(workspace)}/email-ingress`;
export class IngressCommandError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "IngressCommandError";
  }
}
async function command<T>(
  path: string,
  body: object,
  csrf: string,
): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    cache: "no-store",
    credentials: "same-origin",
    headers: { "content-type": "application/json", "x-csrf-token": csrf },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail =
      typeof payload.detail === "object" && payload.detail
        ? payload.detail.code
        : payload.detail;
    const code = typeof payload.code === "string" ? payload.code : detail;
    const suffix =
      typeof code === "string" && /^[A-Z][A-Z0-9_]{1,80}$/.test(code)
        ? ` (${code})`
        : "";
    throw new IngressCommandError(
      (response.status === 401
        ? "Sign in again to review the current inbound setup."
        : response.status === 403 || response.status === 404
          ? "This synthetic inbound action is unavailable to your current membership or activation. Refresh setup and review it again."
          : response.status === 409
            ? "The reviewed setup or request changed. Refresh setup and explicitly review the current activation before trying again."
            : response.status === 422
              ? "Select one to three verified synthetic invoices from the same customer and accounting connection."
              : "The inbound request could not be confirmed. Refresh setup and inspect any existing receipt before retrying.") +
        suffix,
      response.status,
    );
  }
  return response.json();
}
export const ingressApi = {
  prepare: (
    workspace: string,
    body: { request_id: string; evidence: string },
    csrf: string,
  ) => command<IngressSetup>(`${base(workspace)}/setup`, body, csrf),
  setup: (workspace: string, signal?: AbortSignal) =>
    readApi<IngressSetup>(`${base(workspace)}/setup`, signal),
  activate: (
    workspace: string,
    body: {
      email_connection_id: string;
      storage_connection_id: string;
      evidence: string;
      expires_in_seconds: number;
    },
    csrf: string,
  ) => command<IngressActivation>(`${base(workspace)}/activations`, body, csrf),
  simulate: (
    workspace: string,
    activation: string,
    body: SimulateIngress,
    csrf: string,
  ) =>
    command<IngressReceipt>(
      `${base(workspace)}/activations/${encodeURIComponent(activation)}/simulate`,
      body,
      csrf,
    ),
  receipt: (workspace: string, id: string, signal?: AbortSignal) =>
    readApi<IngressReceipt>(
      `${base(workspace)}/receipts/${encodeURIComponent(id)}`,
      signal,
    ),
};
