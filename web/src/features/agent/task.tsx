"use client";
import Link from "next/link";
import { dueLabel, priorityLabel } from "@/lib/format";
import { useQuery } from "@tanstack/react-query";
import { readApi } from "@/lib/api";
import type { Task } from "@/features/review/api";
import { ErrorState, stamp } from "@/features/receivables/shared";
export function AgentTaskDetail({
  workspace,
  id,
}: {
  workspace: string;
  id: string;
}) {
  const task = useQuery({
    queryKey: ["task", workspace, id],
    queryFn: ({ signal }) =>
      readApi<Task>(
        `/api/workspaces/${encodeURIComponent(workspace)}/tasks/${encodeURIComponent(id)}`,
        signal,
      ),
    retry: false,
  });
  return task.isPending ? (
    <p role="status">Loading committed task…</p>
  ) : task.isError ? (
    <ErrorState
      label="Task unavailable"
      error={task.error}
      retry={() => task.refetch()}
    />
  ) : (
    <TaskRecord task={task.data} workspace={workspace} />
  );
}
export function TaskRecord({
  task,
  workspace,
}: {
  task: Task;
  workspace: string;
}) {
  return (
    <section className="panel">
      <p>
        <Link href={`/review?workspace=${encodeURIComponent(workspace)}`}>
          ← Review Queue
        </Link>
      </p>
      <div className="section-heading">
        <h2>{task.title}</h2>
        <span className="badge">
          {task.deleted_at
            ? "Deleted · history retained"
            : task.status.replaceAll("_", " ")}
        </span>
      </div>
      <p>
        {task.origin === "generated" ? "Generated" : "Human"} task · Version{" "}
        {task.version}
      </p>
      <p>Task reference: {task.id}</p>
      <dl className="detail-grid">
        <div>
          <dt>Priority</dt>
          <dd>{priorityLabel(task.priority)}</dd>
        </div>
        <div>
          <dt>Due</dt>
          <dd>{dueLabel(task.due_at, stamp)}</dd>
        </div>
        <div>
          <dt>Assigned member</dt>
          <dd>{task.assignee_id ?? "Unassigned"}</dd>
        </div>
      </dl>
      {task.notes && <p className="task-notes">{task.notes}</p>}
      {task.invoice_id && (
        <p>
          <Link
            href={`/receivables/${encodeURIComponent(task.invoice_id)}?workspace=${encodeURIComponent(workspace)}`}
          >
            Open linked invoice
          </Link>
        </p>
      )}
      {task.source_action_id && <p>Source action: {task.source_action_id}</p>}
      <p className="muted">
        This is the persisted task record. Manage permitted changes in the
        Review Queue. Task status does not change financial status.
      </p>
    </section>
  );
}
