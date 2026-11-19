"use client";
import { createContext, useContext, useMemo, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { shortInvoiceLabel } from "@/lib/format";

// Workspace-wide invoice id -> number lookup so screens whose records only carry an
// invoice id still show the human invoice number. Without a provider (or before the
// lookup loads) labels fall back to the short reference.
const InvoiceNumbers = createContext<ReadonlyMap<string, string>>(new Map());

export function InvoiceNumbersProvider({ workspace, children }: { workspace?: string; children: ReactNode }) {
  const query = useQuery({
    queryKey: ["invoice-numbers", workspace],
    enabled: Boolean(workspace),
    staleTime: 60_000,
    queryFn: ({ signal }) => api.invoices(workspace as string, { page: 1, page_size: 100 }, signal),
  });
  const numbers = useMemo(
    () => new Map((query.data?.items ?? []).map((item) => [item.id, item.number] as const)),
    [query.data],
  );
  return <InvoiceNumbers.Provider value={numbers}>{children}</InvoiceNumbers.Provider>;
}

export function InvoiceRef({ id }: { id: string }) {
  const numbers = useContext(InvoiceNumbers);
  return <>{shortInvoiceLabel(id, numbers.get(id))}</>;
}
