"use client";
/* dnd-kit returns callback refs and listeners for JSX; this lint rule mistakes them for mutable ref reads. */
/* eslint-disable react-hooks/refs */
import { useState } from "react";
import { dueLabel, priorityLabel } from "@/lib/format";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { DndContext, KeyboardSensor, PointerSensor, closestCenter, useSensor, useSensors, type DragEndEvent } from "@dnd-kit/core";
import { SortableContext, useSortable, sortableKeyboardCoordinates, verticalListSortingStrategy, arrayMove } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { ErrorState, stamp } from "@/features/receivables/shared";
import type { WorkspaceView } from "@/lib/generated-api";
import { reviewApi, reviewMutate, reviewPath, type Task } from "./api";
import { TaskForm, type TaskInput } from "./task-form";
import { CompletedTask } from "./completed-task";
import { ApprovalsPanel } from "./approvals";

function SortableTask({ task, position, count, canEdit, busy, onMove, onEdit, onDelete, workspace }: {
  task: Task; position: number; count: number; canEdit: boolean; busy: boolean;
  onMove: (id: string, step: number) => void; onEdit: (task: Task) => void;
  onDelete: (task: Task) => void; workspace: string;
}) {
  const sortable = useSortable({ id: task.id, disabled: busy || !canEdit || task.deadline_locked });
  return <article ref={sortable.setNodeRef} style={{ transform: CSS.Transform.toString(sortable.transform), transition: sortable.transition }}
    className="panel review-task" data-testid="review-task" aria-label={task.title}>
    <div className="review-task-heading"><div><h3 data-testid="review-task-title">{task.title}</h3>
      <p className="muted">{task.origin === "generated" ? "Generated from a source action" : "Human task"} · {task.status.replace("_", " ")} · {priorityLabel(task.priority)}</p>
    </div><div className="review-order-actions">
      <button type="button" aria-label={`Drag ${task.title}`} ref={sortable.setActivatorNodeRef} {...sortable.attributes} {...sortable.listeners}
        disabled={!canEdit || task.deadline_locked || busy}>⋮⋮</button>
      <button type="button" aria-label={`Move ${task.title} up`} onClick={() => onMove(task.id, -1)}
        disabled={!canEdit || task.deadline_locked || position === 0 || busy}>↑</button>
      <button type="button" aria-label={`Move ${task.title} down`} onClick={() => onMove(task.id, 1)}
        disabled={!canEdit || task.deadline_locked || position === count - 1 || busy}>↓</button>
    </div></div>
    <p>Due: {dueLabel(task.due_at, stamp)} {task.deadline_locked && <strong> · Due deadline: fixed ahead of manual order</strong>}</p>
    {task.invoice_id && <p><Link href={`/receivables/${task.invoice_id}?workspace=${encodeURIComponent(workspace)}`}>View linked invoice</Link></p>}
    {task.notes && <p className="task-notes">{task.notes}</p>}
    {canEdit && <div className="review-task-actions"><button type="button" onClick={() => onEdit(task)} aria-label={`Edit ${task.title}`}>Edit</button>
      <button type="button" onClick={() => onDelete(task)} aria-label={`Delete ${task.title}`}>Delete</button></div>}
  </article>;
}

