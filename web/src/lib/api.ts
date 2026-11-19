import type {
  UsageSummary,
  DraftLimit,
  CustomerWorkspace,
  InvoiceWorkspace,
  OperationsView,
  PageCustomerView,
  PageInvoiceView,
  WorkspaceView,
  InvoicesApiWorkspacesWorkspaceIdReceivablesGetData,
} from "./generated-api";
export type SessionView = {
  email: string;
  csrf_token: string;
  workspaces: WorkspaceView[];
};
export type Filters = NonNullable<
  InvoicesApiWorkspacesWorkspaceIdReceivablesGetData["query"]
>;
export async function readApi<T>(
  path: string,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(path, {
    signal,
    cache: "no-store",
    credentials: "same-origin",
  });
  if (response.status === 401)
    throw new Error("Sign in required. Your session may have expired.");
  if (response.status === 403 || response.status === 404)
    throw new Error(
      "This workspace or record is unavailable to your current membership.",
    );
  if (response.status === 422)
    throw new Error(
      "Check filters: amount limits require a currency and a valid minimum/maximum.",
    );
  if (!response.ok)
    throw new Error("The service could not load current data. Try again.");
  return response.json();
}
export const api = {
  usage: (workspace: string, signal?: AbortSignal) =>
    readApi<UsageSummary>(`/api/workspaces/${encodeURIComponent(workspace)}/usage`,signal),
  draftLimit: async (workspace: string, command: DraftLimit, csrfToken: string): Promise<UsageSummary> => {
    const response = await fetch(`/api/workspaces/${encodeURIComponent(workspace)}/usage/draft-limit`, {
      method:'PUT',credentials:'same-origin',cache:'no-store',
      headers:{'content-type':'application/json','x-csrf-token':csrfToken},body:JSON.stringify(command),
    });
    if (!response.ok) throw new Error(response.status === 403 ? 'Owner settings permission is required.' : 'Enter an ISO currency and an exact nonnegative decimal cap.');
    return response.json();
  },
  session: (signal?: AbortSignal) => readApi<SessionView>("/api/me", signal),
  operations: (workspace: string, signal?: AbortSignal) =>
    readApi<OperationsView>(
      `/api/workspaces/${encodeURIComponent(workspace)}/overview`,
      signal,
    ),
  invoices: (workspace: string, filters: Filters, signal?: AbortSignal) => {
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "")
        params.set(key, String(value));
    });
    return readApi<PageInvoiceView>(
      `/api/workspaces/${encodeURIComponent(workspace)}/receivables?${params}`,
      signal,
    );
  },
  invoice: (workspace: string, id: string, signal?: AbortSignal) =>
    readApi<InvoiceWorkspace>(
      `/api/workspaces/${encodeURIComponent(workspace)}/receivables/${encodeURIComponent(id)}`,
      signal,
    ),
  customers: (workspace: string, search: string, signal?: AbortSignal) =>
    readApi<PageCustomerView>(
      `/api/workspaces/${encodeURIComponent(workspace)}/customers?search=${encodeURIComponent(search)}&page_size=100`,
      signal,
    ),
  customer: (workspace: string, id: string, signal?: AbortSignal) =>
    readApi<CustomerWorkspace>(
      `/api/workspaces/${encodeURIComponent(workspace)}/customers/${encodeURIComponent(id)}`,
      signal,
    ),
};
