"use client";
import Link from "next/link";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { InvoiceNumbersProvider } from "@/lib/invoice-numbers";
import { signOut } from "@/lib/sign-out";
import { ErrorState } from "./shared";
import { Overview } from "./overview";
import { Receivables } from "./list";
import { InvoiceDetail } from "./detail";
import { UsagePanel } from "@/features/usage/panel";
import { PolicyEditor } from "@/features/policies/editor";
import { ReviewQueuePanel } from "@/features/review/queue";
import { CommunicationTimeline } from "@/features/communications/timeline";
import { PromiseDetail } from "@/features/agent/promises";
import { CaseDetail } from "@/features/agent/cases";
import { SourceCommunication } from "@/features/agent/communication";
import { AgentTaskDetail } from "@/features/agent/task";
import { AgentPanel } from "@/features/agent/panel";
import { ImpactDashboard } from "@/features/impact/dashboard";
import type { ImpactWindow } from "@/features/impact/api";
import { EmailExecutionPanel } from "@/features/communications/settings";
import { VoicePanel } from "@/features/conversations/voice-session";
type Mode =
  "voice" | "agent-promise" | "agent-case" | "agent-communication" | "agent-task" | "agents" | "agent-run" | "policies" | "overview" | "receivables" | "invoice" | "customer" | "impact" | "review" | "conversations" | "email";
