import type {
  PageCustomerView,
  PageInvoiceView,
  WorkspaceView,
} from "@/lib/generated-api";
export type DemoImportReceipt = {
  provider_connection_id: string;
  synthetic: true;
};
export type DemoImportedRecords = {
  invoices: PageInvoiceView;
  customers: PageCustomerView;
};
export function canImportDemo(
  workspace: Pick<WorkspaceView, "role" | "is_demo" | "permissions">,
): boolean {
  return (
    workspace.role === "owner" &&
    workspace.is_demo &&
    workspace.permissions.includes("settings")
  );
}
const base = (workspace: string) =>
  `/api/workspaces/${encodeURIComponent(workspace)}`;
async function records<T extends PageInvoiceView | PageCustomerView>(
  path: string,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    signal,
  });
  if (!response.ok)
    throw new Error(
      response.status === 401
        ? "Sign in again to read imported source records."
        : response.status === 403 || response.status === 404
          ? "Imported records are unavailable to your current workspace membership."
          : "Current imported records could not be read. Reload them before assuming the import succeeded.",
    );
  const value = (await response.json()) as T;
  if (
    !Number.isSafeInteger(value.total) ||
    value.total < 0 ||
    typeof value.as_of_at !== "string"
  )
    throw new Error(
      "Imported source counts were unavailable. No count is inferred.",
    );
  return value;
}
export const demoApi = {
  async read(
    workspace: string,
    signal?: AbortSignal,
  ): Promise<DemoImportedRecords> {
    const [invoices, customers] = await Promise.all([
      records<PageInvoiceView>(
        `${base(workspace)}/receivables?page_size=1`,
        signal,
      ),
      records<PageCustomerView>(
        `${base(workspace)}/customers?page_size=1`,
        signal,
      ),
    ]);
    return { invoices, customers };
  },
  async import(
    workspace: WorkspaceView,
    csrf: string,
  ): Promise<DemoImportReceipt> {
    if (!canImportDemo(workspace))
      throw new Error(
        "The current Owner's demo settings permission is required to import synthetic source data.",
      );
    if (!csrf)
      throw new Error(
        "Sign in again before importing synthetic source data (CSRF session unavailable).",
      );
    const response = await fetch(`${base(workspace.id)}/demo/accounting/seed`, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "content-type": "application/json", "x-csrf-token": csrf },
      body: JSON.stringify({}),
    });
    if (!response.ok)
      throw new Error(
        response.status === 401
          ? "Sign in again before importing synthetic source data."
          : response.status === 403 || response.status === 404
            ? "Current Owner membership and a permitted demo workspace are required."
            : response.status === 503
              ? "The synthetic accounting provider is not configured in this environment."
              : response.status === 409
                ? "This workspace's demo source setup is unavailable. Refresh current records and workspace settings."
                : "The import outcome could not be confirmed. Reload current imported records before retrying; existing source data is preserved.",
      );
    const value = (await response.json()) as Record<string, unknown>;
    if (
      value.synthetic !== true ||
      typeof value.provider_connection_id !== "string"
    )
      throw new Error(
        "A synthetic source confirmation was not returned. Reload current imported records before assuming success.",
      );
    return {
      provider_connection_id: value.provider_connection_id,
      synthetic: true,
    };
  },
};
