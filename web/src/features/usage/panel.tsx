"use client";
import { useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { ErrorState } from "@/features/receivables/shared";
import { UsageDetails } from "./details";

export function UsagePanel({
  workspace,
  owner,
  csrfToken,
}: {
  workspace: string;
  owner: boolean;
  csrfToken: string;
}) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["usage", workspace],
    queryFn: ({ signal }) => api.usage(workspace, signal),
    retry: false,
  });
  const [currency, setCurrency] = useState("USD");
  const [amount, setAmount] = useState("0");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      const view = await api.draftLimit(
        workspace,
        { currency, amount, requests: 0 },
        csrfToken,
      );
      queryClient.setQueryData(["usage", workspace], view);
    } catch (error) {
      setError(
        error instanceof Error
          ? error.message
          : "Draft limit could not be saved.",
      );
    } finally {
      setSaving(false);
    }
  }
  if (query.isPending) return <p role="status">Loading usage and budgets…</p>;
  if (query.isError)
    return (
      <ErrorState
        label="Usage unavailable"
        error={query.error}
        retry={() => query.refetch()}
      />
    );
  return (
    <section className="panel" aria-label="Usage and budgets">
      <UsageDetails view={query.data} />
      <Button
        variant="outline"
        onClick={() => query.refetch()}
        disabled={query.isFetching}
      >
        Refresh usage
      </Button>
      {owner && (
        <form onSubmit={save}>
          <h3>Draft limit</h3>
          <p>
            Saving a draft does not activate spending. Real execution remains
            blocked; approval and provider activation are unavailable.
          </p>
          <label>
            Draft currency
            <input
              aria-label="Draft currency"
              value={currency}
              onChange={(e) => setCurrency(e.target.value)}
              pattern="[A-Z]{3}"
              required
            />
          </label>
          <label>
            Draft monetary cap
            <input
              aria-label="Draft monetary cap"
              inputMode="decimal"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              required
            />
          </label>
          <Button type="submit" disabled={saving}>
            {saving ? "Saving…" : "Save draft limit"}
          </Button>
          {error && <p role="alert">{error}</p>}
        </form>
      )}
    </section>
  );
}
