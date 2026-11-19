"use client";
import { useState, type FormEvent } from "react";
import type { Assignee, Task } from "./api";

export type TaskInput = { title: string; notes: string; assignee_id: string | null; priority: number; due_at: string | null; status?: Task["status"] };
const localDate = (date: string | null) => {
  if (!date) return "";
  const instant = new Date(date);
  return new Date(instant.getTime() - instant.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
};

export function TaskForm({ task, assignees, pending, onSave, onCancel }: {
  task?: Task; assignees: Assignee[]; pending: boolean;
  onSave: (input: TaskInput) => Promise<void>; onCancel: () => void;
}) {
  const [title, setTitle] = useState(task?.title ?? "");
  const [notes, setNotes] = useState(task?.notes ?? "");
  const [owner, setOwner] = useState(task?.assignee_id ?? "");
  const [priority, setPriority] = useState(task?.priority ?? 1);
  const [due, setDue] = useState(localDate(task?.due_at ?? null));
  const [status, setStatus] = useState<Task["status"]>(task?.status ?? "open");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await onSave({ title: title.trim(), notes, assignee_id: owner || null,
      priority, due_at: due ? new Date(due).toISOString() : null, ...(task ? { status } : {}) });
  }
  return <section className="panel task-form-panel" aria-label={task ? "Edit task" : "New task"}>
    <h2>{task ? "Edit task" : "New task"}</h2>
    <form onSubmit={submit} className="task-form">
      <label>Task title<input required maxLength={240} value={title} onChange={e => setTitle(e.target.value)} /></label>
      <label>Owner<select value={owner} onChange={e => setOwner(e.target.value)}>
        <option value="">Unassigned</option>{assignees.map(a => <option key={a.user_id} value={a.user_id}>{a.email}</option>)}
      </select></label>
      <label>Priority<select value={priority} onChange={e => setPriority(Number(e.target.value))}>
        <option value={0}>Low</option><option value={1}>Normal</option><option value={2}>High</option><option value={3}>Urgent</option>
      </select></label>
      <label>Due date and time (your device timezone)<input type="datetime-local" value={due} onChange={e => setDue(e.target.value)} /></label>
      {task && <label>Status<select value={status} onChange={e => setStatus(e.target.value as Task["status"])}>
        <option value="open">Open</option><option value="in_progress">In progress</option><option value="done">Done</option>
      </select></label>}
      <label className="task-form-notes">Notes<textarea maxLength={4000} rows={4} value={notes} onChange={e => setNotes(e.target.value)} /></label>
      <div className="task-form-actions"><button type="submit" disabled={pending || !title.trim()}>{task ? "Save task" : "Create task"}</button>
        <button type="button" onClick={onCancel}>Cancel</button></div>
    </form>
  </section>;
}
