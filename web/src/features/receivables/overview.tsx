"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatMoney } from "@/lib/money";
import { BarChart, Bar, XAxis, YAxis, ResponsiveContainer } from "recharts";
import type { OperationsView, MoneyJson } from "@/lib/generated-api";
import { stamp, ErrorState } from "./shared";
import { WorkloadSummary } from "./workload";
export function Overview({
  workspace,
  href,
}: {
  workspace: string;
  href: (path: string) => string;
}) {
  const query = useQuery({
    queryKey: ["operations", workspace],
    queryFn: ({ signal }) => api.operations(workspace, signal),
    retry: false,
  });
  if (query.isPending)
    return <p role="status">Loading source-backed overview…</p>;
  if (query.isError)
    return (
      <ErrorState
        label="Overview unavailable"
        error={query.error}
        retry={() => query.refetch()}
      />
    );
  const view = query.data;
  return (
    <>
      <div className="section-heading">
        <p>Current financial position · as of {stamp(view.as_of_at)}</p>
        <Button
          variant="outline"
          disabled={query.isFetching}
          onClick={() => query.refetch()}
        >
          Refresh data
        </Button>
      </div>
      <p className="muted">
        {view.basis} Due today is current. Stale balances are last-known, not a
        fresh source verification.
      </p>
      {view.availability === "unavailable" ? (
        <section className="panel">
          <h2>No imported accounting data</h2>
          <p>
            Financial totals are unavailable. No invoices have been imported;
            accounting setup is required.
          </p>
        </section>
      ) : (
        view.currencies.map((group) => (
          <CurrencyOverview key={group.currency} group={group} />
        ))
      )}
      {view.currencies.some(group => group.provenance === "synthetic" || group.provenance === "mixed") && <section className="panel" aria-label="Demo source payment walkthrough">
        <h2>Demonstrate a source payment</h2>
        <p>Open a verified synthetic invoice to record a payment in demo accounting. Return here to inspect the persisted source-backed balances and aging.</p>
        <Link href={href("/receivables")}>Choose an invoice for a demo source payment</Link>
      </section>}
      <WorkloadSummary key={workspace} workspace={workspace} href={href} />
      <div className="panel-grid">
        <section className="panel"><h2>Needs Your Attention</h2>
          <p>Review saved tasks and human approval requests in this workspace.</p>
          <Link href={href("/review")}>Open Review Queue</Link>
        </section>
        <section className="panel"><h2>AI investigations</h2>
          <p>Inspect recent investigations, source evidence and committed outcomes. Each run identifies its simulated or real model.</p>
          <Link href={href("/agents")}>View investigation history</Link>
        </section>
      </div>
      <section className="panel impact-entry">
        <div>
          <h2>Measure the outcome</h2>
          <p>
            Explore source-backed cohorts, recorded case outcomes and clearly labeled cost assumptions.
          </p>
        </div>
        <Link href={href("/business-impact")}>View Business Impact →</Link>
      </section>
    </>
  );
}
export function CurrencyOverview({
  group,
}: {
  group: OperationsView["currencies"][number];
}) {
  const currency = group.currency;
  if (!group.outstanding || !group.overdue || !group.aging)
    return (
      <section className="panel">
        <h2>{currency} receivables</h2>
        <p className="notice">
          Unavailable — no supported financial balance in this currency. Unknown
          source amounts are not zero AR.
        </p>
        <p>
          {group.provenance} · {group.completeness} · {group.freshness} · oldest
          source read {stamp(group.last_synced_at)}
        </p>
        <p>
          {group.excluded_count} excluded source invoices. Signed retained
          source balance: {formatMoney(group.excluded_balance)}.
        </p>
      </section>
    );

  const bins = [
    ["Current", group.aging.current],
    ["1–30 days", group.aging.days_1_30],
    ["31–60 days", group.aging.days_31_60],
    ["61–90 days", group.aging.days_61_90],
    ["91+ days", group.aging.days_91_plus],
  ];
  const total = BigInt(group.aging.total);
  const money = (minor_units: string): MoneyJson => ({
    currency: group.currency,
    exponent: group.exponent,
    minor_units,
  });
  return (
    <section
      className="currency-section"
      aria-label={`${currency} financial position`}
    >
      <div className="section-heading">
        <h2>{currency} receivables</h2>
        <span className="badge">
          {group.provenance === "synthetic"
            ? "Synthetic — Accounting-sourced"
            : group.provenance === "observed"
              ? "Observed — Accounting-sourced"
              : "Mixed synthetic and observed — inspect source rows"}
        </span>
      </div>
      <p>
        <span className={`badge freshness-${group.freshness}`}>
          {group.freshness} · {group.completeness}
        </span>{" "}
        Oldest source read: {stamp(group.last_synced_at)}
      </p>
      {group.completeness !== "complete" && (
        <p className="notice">
          {group.completeness === "incomplete"
            ? "Incomplete — unknown or unavailable verification. Displayed amounts cover only the supported basis."
            : "Provisional — last-known verified balances; source data is stale."}
        </p>
      )}
      <div className="metric-grid">
        <section className="metric-card">
          <p>Outstanding AR</p>
          <strong data-testid={`outstanding-${currency}`}>
            {formatMoney(group.outstanding)}
          </strong>
          <small>{group.included_count} positive open / partial invoices</small>
        </section>
        <section className="metric-card">
          <p>Overdue AR</p>
          <strong data-testid={`overdue-${currency}`}>
            {formatMoney(group.overdue)}
          </strong>
          <small>
            {group.overdue_ratio === null
              ? "Ratio: Insufficient data"
              : `${(Number(group.overdue_ratio) * 100).toFixed(1)}% of included outstanding`}
          </small>
        </section>
      </div>
      <section className="panel">
        <h3>Aging of outstanding AR</h3>
        <p className="muted">
          Tenant-calendar days past due · {currency} · same basis as outstanding
        </p>
        <div
          aria-hidden="true"
          style={{ height: 200, width: "100%", minWidth: 0 }}
        >
          <ResponsiveContainer
            width="100%"
            height="100%"
            minWidth={0}
            initialDimension={{ width: 300, height: 200 }}
          >
            <BarChart
              layout="vertical"
              data={bins.map(([name, value]) => ({
                name,
                share:
                  total > 0n
                    ? Number((BigInt(value) * 10000n) / total) / 100
                    : 0,
              }))}
              margin={{ left: 0, right: 12 }}
            >
              <XAxis
                type="number"
                domain={[0, 100]}
                tickFormatter={(v) => `${v}%`}
                tick={{ fontSize: 11 }}
              />
              <YAxis
                type="category"
                dataKey="name"
                width={90}
                tick={{ fontSize: 11 }}
              />
              <Bar dataKey="share" fill="#267f84" isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
        <table className="aging-table">
          <caption className="sr-only">
            Exact aging balances in {currency}
          </caption>
          <tbody>
            {bins.map(([name, value]) => (
              <tr key={name}>
                <th scope="row">{name}</th>
                <td>{formatMoney(money(value))}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p data-testid={`aging-total-${currency}`}>
          Aging total: {formatMoney(group.outstanding)}
        </p>
        <details>
          <summary>Basis and excluded source facts</summary>
          <p>
            {group.invoice_count} source invoices; {group.excluded_count}{" "}
            excluded from positive AR. Signed excluded balance:{" "}
            {formatMoney(group.excluded_balance)}. Credits, voids and unknowns
            are not cash.
          </p>
          <ul>
            {Object.entries(group.excluded_by_reason).map(([reason, count]) => (
              <li key={reason}>
                {reason.replaceAll("_", " ")}: {count}
              </li>
            ))}
          </ul>
        </details>
      </section>
    </section>
  );
}
