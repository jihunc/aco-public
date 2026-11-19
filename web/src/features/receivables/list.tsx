"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatMoney } from "@/lib/money";
import { useState } from "react";
import type { Filters } from "@/lib/api";
import { stamp, ErrorState, SourceBadges } from "./shared";
import { CustomerContactActivity, NextSavedAction } from "./customer-contact";
export function Receivables({
  workspace,
  customerId,
  href,
}: {
  workspace: string;
  customerId?: string;
  href: (path: string) => string;
}) {
  const [filters, setFilters] = useState<Filters>({ page: 1, page_size: 20 });
  const [customerSearch, setCustomerSearch] = useState("");
  const effective = {
    ...filters,
    customer_id: customerId ?? filters.customer_id,
  };
  const query = useQuery({
    queryKey: ["receivables", workspace, effective],
    queryFn: ({ signal }) => api.invoices(workspace, effective, signal),
    retry: false,
  });
  const customers = useQuery({
    queryKey: ["customers", workspace, customerSearch],
    queryFn: ({ signal }) => api.customers(workspace, customerSearch, signal),
    retry: false,
  });
  const customer = useQuery({
    queryKey: ["customer", workspace, customerId],
    queryFn: ({ signal }) => api.customer(workspace, customerId!, signal),
    enabled: !!customerId,
    retry: false,
  });
  return (
    <>
      {customerId && (
        <section className="panel">
          {customer.isPending ? (
            <p role="status">Loading customer…</p>
          ) : customer.isError ? (
            <ErrorState
              label="Customer unavailable"
              error={customer.error}
              retry={() => customer.refetch()}
            />
          ) : (
            <>
              <h2>{customer.data.customer.name}</h2>
              <p>
                {customer.data.customer.synthetic
                  ? "Synthetic imported customer"
                  : "Observed imported customer"}{" "}
                · {customer.data.contact_email ?? "No imported contact"}
              </p>
              <p>
                {customer.data.verified_for_outreach
                  ? "Outreach contact independently verified"
                  : "Imported contact is not verified outreach authority."}
              </p>
              <p className="muted">
                Customer source observation:{" "}
                {stamp(customer.data.customer.source.observed_at)} · revision{" "}
                {customer.data.customer.source.revision}. Contact data does not
                confer outreach permission.
              </p>
              <CustomerContactActivity interactions={customer.data.interactions} frequency={customer.data.contact_frequency} />
            </>
          )}
        </section>
      )}
      <form
        className="panel filter-panel"
        onSubmit={(e) => {
          e.preventDefault();
          const f = new FormData(e.currentTarget);
          const values = Object.fromEntries(f.entries());
          setFilters({
            page: 1,
            page_size: 20,
            search: String(values.search),
            customer_id: String(values.customer_id ?? "") || undefined,
            currency: String(values.currency) || undefined,
            financial_status: String(values.financial_status) || undefined,
            collection_state: String(values.collection_state) || undefined,
            overdue:
              values.overdue === "" ? undefined : values.overdue === "true",
            stale: values.stale === "" ? undefined : values.stale === "true",
            min_minor: String(values.min_minor) || undefined,
            max_minor: String(values.max_minor) || undefined,
          } as Filters);
        }}
      >
        <div className="filter-grid">
          <label>
            Search invoices or customers
            <input
              name="search"
              type="search"
              placeholder="Invoice number or customer name"
            />
          </label>
          <label>
            Find customer
            <input
              type="search"
              value={customerSearch}
              onChange={(e) => setCustomerSearch(e.target.value)}
              placeholder="Search customer names"
            />
          </label>
          <label>
            Customer
            <select
              name="customer_id"
              aria-label="Customer"
              disabled={!!customerId || customers.isPending}
            >
              <option value="">
                {customerId ? "This customer account" : "All customers"}
              </option>
              {customers.data?.items.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Financial status
            <select name="financial_status" aria-label="Financial status">
              <option value="">All financial states</option>
              {[
                "open",
                "partially_paid",
                "paid",
                "voided",
                "credited",
                "written_off",
                "unknown",
              ].map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
          </label>
          <label>
            Collection state
            <select name="collection_state" aria-label="Collection state">
              <option value="">All collection states</option>
              {["active", "closed", "held"].map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
          </label>
          <label>
            Overdue
            <select name="overdue" aria-label="Overdue">
              <option value="">All due dates</option>
              <option value="true">Overdue</option>
              <option value="false">Not overdue</option>
            </select>
          </label>
          <label>
            Source freshness
            <select name="stale" aria-label="Source freshness">
              <option value="">All source reads</option>
              <option value="true">Stale or unknown</option>
              <option value="false">Fresh</option>
            </select>
          </label>
          <label>
            Currency
            <input
              name="currency"
              placeholder="AUD"
              pattern="[A-Z]{3}"
              maxLength={3}
            />
          </label>
          <label>
            Minimum balance (minor units)
            <input
              name="min_minor"
              inputMode="numeric"
              pattern="-?[0-9]{1,30}"
              placeholder="e.g. 10000 = AUD 100.00"
            />
          </label>
          <label>
            Maximum balance (minor units)
            <input
              name="max_minor"
              inputMode="numeric"
              pattern="-?[0-9]{1,30}"
            />
          </label>
        </div>
        {customers.data &&
          customers.data.total > customers.data.items.length && (
            <p className="muted">
              Showing the first 100 matching customers. Narrow the customer
              search to find more.
            </p>
          )}
        {customers.isError && (
          <p role="alert">
            Customer search unavailable. Invoice search remains available.
          </p>
        )}
        <div className="form-actions">
          <Button type="submit" disabled={query.isFetching}>
            Apply filters
          </Button>
          <Button
            variant="outline"
            type="reset"
            onClick={() => {
              setFilters({ page: 1, page_size: 20 });
              setCustomerSearch("");
            }}
          >
            Clear filters
          </Button>
        </div>
      </form>
      {query.isPending ? (
        <p role="status">Loading receivables…</p>
      ) : query.isError ? (
        <ErrorState
          label="Receivables unavailable"
          error={query.error}
          retry={() => query.refetch()}
        />
      ) : (
        <>
          <div className="section-heading">
            <p><span>{query.data.total} invoices</span> · <Link href={href("/agents")}>Investigate invoices</Link></p>
            <p className="muted">
              As of {stamp(query.data.as_of_at)} · read-only source snapshots
            </p>
          </div>
          {query.data.items.length === 0 ? (
            <section className="panel">
              <p>No invoices match these filters.</p>
            </section>
          ) : (
            <div className="invoice-list">
              {query.data.items.map((invoice) => (
                <article className="invoice-row" key={invoice.id}>
                  <div>
                    <Link
                      className="invoice-number"
                      href={href(`/receivables/${invoice.id}`)}
                    >
                      {invoice.number}
                    </Link>
                    <Link
                      className="customer-link"
                      href={href(`/customers/${invoice.customer.id}`)}
                    >
                      {invoice.customer.name}
                    </Link>
                    <p>
                      Due {invoice.due_date}
                      {invoice.overdue ? " · Overdue" : ""}
                    </p>
                  </div>
                  <div>
                    <SourceBadges invoice={invoice} />
                    <p className="muted">
                      {invoice.source.synthetic ? "Synthetic" : "Observed"} ·{" "}
                      {invoice.accounting_provider} · source read{" "}
                      {stamp(invoice.last_synced_at)}
                    </p>
                  </div>
                  <div className="invoice-amount">
                    <strong>{formatMoney(invoice.outstanding)}</strong>
                    <p>
                      {invoice.verified
                        ? "Last-known source-verified snapshot"
                        : "Verification unknown — retained source balance"}
                    </p>
                    <NextSavedAction action={invoice.next_action} />
                  </div>
                </article>
              ))}
            </div>
          )}
          <nav className="pagination" aria-label="Receivables pages">
            <Button
              variant="outline"
              disabled={query.data.page <= 1 || query.isFetching}
              onClick={() =>
                setFilters({ ...filters, page: (filters.page ?? 1) - 1 })
              }
            >
              Previous page
            </Button>
            <span>
              Page {query.data.page} of{" "}
              {Math.max(1, Math.ceil(query.data.total / query.data.page_size))}
            </span>
            <Button
              variant="outline"
              disabled={
                query.data.page * query.data.page_size >= query.data.total ||
                query.isFetching
              }
              onClick={() =>
                setFilters({ ...filters, page: (filters.page ?? 1) + 1 })
              }
            >
              Next page
            </Button>
          </nav>
        </>
      )}
    </>
  );
}