type Props = { mode: Mode; workspace?: string; id?: string; impactWindow?: ImpactWindow };
export function ProductWorkspace({ mode, workspace: requested, id, impactWindow }: Props) {
  const [signOutFailed, setSignOutFailed] = useState(false);
  const session = useQuery({
    queryKey: ["me"],
    queryFn: ({ signal }) => api.session(signal),
    retry: false,
  });
  const workspace =
    session.data?.workspaces.find((w) => w.id === requested) ??
    (!requested ? session.data?.workspaces[0] : undefined);
  const href = (path: string) =>
    `${path}?workspace=${encodeURIComponent(workspace?.id ?? requested ?? "")}`;
  const title =
    mode === "voice" ? "Voice sessions" :
    mode === "agent-promise" ? "Promise detail" :
    mode === "agent-case" ? "Case detail" :
    mode === "agent-communication" ? "Communication evidence" :
    mode === "agent-task" ? "Task detail" :
    mode === "agents" || mode === "agent-run" ? "AI investigations" :
    mode === "policies"
      ? "Collection policies"
      : mode === "review"
        ? "Review Queue"
      : mode === "conversations"
        ? "Conversations"
      : mode === "email"
        ? "Email execution"
      : mode === "invoice"
        ? "Invoice workspace"
        : mode === "customer"
          ? "Customer account"
          : mode === "impact"
            ? "Business Impact"
            : mode === "overview"
              ? "Overview"
              : "Receivables";
  return (
    <div className="product-shell">
      <a className="skip-link" href="#content">
        Skip to content
      </a>
      <aside className="product-sidebar">
        <Link className="product-brand" href={href("/overview")}>
          ACO<span>Collections operator</span>
        </Link>
        <nav aria-label="Main navigation">
          <Link
            aria-current={mode === "overview" ? "page" : undefined}
            href={href("/overview")}
          >
            Overview
          </Link>
          <Link
            aria-current={
              mode === "receivables" ||
              mode === "invoice" ||
              mode === "customer" ||
              mode === "agent-case" ||
              mode === "agent-promise"
                ? "page"
                : undefined
            }
            href={href("/receivables")}
          >
            Receivables
          </Link>
          <Link aria-current={mode === "conversations" || mode === "agent-communication" ? "page" : undefined} href={href("/conversations")}>Conversations</Link>
          <Link aria-current={mode === "agents" || mode === "agent-run" ? "page" : undefined} href={href("/agents")}>AI investigations</Link>
          <Link aria-current={mode === "review" || mode === "agent-task" ? "page" : undefined} href={href("/review")}>Review Queue</Link>
          <Link
            aria-current={mode === "impact" ? "page" : undefined}
            href={href("/business-impact")}
          >
            Business Impact
          </Link>
          <Link
            aria-current={mode === "policies" ? "page" : undefined}
            href={href("/settings/policies")}
          >
            Collection policies
          </Link>
          <Link aria-current={mode === "voice" ? "page" : undefined} href={href("/voice")}>Voice sessions</Link>
          <Link aria-current={mode === "email" ? "page" : undefined} href={href("/settings/email")}>Email execution</Link>
          <Link href="/onboarding">Settings &amp; setup</Link>
        </nav>
        {session.data && (
          <div className="sidebar-account">
            <span className="sidebar-account-email" title={session.data.email}>{session.data.email}</span>
            <button
              type="button"
              className="btn btn-ghost-inverse"
              onClick={async () => {
                setSignOutFailed(!(await signOut(session.data?.csrf_token ?? "")));
              }}
            >
              Sign out
            </button>
            {signOutFailed && (
              <p role="alert" className="sidebar-alert">Sign-out failed. Your session is still active — try again.</p>
            )}
          </div>
        )}
        <p className="sidebar-note">
          Source-backed finances.
          <br />
          Human-controlled collections.
        </p>
      </aside>
      <InvoiceNumbersProvider workspace={workspace?.id}>
      <div className="product-body">
        <header className="product-topbar">
          <span>Autonomous Collections Operator</span>
          <span className="badge">Accounting remains read-only</span>
        </header>
        <main id="content" tabIndex={-1}>
          <div className="page-heading">
            <div>
              <p className="eyebrow">Your collections workspace</p>
              <h1>{title}</h1>
            </div>
            {session.data && (
              <label className="workspace-picker">
                Workspace
                <select
                  aria-label="Workspace"
                  value={workspace?.id ?? ""}
                  onChange={(e) => {
                    window.location.assign(
                      `${window.location.pathname}?workspace=${encodeURIComponent(e.target.value)}`,
                    );
                  }}
                >
                  {!workspace && <option value="">Choose a workspace</option>}
                  {session.data.workspaces.map((w) => (
                    <option value={w.id} key={w.id}>
                      {w.name} · {w.role}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
          {session.isPending ? (
            <p role="status">Loading your workspace…</p>
          ) : session.isError ? (
            <ErrorState
              label="Workspace unavailable"
              error={session.error}
              retry={() => session.refetch()}
            />
          ) : !workspace ? (
            <section className="panel">
              <h2>Select or create a workspace</h2>
              <p>Your current membership does not provide this workspace.</p>
              <Link href="/onboarding">Open setup</Link>
            </section>
          ) : (
            <>
              <div className="safety-strip">
                <span>
                  {workspace.is_demo
                    ? "Synthetic assessment workspace"
                    : "Accounting-sourced workspace"}
                </span>
                <span>
                  {workspace.outbound_paused
                    ? "Outbound paused"
                    : "Outbound subject to current guards"}{" "}
                  · {workspace.timezone}
                </span>
              </div>
              {mode === "agent-promise" && id ? (
                <PromiseDetail workspace={workspace.id} id={id} currentWorkspace={workspace} csrfToken={session.data?.csrf_token ?? ""} />
              ) : mode === "agent-case" && id ? (
                <CaseDetail workspace={workspace.id} id={id} currentWorkspace={workspace} csrfToken={session.data?.csrf_token ?? ""} />
              ) : mode === "agent-communication" && id ? (
                <SourceCommunication workspace={workspace.id} id={id} />
              ) : mode === "agent-task" && id ? (
                <AgentTaskDetail workspace={workspace.id} id={id} />
              ) : mode === "agents" || mode === "agent-run" ? (
                <AgentPanel workspace={workspace} csrfToken={session.data?.csrf_token ?? ""} runId={mode === "agent-run" ? id : undefined} initialInvoice={mode === "agents" ? id : undefined} />
              ) : mode === "policies" ? (
                <PolicyEditor
                  key={workspace.id}
                  workspace={workspace}
                  csrfToken={session.data?.csrf_token ?? ""}
                />
              ) : mode === "review" ? (
                <ReviewQueuePanel workspace={workspace} csrfToken={session.data?.csrf_token ?? ""} approvalId={id} />
              ) : mode === "conversations" ? (
                <CommunicationTimeline workspace={workspace.id} />
              ) : mode === "voice" ? (
                <VoicePanel workspace={workspace} csrfToken={session.data?.csrf_token ?? ""} sessionId={id} />
              ) : mode === "email" ? (
                <EmailExecutionPanel workspace={workspace} csrfToken={session.data?.csrf_token ?? ""} receiptId={id} />
              ) : mode === "overview" ? (
                <>
                  <Overview workspace={workspace.id} href={href} />
                  <UsagePanel
                    workspace={workspace.id}
                    owner={workspace.role === "owner"}
                    csrfToken={session.data?.csrf_token ?? ""}
                  />
                </>
              ) : mode === "impact" ? (
                <ImpactDashboard key={`${workspace.id}:${impactWindow?.since}:${impactWindow?.until}`} workspace={workspace.id} initialWindow={impactWindow} csrfToken={session.data?.csrf_token ?? ""} />
              ) : mode === "invoice" && id ? (
                <InvoiceDetail workspace={workspace.id} id={id} href={href} currentWorkspace={workspace} csrfToken={session.data?.csrf_token ?? ""} />
              ) : (
                <>
                  <Receivables workspace={workspace.id} customerId={mode === "customer" ? id : undefined} href={href} />
                  {mode === "customer" && id && <CommunicationTimeline workspace={workspace.id} customerId={id} />}
                </>
              )}
            </>
          )}
        </main>
        <footer>
          ACO · Accounting is the source of financial truth. Read access never
          sends a reminder.
        </footer>
      </div>
      </InvoiceNumbersProvider>
    </div>
  );
}
