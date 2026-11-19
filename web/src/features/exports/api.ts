/** Fixed business projections; original bytes always use their separate private download. */
export const exportResources = [
  ["customers", "Customers"], ["invoices", "Invoices"], ["payments", "Verified payments"],
  ["accounting_events", "Accounting source events"], ["tasks", "Tasks"], ["cases", "Cases"],
  ["promises", "Payment promises"], ["actions", "Action history"], ["communications", "Communication metadata"],
  ["artifacts", "Private artifact metadata"], ["agent_runs", "Investigations and steps"],
  ["approvals", "Approvals and decisions"], ["voice_sessions", "Voice session metadata"],
  ["usage", "Usage and cost history"], ["policies", "Policies and versions"], ["audit_history", "Audit metadata"],
] as const;
export type ExportResource = typeof exportResources[number][0];
export type ExportCommand = { resources: ExportResource[]; created_from: string; created_before: string };
export async function downloadExport(workspace: string, command: ExportCommand, csrf: string) {
  const response = await fetch(`/api/workspaces/${encodeURIComponent(workspace)}/exports`, {
    method: "POST", credentials: "same-origin", cache: "no-store",
    headers: { "content-type": "application/json", "x-csrf-token": csrf }, body: JSON.stringify(command),
  });
  if (!response.ok) throw Error(response.status === 413
    ? "Narrow the selected resources or creation window. No file was produced."
    : response.status === 403 ? "Current Owner or Finance Manager export permission is required. Refresh your workspace."
      : response.status === 422 ? "Choose resources and a valid creation window of at most 366 days. No file was produced."
        : "Export was not confirmed. No file was downloaded; review the selection before trying again.");
  const sha256 = response.headers.get("x-content-sha256");
  if (!sha256 || !/^[a-f0-9]{64}$/.test(sha256)) throw Error("Export integrity reference is missing. No file was downloaded.");
  return { blob: await response.blob(), sha256 };
}
