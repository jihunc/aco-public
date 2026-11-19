"use client";
import Link from "next/link";
import { stamp } from "@/features/receivables/shared";
import { dueLabel, priorityLabel } from "@/lib/format";
import type { Task } from "./api";

export function CompletedTask({ task, workspace, canEdit, busy, onEdit, onDelete }: {
  task: Task; workspace: string; canEdit: boolean; busy: boolean;
  onEdit: (task: Task) => void; onDelete: (task: Task) => void;
}) {
  return <article className="panel review-task" data-testid="completed-task" aria-label={task.title}>
    <h3>{task.title}</h3>
    <p className="muted">{task.origin === "generated" ? "Generated from a source action" : "Human task"} · Done · {priorityLabel(task.priority)}</p>
    <p>Due: {dueLabel(task.due_at, stamp)}</p>
    {task.invoice_id && <p><Link href={`/receivables/${task.invoice_id}?workspace=${encodeURIComponent(workspace)}`}>View linked invoice</Link></p>}
    {task.notes && <p className="task-notes">{task.notes}</p>}
    {canEdit && <div className="review-task-actions">
      <button type="button" onClick={() => onEdit(task)} aria-label={`Edit ${task.title}`} disabled={busy}>Edit</button>
      <button type="button" onClick={() => onDelete(task)} aria-label={`Delete ${task.title}`} disabled={busy}>Delete</button>
    </div>}
  </article>;
}
