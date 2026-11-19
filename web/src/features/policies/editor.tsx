"use client";
import { useState, type FormEvent } from "react";
import { Dialog } from "radix-ui";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/features/receivables/shared";
import { readApi } from "@/lib/api";
import type {
  PolicyDefinition,
  PolicyVersion,
  PolicyPreview,
  RecipientView,
  RecipientSave,
  WorkspaceView,
} from "@/lib/generated-api";

function newDefinition(): PolicyDefinition {
  return {
    rules: [
      {
        id: crypto.randomUUID(),
        offset_days: 3,
        local_time: "09:00",
        channel: "email",
        template_id: "routine",
      },
    ],
    templates: [
      {
        id: "routine",
        subject: "Invoice reminder",
        body: "Please review the verified invoice balance.",
      },
    ],
    weekdays: [0, 1, 2, 3, 4],
    opens_at: "09:00",
    closes_at: "17:00",
    cap_24h: 1,
    cap_7d: 3,
    overrides: [],
    hold_disputes: true,
    hold_promises: true,
    ai_external_approval_required: true,
    autonomy: "internal_only",
    approval_threshold_minor: null,
    approval_currency: null,
    voice_enabled: false,
    voice_after_days: 30,
    voice_approval_required: true,
  };
}
async function mutate<T>(
  path: string,
  body: object,
  token: string,
  method = "POST",
): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: { "content-type": "application/json", "x-csrf-token": token },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const result = await response.json();
    throw new Error(
      typeof result.detail === "string"
        ? result.detail
        : "Check the policy fields and required constraints.",
    );
  }
  return response.json();
}
export function PolicyEditor({
  workspace,
  csrfToken,
}: {
  workspace: WorkspaceView;
  csrfToken: string;
}) {
  const cache = useQueryClient();
  const base = `/api/workspaces/${workspace.id}`;
  const policies = useQuery({
    queryKey: ["policies", workspace.id],
    queryFn: ({ signal }) =>
      readApi<PolicyVersion[]>(`${base}/policies`, signal),
    retry: false,
  });
  const recipients = useQuery({
    queryKey: ["policy-recipients", workspace.id],
    queryFn: ({ signal }) =>
      readApi<RecipientView[]>(`${base}/policy-recipients`, signal),
    retry: false,
  });
  const [saved, setSaved] = useState<PolicyVersion | null>(null);
  const [definition, setDefinition] = useState<PolicyDefinition | null>(null);
  const [name, setName] = useState("");
  const [reviewed, setReviewed] = useState(false);
  const [native, setNative] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [preview, setPreview] = useState<PolicyPreview | null>(null);
  const [history, setHistory] = useState<PolicyVersion[] | null>(null);
  const [archive, setArchive] = useState(false);
  const [replacement, setReplacement] = useState<PolicyVersion | null>(null);
  const [replacementReviewed, setReplacementReviewed] = useState(false);
  const [replacementNative, setReplacementNative] = useState(false);
  const [suspend, setSuspend] = useState(false);
  const owner = workspace.permissions.includes("settings");
  const editable = owner && !saved?.archived;
  function load(row: PolicyVersion) {
    setSaved(row);
    setName(row.name);
    setPreview(null);
    setHistory(null);
    setError("");
    setNotice("");
    setNative(false);
    setReviewed(false);
    setDefinition(
      Array.isArray(row.definition.rules)
        ? (row.definition as unknown as PolicyDefinition)
        : null,
    );
  }
  async function action(fn: () => Promise<void>) {
    setPending(true);
    setError("");
    setNotice("");
    try {
      await fn();
      await cache.invalidateQueries({ queryKey: ["policies", workspace.id] });
      await cache.invalidateQueries({ queryKey: ["me"] });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Policy operation failed.");
    } finally {
      setPending(false);
    }
  }
  async function save(activate: boolean) {
    if (!definition) return;
    await action(async () => {
      const row = await mutate<PolicyVersion>(
        `${base}/policies${saved ? `/${saved.policy_id}` : ""}`,
        {
          name,
          definition,
          expected_version: saved?.version ?? 0,
          activate,
          native_reminders_disabled: native,
          configuration_reviewed: reviewed,
        },
        csrfToken,
        saved ? "PUT" : "POST",
      );
      setSaved(row);
      setDefinition(row.definition as unknown as PolicyDefinition);
      setPreview(null);
      setHistory(null);
      setNotice(`Saved version ${row.version}`);
    });
  }
  if (policies.isPending) return <p role="status">Loading policies…</p>;
  if (policies.isError)
    return (
      <ErrorState
        label="Policies unavailable"
        error={policies.error}
        retry={() => policies.refetch()}
      />
    );
  return (
    <div className="space-y-6">
      <section className="panel space-y-3">
        <h2>Policy library</h2>
        <p>
          Each invoice keeps its own schedule. Contact limits are shared across
          the customer and recipient.
        </p>
        <p>
          Activating a reviewed policy schedules source checks. Every dispatch
          still requires current provider, source, recipient, limits and global
          guards, plus any required approval.
        </p>
        <p>
          {workspace.automation_enabled
            ? "Policy automation enabled"
            : "Policy automation off"}{" "}
          ·{" "}
          {workspace.outbound_paused
            ? "Tenant outbound paused"
            : "Tenant outbound resumed"}
        </p>
        <div className="flex flex-wrap gap-3">
          {workspace.permissions.includes("pause") && (
            <Button
              disabled={pending || workspace.outbound_paused}
              onClick={() =>
                action(async () => {
                  await mutate(
                    `${base}/automation/pause`,
                    { scope: "tenant", paused: true },
                    csrfToken,
                  );
                  setNotice("Tenant outbound paused");
                })
              }
            >
              Pause outbound
            </Button>
          )}
          {workspace.permissions.includes("resume") && (
            <Button
              variant="outline"
              disabled={pending || !workspace.outbound_paused}
              onClick={() =>
                action(async () => {
                  await mutate(
                    `${base}/automation/pause`,
                    { scope: "tenant", paused: false },
                    csrfToken,
                  );
                  setNotice(
                    "Tenant outbound resumed; global and dispatch guards still apply",
                  );
                })
              }
            >
              Resume outbound
            </Button>
          )}
          {owner && (
            <Button
              variant="outline"
              disabled={pending}
              onClick={() => {
                setSaved(null);
                setHistory(null);
                setName("New collection policy");
                setDefinition(newDefinition());
                setPreview(null);
                setNative(false);
                setReviewed(false);
                setNotice("");
                setError("");
              }}
            >
              New policy
            </Button>
          )}
        </div>
        <label className="block">
          Saved policy
          <select
            aria-label="Saved policy"
            value={saved?.policy_id ?? ""}
            onChange={(e) => {
              const row = policies.data.find(
                (p) => p.policy_id === e.target.value,
              );
              if (row) load(row);
            }}
          >
            <option value="">Choose a policy</option>
            {policies.data.map((row) => (
              <option key={row.policy_id} value={row.policy_id}>
                {row.name} · v{row.version}
                {row.archived
                  ? " · archived"
                  : row.active
                    ? " · active"
                    : " · draft"}
              </option>
            ))}
          </select>
        </label>
        {(pending || notice) && (
          <p role="status">{pending ? "Saving or loading policy…" : notice}</p>
        )}
        {error && !archive && (
          <p role="alert" aria-label="Policy error">
            {error}
          </p>
        )}
        {saved && !definition && (
          <p>
            This imported baseline version is retained for history. Create a
            policy to configure editable rules.
          </p>
        )}
      </section>
      {definition && (
        <section className="panel space-y-4">
          <form
            onSubmit={(e: FormEvent) => {
              e.preventDefault();
              void save(false);
            }}
            className="space-y-4"
          >
            <fieldset
              disabled={!editable || pending}
              className="space-y-4 min-w-0"
            >
              <legend>
                {saved
                  ? `Version ${saved.version} · ${new Date(saved.effective_at).toLocaleString()}`
                  : "New policy"}
              </legend>
              {saved?.active && (
                <p>
                  Saving a draft suspends this active policy. Activate a
                  reviewed version to continue scheduling.
                </p>
              )}
              <label className="block">
                Policy name
                <input
                  aria-label="Policy name"
                  value={name}
                  required
                  maxLength={120}
                  onChange={(e) => setName(e.target.value)}
                />
              </label>
              <h3>Invoice rules</h3>
              {definition.rules.map((rule, index) => (
                <fieldset
                  className="rounded border p-3 space-y-2 min-w-0"
                  key={rule.id}
                >
                  <legend>Rule {index + 1}</legend>
                  <label className="block">
                    Days from due date
                    <input
                      aria-label={`Days from due date ${index + 1}`}
                      type="number"
                      min={-365}
                      max={365}
                      required
                      value={rule.offset_days}
                      onChange={(e) =>
                        setDefinition({
                          ...definition,
                          rules: definition.rules.map((r, i) =>
                            i === index
                              ? { ...r, offset_days: Number(e.target.value) }
                              : r,
                          ),
                        })
                      }
                    />
                  </label>
                  <label className="block">
                    Recipient local time
                    <input
                      aria-label={`Local time ${index + 1}`}
                      type="time"
                      required
                      value={rule.local_time ?? "09:00"}
                      onChange={(e) =>
                        setDefinition({
                          ...definition,
                          rules: definition.rules.map((r, i) =>
                            i === index
                              ? { ...r, local_time: e.target.value }
                              : r,
                          ),
                        })
                      }
                    />
                  </label>
                  <label className="block">
                    Channel
                    <select
                      aria-label={`Channel ${index + 1}`}
                      value={rule.channel ?? "email"}
                      onChange={(e) =>
                        setDefinition({
                          ...definition,
                          rules: definition.rules.map((r, i) =>
                            i === index
                              ? {
                                  ...r,
                                  channel: e.target.value as "email" | "voice",
                                }
                              : r,
                          ),
                        })
                      }
                    >
                      <option value="email">Email</option>
                      <option value="voice">
                        Voice · permission unavailable
                      </option>
                    </select>
                  </label>
                  <label className="block">
                    Template
                    <select
                      aria-label={`Template ${index + 1}`}
                      value={rule.template_id ?? "routine"}
                      onChange={(e) =>
                        setDefinition({
                          ...definition,
                          rules: definition.rules.map((r, i) =>
                            i === index
                              ? { ...r, template_id: e.target.value }
                              : r,
                          ),
                        })
                      }
                    >
                      {definition.templates?.map((t) => (
                        <option key={t.id} value={t.id}>
                          {t.id}
                        </option>
                      ))}
                    </select>
                  </label>
                  <Button
                    type="button"
                    variant="outline"
                    disabled={definition.rules.length <= 1}
                    onClick={() =>
                      setDefinition({
                        ...definition,
                        rules: definition.rules.filter((_, i) => i !== index),
                      })
                    }
                  >
                    Remove rule {index + 1}
                  </Button>
                </fieldset>
              ))}
              <Button
                type="button"
                variant="outline"
                disabled={definition.rules.length >= 30}
                onClick={() =>
                  setDefinition({
                    ...definition,
                    rules: [
                      ...definition.rules,
                      {
                        id: crypto.randomUUID(),
                        offset_days: 10,
                        local_time: "09:00",
                        channel: "email",
                        template_id: definition.templates?.[0]?.id ?? "routine",
                      },
                    ],
                  })
                }
              >
                Add invoice rule
              </Button>
              <h3>Working window and contact limits</h3>
              <p>
                Recipient timezone is required. Windows do not establish holiday
                or legal permission.
              </p>
              <div className="flex flex-wrap gap-3">
                {["Mon", "Tue", "Wed", "Thu", "Fri"].map((day, i) => (
                  <label key={day}>
                    <input
                      type="checkbox"
                      checked={definition.weekdays?.includes(i) ?? false}
                      onChange={(e) =>
                        setDefinition({
                          ...definition,
                          weekdays: e.target.checked
                            ? [...(definition.weekdays ?? []), i].sort()
                            : (definition.weekdays ?? []).filter(
                                (d) => d !== i,
                              ),
                        })
                      }
                    />
                    {day}
                  </label>
                ))}
              </div>
              <label className="block">
                Window opens
                <input
                  aria-label="Window opens"
                  type="time"
                  min="09:00"
                  max="16:59"
                  value={definition.opens_at ?? "09:00"}
                  onChange={(e) =>
                    setDefinition({ ...definition, opens_at: e.target.value })
                  }
                />
              </label>
              <label className="block">
                Window closes
                <input
                  aria-label="Window closes"
                  type="time"
                  min="09:01"
                  max="17:00"
                  value={definition.closes_at ?? "17:00"}
                  onChange={(e) =>
                    setDefinition({ ...definition, closes_at: e.target.value })
                  }
                />
              </label>
              <label className="block">
                24-hour contact cap
                <input
                  aria-label="24-hour contact cap"
                  type="number"
                  min={0}
                  max={1}
                  value={definition.cap_24h ?? 1}
                  onChange={(e) =>
                    setDefinition({
                      ...definition,
                      cap_24h: Number(e.target.value),
                    })
                  }
                />
              </label>
              <label className="block">
                7-day contact cap
                <input
                  aria-label="7-day contact cap"
                  type="number"
                  min={0}
                  max={3}
                  value={definition.cap_7d ?? 3}
                  onChange={(e) =>
                    setDefinition({
                      ...definition,
                      cap_7d: Number(e.target.value),
                    })
                  }
                />
              </label>
              <h3>Templates</h3>
              {definition.templates?.map((template, index) => (
                <fieldset key={template.id} className="space-y-2 min-w-0">
                  <legend>{template.id}</legend>
                  <label className="block">
                    Subject
                    <input
                      aria-label={`Subject ${index + 1}`}
                      required
                      value={template.subject}
                      onChange={(e) =>
                        setDefinition({
                          ...definition,
                          templates: definition.templates?.map((t, i) =>
                            i === index ? { ...t, subject: e.target.value } : t,
                          ),
                        })
                      }
                    />
                  </label>
                  <label className="block">
                    Message
                    <textarea
                      className="w-full"
                      aria-label={`Message ${index + 1}`}
                      required
                      value={template.body}
                      onChange={(e) =>
                        setDefinition({
                          ...definition,
                          templates: definition.templates?.map((t, i) =>
                            i === index ? { ...t, body: e.target.value } : t,
                          ),
                        })
                      }
                    />
                  </label>
                </fieldset>
              ))}
              <Button
                type="button"
                variant="outline"
                onClick={() =>
                  setDefinition({
                    ...definition,
                    templates: [
                      ...(definition.templates ?? []),
                      {
                        id: `template-${crypto.randomUUID().slice(0, 8)}`,
                        subject: "Invoice reminder",
                        body: "Please review the verified invoice balance.",
                      },
                    ],
                  })
                }
              >
                Add template
              </Button>
              <h3>Review and voice conditions</h3>
              <label className="block">
                Autonomy
                <select
                  aria-label="Autonomy"
                  value={definition.autonomy ?? "internal_only"}
                  onChange={(e) =>
                    setDefinition({
                      ...definition,
                      autonomy: e.target.value as
                        "internal_only" | "draft_and_review",
                    })
                  }
                >
                  <option value="internal_only">Internal checks only</option>
                  <option value="draft_and_review">
                    Draft for human review
                  </option>
                </select>
              </label>
              <label className="block">
                Approval threshold in minor units
                <input
                  aria-label="Approval threshold in minor units"
                  inputMode="numeric"
                  pattern="[0-9]*"
                  value={definition.approval_threshold_minor ?? ""}
                  onChange={(e) =>
                    setDefinition({
                      ...definition,
                      approval_threshold_minor: e.target.value || null,
                    })
                  }
                />
              </label>
              <label className="block">
                Approval currency
                <input
                  aria-label="Approval currency"
                  pattern="[A-Z]{3}"
                  maxLength={3}
                  value={definition.approval_currency ?? ""}
                  onChange={(e) =>
                    setDefinition({
                      ...definition,
                      approval_currency: e.target.value || null,
                    })
                  }
                />
              </label>
              <label className="block">
                <input
                  type="checkbox"
                  checked={definition.voice_enabled ?? false}
                  onChange={(e) =>
                    setDefinition({
                      ...definition,
                      voice_enabled: e.target.checked,
                    })
                  }
                />
                Include voice in reviewed policy
              </label>
              <label className="block">
                Voice overdue days
                <input
                  aria-label="Voice overdue days"
                  type="number"
                  min={0}
                  max={365}
                  value={definition.voice_after_days ?? 30}
                  onChange={(e) =>
                    setDefinition({
                      ...definition,
                      voice_after_days: Number(e.target.value),
                    })
                  }
                />
              </label>
              <p>
                Dispute and promise holds, human approval for external AI
                actions and voice approval are mandatory. Recording,
                transcription and external execution remain disabled.
              </p>
              <h3>Customer overrides</h3>
              {definition.overrides?.map((override, index) => (
                <fieldset
                  key={override.customer_id}
                  className="space-y-2 min-w-0"
                >
                  <legend>Customer {override.customer_id}</legend>
                  <label className="block">
                    <input
                      type="checkbox"
                      checked={override.opt_out ?? false}
                      onChange={(e) =>
                        setDefinition({
                          ...definition,
                          overrides: definition.overrides?.map((o, i) =>
                            i === index
                              ? { ...o, opt_out: e.target.checked }
                              : o,
                          ),
                        })
                      }
                    />
                    Exclude customer
                  </label>
                  <label className="block">
                    Additional offset days
                    <input
                      aria-label={`Customer offset ${index + 1}`}
                      type="number"
                      min={-365}
                      max={365}
                      value={override.offset_adjustment_days ?? 0}
                      onChange={(e) =>
                        setDefinition({
                          ...definition,
                          overrides: definition.overrides?.map((o, i) =>
                            i === index
                              ? {
                                  ...o,
                                  offset_adjustment_days: Number(
                                    e.target.value,
                                  ),
                                }
                              : o,
                          ),
                        })
                      }
                    />
                  </label>
                  <label className="block">
                    Allowed customer channels
                    <select
                      aria-label={`Customer channels ${index + 1}`}
                      value={(override.allowed_channels ?? []).join(",")}
                      onChange={(e) =>
                        setDefinition({
                          ...definition,
                          overrides: definition.overrides?.map((o, i) =>
                            i === index
                              ? {
                                  ...o,
                                  allowed_channels: e.target.value
                                    .split(",")
                                    .filter(Boolean) as ("email" | "voice")[],
                                }
                              : o,
                          ),
                        })
                      }
                    >
                      <option value="">None</option>
                      <option value="email">Email</option>
                      <option value="voice">Voice</option>
                      <option value="email,voice">Email and voice</option>
                    </select>
                  </label>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() =>
                      setDefinition({
                        ...definition,
                        overrides: definition.overrides?.filter(
                          (_, i) => i !== index,
                        ),
                      })
                    }
                  >
                    Remove customer override {index + 1}
                  </Button>
                </fieldset>
              ))}
              <label className="block">
                Add customer override
                <select
                  aria-label="Add customer override"
                  value=""
                  onChange={(e) => {
                    if (e.target.value)
                      setDefinition({
                        ...definition,
                        overrides: [
                          ...(definition.overrides ?? []),
                          {
                            customer_id: e.target.value,
                            opt_out: false,
                            offset_adjustment_days: 0,
                            allowed_channels: ["email"],
                          },
                        ],
                      });
                  }}
                >
                  <option value="">Choose customer</option>
                  {recipients.data
                    ?.filter(
                      (r) =>
                        !definition.overrides?.some(
                          (o) => o.customer_id === r.customer_id,
                        ),
                    )
                    .map((r) => (
                      <option value={r.customer_id} key={r.customer_id}>
                        {r.destination ?? r.customer_id}
                      </option>
                    ))}
                </select>
              </label>
              <label className="block">
                <input
                  type="checkbox"
                  checked={native}
                  onChange={(e) => setNative(e.target.checked)}
                />
                I verified native accounting reminders are disabled
              </label>
              <label className="block">
                <input
                  type="checkbox"
                  checked={reviewed}
                  onChange={(e) => setReviewed(e.target.checked)}
                />
                I reviewed this policy configuration
              </label>
              <div className="flex flex-wrap gap-3">
                <Button type="submit">Save draft</Button>
                <Button type="button" onClick={() => save(true)}>
                  Activate reviewed policy
                </Button>
              </div>
            </fieldset>
          </form>
          {saved && (
            <div className="flex flex-wrap gap-3">
              <Button
                variant="outline"
                disabled={pending}
                onClick={() =>
                  action(async () => {
                    setPreview(
                      await readApi<PolicyPreview>(
                        `${base}/policies/${saved.policy_id}/preview`,
                      ),
                    );
                  })
                }
              >
                Preview saved policy
              </Button>
              <Button
                variant="outline"
                disabled={pending}
                onClick={() =>
                  action(async () => {
                    setHistory(
                      await readApi<PolicyVersion[]>(
                        `${base}/policies/${saved.policy_id}/versions`,
                      ),
                    );
                  })
                }
              >
                View version history
              </Button>
              {owner && saved.active && !saved.archived && (
                <Button
                  variant="outline"
                  disabled={pending}
                  onClick={() =>
                    action(async () => {
                      await mutate(
                        `${base}/policies/${saved.policy_id}/recompute`,
                        { expected_version: saved.version },
                        csrfToken,
                      );
                      setNotice("Future checks recomputed from current facts");
                    })
                  }
                >
                  Recompute future checks
                </Button>
              )}
              {owner && !saved.archived && (
                <Button
                  variant="outline"
                  disabled={pending}
                  onClick={() => {
                    setReplacement(null);
                    setReplacementReviewed(false);
                    setReplacementNative(false);
                    setSuspend(false);
                    setError("");
                    setArchive(true);
                  }}
                >
                  Archive policy
                </Button>
              )}
            </div>
          )}
        </section>
      )}
      {saved && (
        <Dialog.Root open={archive} onOpenChange={setArchive}>
          <Dialog.Portal>
            <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40" />
            <Dialog.Content className="fixed inset-x-4 top-[15vh] z-50 mx-auto max-w-xl rounded bg-white p-6 space-y-3">
              <Dialog.Title>Archive policy</Dialog.Title>
              {error && (
                <p role="alert" aria-label="Policy error">
                  {error}
                </p>
              )}
              <Dialog.Description>
                History is retained. Active policies need a reviewed replacement
                or automation suspension.
              </Dialog.Description>
              <label className="block">
                Replacement policy
                <select
                  aria-label="Replacement policy"
                  value={replacement?.policy_id ?? ""}
                  disabled={pending}
                  onChange={(e) => {
                    const chosen = policies.data.find(
                      (p) => p.policy_id === e.target.value,
                    );
                    setReplacement(chosen ? structuredClone(chosen) : null);
                    setReplacementReviewed(false);
                    setReplacementNative(false);
                  }}
                >
                  <option value="">None</option>
                  {policies.data
                    .filter(
                      (p) =>
                        p.policy_id !== saved.policy_id &&
                        !p.active &&
                        !p.archived,
                    )
                    .map((p) => (
                      <option key={p.policy_id} value={p.policy_id}>
                        {p.name} · v{p.version}
                      </option>
                    ))}
                </select>
              </label>
              <Button
                variant="outline"
                disabled={pending}
                onClick={() =>
                  action(async () => {
                    await policies.refetch();
                    setReplacement(null);
                    setReplacementReviewed(false);
                    setReplacementNative(false);
                  })
                }
              >
                Refresh replacement choices
              </Button>
              {replacement && (
                <div className="space-y-2">
                  <details>
                    <summary>
                      Review replacement version {replacement.version}
                    </summary>
                    <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all">
                      {JSON.stringify(replacement.definition, null, 2)}
                    </pre>
                  </details>
                  <p>
                    Review is bound to this selected version. A conflict
                    requires refreshing and reviewing again.
                  </p>
                  <label className="block">
                    <input
                      type="checkbox"
                      disabled={pending}
                      checked={replacementNative}
                      onChange={(e) => setReplacementNative(e.target.checked)}
                    />
                    Native accounting reminders are disabled for this
                    replacement
                  </label>
                  <label className="block">
                    <input
                      type="checkbox"
                      disabled={pending}
                      checked={replacementReviewed}
                      onChange={(e) => setReplacementReviewed(e.target.checked)}
                    />
                    I reviewed this replacement version
                  </label>
                </div>
              )}
              <label className="block">
                <input
                  type="checkbox"
                  checked={suspend}
                  onChange={(e) => setSuspend(e.target.checked)}
                />
                Suspend automation and cancel unsent actions
              </label>
              <Button
                disabled={pending}
                onClick={() =>
                  action(async () => {
                    await mutate(
                      `${base}/policies/${saved.policy_id}/archive`,
                      {
                        expected_version: saved.version,
                        replacement_policy_id: replacement?.policy_id ?? null,
                        replacement_expected_version:
                          replacement?.version ?? null,
                        suspend_automation: suspend,
                        native_reminders_disabled: replacementNative,
                        configuration_reviewed: replacementReviewed,
                      },
                      csrfToken,
                    );
                    setSaved({ ...saved, archived: true, active: false });
                    setArchive(false);
                    setNotice("Policy archived");
                  })
                }
              >
                Confirm archive
              </Button>
              <Button
                variant="outline"
                disabled={pending}
                onClick={() => setArchive(false)}
              >
                Cancel archive
              </Button>
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
      )}
      {history && (
        <section className="panel space-y-3">
          <h2>Version history</h2>
          {history.map((version) => (
            <details key={version.id} className="break-words">
              <summary>
                {version.name} · version {version.version} · {version.state} ·{" "}
                {new Date(version.effective_at).toLocaleString()}
              </summary>
              <pre className="whitespace-pre-wrap break-all">
                {JSON.stringify(version.definition, null, 2)}
              </pre>
            </details>
          ))}
        </section>
      )}
      {preview && (
        <section className="panel space-y-3">
          <h2>Schedule preview</h2>
          <p>
            Saved version only · {new Date(preview.as_of).toLocaleString()} · No
            outreach is submitted.
          </p>
          {preview.native_reminder_warning && (
            <p>
              Native accounting reminders have not been attested as disabled.
            </p>
          )}
          {preview.items.map((item) => (
            <article
              key={`${item.invoice_id}-${item.rule_id}`}
              className="rounded border p-3 break-words"
            >
              <h3>
                {item.invoice_number} · {item.channel}
              </h3>
              <p>
                {item.destination ?? "No recipient"} ·{" "}
                {item.recipient_timezone ?? "Timezone review required"}
              </p>
              <p>
                {item.next_at
                  ? new Date(item.next_at).toLocaleString("en-AU", {
                      timeZone: item.recipient_timezone ?? "UTC",
                      dateStyle: "medium",
                      timeStyle: "short",
                    })
                  : "No future occurrence"}{" "}
                · source {item.source_health}
              </p>
              <p>
                Holds:{" "}
                {item.holds === null
                  ? "Operational projection missing"
                  : item.holds.join(", ") || "None"}
              </p>
              <ul>
                {item.reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            </article>
          ))}
        </section>
      )}
      <section className="panel space-y-3">
        <h2>Recipient review</h2>
        <p>
          Imported contact details do not establish permission. Record your
          review and recipient timezone.
        </p>
        {recipients.isPending ? (
          <p role="status">Loading recipients…</p>
        ) : recipients.isError ? (
          <ErrorState
            label="Recipients unavailable"
            error={recipients.error}
            retry={() => recipients.refetch()}
          />
        ) : (
          recipients.data.map((recipient) => (
            <RecipientForm
              key={recipient.contact_id}
              recipient={recipient}
              disabled={!owner || pending}
              save={async (command) => {
                let result: RecipientView | undefined;
                await action(async () => {
                  result = await mutate<RecipientView>(
                    `${base}/policy-recipients/${recipient.contact_id}`,
                    command,
                    csrfToken,
                    "PUT",
                  );
                  await cache.invalidateQueries({
                    queryKey: ["policy-recipients", workspace.id],
                  });
                  setNotice("Recipient review saved");
                });
                return result;
              }}
              refresh={async () => {
                let result: RecipientView | undefined;
                await action(async () => {
                  const current = await readApi<RecipientView[]>(
                    `${base}/policy-recipients`,
                  );
                  result = current.find(
                    (row) => row.contact_id === recipient.contact_id,
                  );
                  if (!result) throw new Error("CONTACT_NOT_FOUND");
                  cache.setQueryData(
                    ["policy-recipients", workspace.id],
                    current,
                  );
                  setNotice(
                    "Recipient refreshed. Review the displayed destination and timezone again.",
                  );
                });
                return result;
              }}
            />
          ))
        )}
      </section>
    </div>
  );
}
function currentReview(recipient: RecipientView): boolean {
  return (
    recipient.review_matches_source &&
    (!recipient.owner_confirmed_tenant_timezone ||
      recipient.confirmed_tenant_timezone === recipient.tenant_timezone)
  );
}
function RecipientForm({
  recipient,
  disabled,
  save,
  refresh,
}: {
  recipient: RecipientView;
  disabled: boolean;
  save: (command: RecipientSave) => Promise<RecipientView | undefined>;
  refresh: () => Promise<RecipientView | undefined>;
}) {
  // Only explicit review refresh or this form's own successful save adopts a new
  // binding. A background query update cannot transfer local attestations.
  const [captured, setCaptured] = useState(recipient);
  const validInitialReview = currentReview(recipient);
  const [timezone, setTimezone] = useState(
    validInitialReview ? (recipient.timezone ?? "") : "",
  );
  const [fallback, setFallback] = useState(
    validInitialReview && recipient.owner_confirmed_tenant_timezone,
  );
  const [verified, setVerified] = useState(
    validInitialReview && recipient.destination_verified,
  );
  const [optOut, setOptOut] = useState(recipient.opt_out);
  const [emailAllowed, setEmailAllowed] = useState(
    validInitialReview && recipient.allowed_channels.includes("email"),
  );
  const [voiceAllowed, setVoiceAllowed] = useState(
    validInitialReview && recipient.allowed_channels.includes("voice"),
  );
  const [evidence, setEvidence] = useState(
    validInitialReview ? (recipient.evidence ?? "") : "",
  );
  const changed =
    recipient.version !== captured.version ||
    recipient.source_revision !== captured.source_revision ||
    recipient.destination !== captured.destination ||
    recipient.tenant_timezone !== captured.tenant_timezone;
  function adopt(row: RecipientView, freshReview: boolean) {
    setCaptured(row);
    const reuse = !freshReview && currentReview(row);
    setTimezone(reuse ? (row.timezone ?? "") : "");
    setFallback(reuse && row.owner_confirmed_tenant_timezone);
    setVerified(reuse && row.destination_verified);
    setEmailAllowed(reuse && row.allowed_channels.includes("email"));
    setVoiceAllowed(reuse && row.allowed_channels.includes("voice"));
    setOptOut(row.opt_out);
    setEvidence(reuse ? (row.evidence ?? "") : "");
  }
  return (
    <form
      data-recipient-id={recipient.contact_id}
      onSubmit={(e) => {
        e.preventDefault();
        void save({
          expected_version: captured.version,
          expected_contact_revision: captured.source_revision,
          expected_destination: captured.destination,
          expected_tenant_timezone: fallback ? captured.tenant_timezone : null,
          timezone: timezone || null,
          timezone_verified: !!timezone && !fallback,
          owner_confirmed_tenant_timezone: fallback,
          destination_verified: verified,
          allowed_channels: [
            ...(emailAllowed ? ["email" as const] : []),
            ...(voiceAllowed ? ["voice" as const] : []),
          ],
          opt_out: optOut,
          evidence,
        }).then((row) => {
          if (row) adopt(row, false);
        });
      }}
    >
      <fieldset
        disabled={disabled}
        className="rounded border p-3 space-y-2 min-w-0"
      >
        <legend className="break-all">
          {captured.destination ?? "Missing imported destination"}
        </legend>
        <p className="break-all">
          Reviewing destination:{" "}
          {captured.destination ?? "Missing imported destination"}
        </p>
        <p>Workspace timezone for this review: {captured.tenant_timezone}</p>
        <p className="break-all">
          Current imported destination:{" "}
          {recipient.destination ?? "Missing imported destination"}
        </p>
        <p>Current workspace timezone: {recipient.tenant_timezone}</p>
        {changed && (
          <p>
            Recipient details changed. This form retains your earlier review.
            Refresh and review again before saving.
          </p>
        )}
        {recipient.version > 0 && (
          <div className="break-all">
            <p>
              Saved review:{" "}
              {currentReview(recipient)
                ? "current"
                : "historical — fresh review required"}
            </p>
            <p>
              Saved reviewed destination:{" "}
              {recipient.reviewed_destination ?? "Missing destination"}
            </p>
            <p>
              Saved destination verification:{" "}
              {recipient.destination_verified ? "confirmed" : "not confirmed"}
            </p>
            <p>
              Saved recipient timezone: {recipient.timezone ?? "none"} ·{" "}
              {recipient.timezone_verified ? "verified" : "not verified"}
            </p>
            <p>
              Saved workspace timezone confirmation:{" "}
              {recipient.confirmed_tenant_timezone ?? "none"}
            </p>
            <p>Saved evidence: {recipient.evidence}</p>
          </div>
        )}
        <Button
          type="button"
          onClick={() => {
            void refresh().then((row) => {
              if (row) adopt(row, true);
            });
          }}
        >
          Refresh recipient for review
        </Button>
        <label className="block">
          Recipient timezone
          <input
            aria-label={`Timezone for ${captured.destination}`}
            value={timezone}
            placeholder="Australia/Melbourne"
            onChange={(e) => {
              setTimezone(e.target.value);
              if (e.target.value) setFallback(false);
            }}
          />
        </label>
        <label className="block">
          <input
            type="checkbox"
            checked={fallback}
            onChange={(e) => {
              setFallback(e.target.checked);
              if (e.target.checked) setTimezone("");
            }}
          />
          I confirm the workspace timezone applies to this recipient
        </label>
        <p>
          Confirmation applies only to {captured.tenant_timezone} as displayed
          for this review.
        </p>
        <label className="block">
          <input
            type="checkbox"
            checked={verified}
            onChange={(e) => setVerified(e.target.checked)}
          />
          Destination and email permission verified
        </label>
        <label className="block">
          <input
            type="checkbox"
            checked={emailAllowed}
            onChange={(e) => setEmailAllowed(e.target.checked)}
          />
          Allow email channel
        </label>
        <label className="block">
          <input
            type="checkbox"
            checked={voiceAllowed}
            onChange={(e) => setVoiceAllowed(e.target.checked)}
          />
          Allow voice channel · additional consent and approval required
        </label>
        <label className="block">
          <input
            type="checkbox"
            checked={optOut}
            onChange={(e) => setOptOut(e.target.checked)}
          />
          Recipient opted out
        </label>
        <label className="block">
          Review evidence
          <textarea
            aria-label={`Review evidence for ${captured.destination}`}
            className="w-full"
            required
            value={evidence}
            onChange={(e) => setEvidence(e.target.value)}
          />
        </label>
        <Button type="submit">Save recipient review</Button>
      </fieldset>
    </form>
  );
}
