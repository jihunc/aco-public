"use client";
import { useId, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  CaseView,
  HandlingTimePage,
  HandlingTimeView,
  RecordHandlingTime,
  WorkspaceView,
} from "@/lib/generated-api";
import { readApi } from "@/lib/api";
import { stamp } from "@/features/receivables/shared";

export function HandlingTimePanel({
  workspace,
  record,
  csrfToken,
}: {
  workspace: WorkspaceView;
  record: CaseView;
  csrfToken: string;
}) {
  const cache = useQueryClient();
  const field = useId();
  const [history, setHistory] = useState("");
  const [minutes, setMinutes] = useState("");
  const [evidence, setEvidence] = useState("");
  const [pending, setPending] = useState<RecordHandlingTime | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const path = `/api/workspaces/${encodeURIComponent(workspace.id)}/cases/${encodeURIComponent(record.id)}/handling-time`;
  const query = useQuery({
    queryKey: ["case-handling", workspace.id, record.id, record.version],
    queryFn: ({ signal }) => readApi<HandlingTimePage>(path, signal),
    retry: false,
  });
  const data = query.data;
  const current = data?.entries.find(
    (entry) =>
      entry.history_id === history && data.current_entry_ids.includes(entry.id),
  );
  async function send(command: RecordHandlingTime) {
    if (busy) return;
    setPending(command);
    setBusy(true);
    setError("");
    try {
      const response = await fetch(path, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: {
          "content-type": "application/json",
          "x-csrf-token": csrfToken,
        },
        body: JSON.stringify(command),
      });
      if (!response.ok)
        throw new Error(
          response.status === 409
            ? "The case or report changed. Inspect current entries before preparing another report."
            : "The report was not confirmed. Inspect current entries; retry only this captured request.",
        );
      const saved = (await response.json()) as HandlingTimeView;
      if (saved.basis !== "MANUAL_REPORTED")
        throw new Error(
          "Unexpected reporting basis; refresh recorded entries.",
        );
      setPending(null);
      setMinutes("");
      setEvidence("");
      await query.refetch();
      await cache.invalidateQueries({
        queryKey: ["case-outcomes", workspace.id],
      });
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : "Report outcome unconfirmed. Inspect current entries before retrying.",
      );
      await query.refetch();
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="panel promise-controls"
      aria-label="Reported case handling time"
    >
      <h2>Reported handling time</h2>
      <p>
        MANUAL_REPORTED active minutes for your own recorded substantive work.
        This is not measured elapsed time or claimed time saved. Do not count
        the same work more than once.
      </p>
      {query.isPending ? (
        <p role="status">Loading reported handling entries…</p>
      ) : query.isError ? (
        <>
          <p role="alert">Handling history unavailable.</p>
          <button type="button" onClick={() => void query.refetch()}>
            Retry handling history
          </button>
        </>
      ) : (
        data && (
          <>
            <ul className="agent-record-list">
              {data.entries.map((entry) => (
                <li key={entry.id}>
                  {entry.minutes} reported active minutes · {entry.basis} ·{" "}
                  {data.current_entry_ids.includes(entry.id)
                    ? "Current"
                    : "Superseded"}
                  <p>{entry.evidence}</p>
                  <p className="muted">
                    Entry {entry.id} · Event {entry.history_id} ·{" "}
                    {stamp(entry.created_at)}
                    {entry.supersedes_id
                      ? ` · Corrects ${entry.supersedes_id}`
                      : ""}
                  </p>
                </li>
              ))}
            </ul>
            {!data.entries.length && (
              <p>
                No handling time has been reported; missing work duration
                remains unknown.
              </p>
            )}
            {workspace.permissions.includes("task_write") &&
              data.can_record_history_ids.length > 0 && (
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    if (pending) {
                      void send(pending);
                      return;
                    }
                    if (
                      !/^(?:0|[1-9]\d{0,3})(?:\.\d{1,2})?$/.test(minutes) ||
                      Number(minutes) > 1440
                    ) {
                      setError(
                        "Enter exact active minutes from 0 to 1440 with at most two decimal places.",
                      );
                      return;
                    }
                    void send({
                      request_id: crypto.randomUUID(),
                      history_id: history,
                      expected_case_version: data.expected_case_version,
                      minutes,
                      evidence,
                      supersedes_id: current?.id ?? null,
                    });
                  }}
                >
                  <label htmlFor={`${field}-event`}>Recorded work event</label>
                  <select
                    id={`${field}-event`}
                    required
                    value={history}
                    disabled={busy || !!pending}
                    onChange={(event) => {
                      setHistory(event.target.value);
                      setMinutes("");
                    }}
                  >
                    <option value="">Choose your own substantive event</option>
                    {data.can_record_history_ids.map((id) => (
                      <option key={id} value={id}>
                        {record.history
                          .find((item) => item.id === id)
                          ?.event.replaceAll("_", " ") ?? "Recorded work"}{" "}
                        · {id}
                      </option>
                    ))}
                  </select>
                  {current && (
                    <p>
                      Current report {current.id}: {current.minutes} minutes.
                      The next entry explicitly corrects it; its original
                      remains preserved.
                    </p>
                  )}
                  <label>
                    Reported active minutes
                    <input
                      required
                      inputMode="decimal"
                      value={minutes}
                      disabled={busy || !!pending}
                      onChange={(event) => setMinutes(event.target.value)}
                    />
                  </label>
                  <label>
                    Handling report evidence
                    <textarea
                      required
                      minLength={10}
                      maxLength={1000}
                      value={evidence}
                      disabled={busy || !!pending}
                      onChange={(event) => setEvidence(event.target.value)}
                    />
                  </label>
                  {error && <p role="alert">{error}</p>}
                  {pending && (
                    <p>
                      Captured request {pending.request_id} · Event{" "}
                      {pending.history_id} · {pending.minutes} minutes. Inspect
                      preserved entries before retrying an unconfirmed result.
                    </p>
                  )}
                  <button
                    type="submit"
                    disabled={busy || !csrfToken || !history}
                  >
                    {busy
                      ? "Recording report…"
                      : pending
                        ? "Retry captured handling report"
                        : "Record reported handling time"}
                  </button>
                  {pending && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        void query.refetch();
                        setPending(null);
                        setError("");
                      }}
                    >
                      Inspect current entries and discard captured request
                    </button>
                  )}
                </form>
              )}
          </>
        )
      )}
    </section>
  );
}
