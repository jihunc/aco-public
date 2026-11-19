import type { ProviderConnectionView } from "@/lib/generated-api";

export type CapturedDisconnect = Readonly<{ id: string; version: number }>;
export function captureDisconnect(row: Pick<ProviderConnectionView, "id" | "version">): CapturedDisconnect {
  return Object.freeze({ id: row.id, version: row.version });
}
const base = (workspace: string) => `/api/workspaces/${encodeURIComponent(workspace)}/provider-connections`;
async function request<T>(url: string, options: RequestInit): Promise<T> {
  const response = await fetch(url, { credentials: "same-origin", cache: "no-store", ...options });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : `Request failed (${response.status}).`);
  return result;
}
export function listConnections(workspace: string, signal?: AbortSignal): Promise<ProviderConnectionView[]> {
  return request(base(workspace), { method: "GET", signal });
}
export function disconnectConnection(workspace: string, captured: CapturedDisconnect, csrf: string): Promise<ProviderConnectionView> {
  return request(`${base(workspace)}/${encodeURIComponent(captured.id)}/disconnect`, {
    method: "POST", headers: { "content-type": "application/json", "x-csrf-token": csrf },
    body: JSON.stringify({ expected_version: captured.version }),
  });
}