export function ReviewQueuePanel({ workspace, csrfToken, approvalId }: { workspace: WorkspaceView; csrfToken: string; approvalId?: string }) {
  return <><ApprovalsPanel key={workspace.id} workspace={workspace} csrfToken={csrfToken} approvalId={approvalId} />
    <TasksPanel key={`tasks-${workspace.id}`} workspace={workspace} csrfToken={csrfToken} /></>;
}
function TasksPanel({ workspace, csrfToken }: { workspace: WorkspaceView; csrfToken: string }) {
  const cache = useQueryClient();
  const queue = useQuery({ queryKey: ["review-queue", workspace.id], queryFn: ({ signal }) => reviewApi.queue(workspace.id, signal), retry: false });
  const tasks = useQuery({ queryKey: ["tasks", workspace.id], queryFn: ({ signal }) => reviewApi.tasks(workspace.id, undefined, signal), retry: false });
  const canEdit = workspace.permissions.includes("task_write");
  const assignees = useQuery({ queryKey: ["task-assignees", workspace.id], queryFn: ({ signal }) => reviewApi.assignees(workspace.id, signal), enabled: canEdit, retry: false });
  const [editing, setEditing] = useState<Task | "new" | null>(null);
  const [deleting, setDeleting] = useState<Task | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }), useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }));
  async function refresh() { await Promise.all([cache.invalidateQueries({ queryKey: ["review-queue", workspace.id] }), cache.invalidateQueries({ queryKey: ["tasks", workspace.id] })]); }
  async function act(fn: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await fn(); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "The task change failed."); await refresh(); }
    finally { setBusy(false); }
  }
  function reorder(ids: string[]) {
    if (!queue.data || busy) return;
    void act(async () => {
      await reviewMutate(`${reviewPath(workspace.id)}/review-queue`, "PATCH", { ids, expected_version: queue.data.version }, csrfToken);
      setNotice("Review order saved.");
    });
  }
  function move(id: string, step: number) {
    const list = queue.data?.items ?? [];
    const from = list.findIndex(t => t.id === id), to = from + step;
    if (from < 0 || to < 0 || to >= list.length || list[to].deadline_locked) return;
    reorder(arrayMove(list.map(t => t.id), from, to));
  }
  function dragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (!over || active.id === over.id || !queue.data) return;
    const list = queue.data.items;
    const from = list.findIndex(t => t.id === active.id), to = list.findIndex(t => t.id === over.id);
    if (from < 0 || to < 0 || list[to].deadline_locked) return;
    reorder(arrayMove(list.map(t => t.id), from, to));
  }
  async function save(input: TaskInput) {
    await act(async () => {
      if (editing && editing !== "new") {
        await reviewMutate(`${reviewPath(workspace.id)}/tasks/${editing.id}`, "PATCH", { ...input, expected_version: editing.version }, csrfToken);
      } else {
        await reviewMutate(`${reviewPath(workspace.id)}/tasks`, "POST", input, csrfToken);
      }
      setEditing(null);
      setNotice("Task saved.");
    });
  }
  async function remove() {
    if (!deleting) return;
    await act(async () => {
      await reviewMutate(`${reviewPath(workspace.id)}/tasks/${deleting.id}`, "DELETE", { expected_version: deleting.version }, csrfToken);
      setDeleting(null);
      setNotice("Task deleted. Its audit history is retained.");
    });
  }
  if (queue.isPending || tasks.isPending) return <p role="status">Loading review tasks…</p>;
  if (queue.isError) return <ErrorState label="Review queue unavailable" error={queue.error} retry={() => queue.refetch()} />;
  if (tasks.isError) return <ErrorState label="Tasks unavailable" error={tasks.error} retry={() => tasks.refetch()} />;
  return <>
    <section className="panel"><div className="section-heading"><div><h2>Collection tasks</h2><p>Saved order follows current deadlines and your manual priority.</p></div>
      {canEdit && <button type="button" onClick={() => setEditing("new")}>New task</button>}</div>
      {canEdit && <p className="muted">Drag the handle or use the move buttons. Changes save to this workspace.</p>}
    </section>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {editing && <TaskForm key={editing === "new" ? "new" : editing.id} task={editing === "new" ? undefined : editing}
      assignees={assignees.data ?? []} pending={busy} onSave={save} onCancel={() => setEditing(null)} />}
    {deleting && <section className="panel" role="alertdialog" aria-label="Confirm deletion"><h2>Confirm deletion</h2>
      <p>Delete “{deleting.title}”? The task leaves the queue; its audit record remains.</p>
      <button type="button" className="btn btn-destructive" onClick={() => void remove()} disabled={busy}>Confirm delete</button>
      <button type="button" onClick={() => setDeleting(null)}>Cancel</button></section>}
    {queue.data.items.length === 0 ? <section className="panel"><h2>No open review tasks</h2><p>Create a task or wait for a source-backed action to require review.</p></section> :
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={dragEnd}>
        <SortableContext items={queue.data.items.map(t => t.id)} strategy={verticalListSortingStrategy}>
          <div className="review-task-list">{queue.data.items.map((task, index) => <SortableTask key={task.id} task={task} position={index} count={queue.data.items.length}
            canEdit={canEdit} busy={busy} onMove={move} onEdit={setEditing} onDelete={setDeleting} workspace={workspace.id} />)}</div>
        </SortableContext>
      </DndContext>}
    {tasks.data.items.some(t => t.status === "done") && <section className="panel"><h2>Completed tasks</h2>
      <div className="review-task-list">{tasks.data.items.filter(t => t.status === "done").map(t =>
        <CompletedTask key={t.id} task={t} workspace={workspace.id} canEdit={canEdit} busy={busy} onEdit={setEditing} onDelete={setDeleting} />)}</div>
    </section>}
  </>;
}
