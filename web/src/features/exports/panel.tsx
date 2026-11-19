"use client";
import { useId, useState } from "react";
import { downloadExport, exportResources, type ExportResource } from "./api";

export function ExportPanel({ workspace, csrfToken, disabled = false }: { workspace: string; csrfToken: string; disabled?: boolean }) {
  const id = useId();
  const [selected, setSelected] = useState<ExportResource[]>(exportResources.map(([key]) => key));
  const [from, setFrom] = useState(() => new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10));
  const [through, setThrough] = useState(() => new Date().toISOString().slice(0, 10));
  const [pending, setPending] = useState(false), [error, setError] = useState("");
  const [receipt, setReceipt] = useState("");
  async function download() {
    setPending(true); setError(""); setReceipt("");
    try {
      const before = new Date(`${through}T00:00:00Z`); before.setUTCDate(before.getUTCDate() + 1);
      const result = await downloadExport(workspace, { resources: selected,
        created_from: new Date(`${from}T00:00:00Z`).toISOString(), created_before: before.toISOString() }, csrfToken);
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = `aco-business-${workspace}.json`;
      document.body.appendChild(anchor); anchor.click(); anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setReceipt(`Download prepared. SHA-256 ${result.sha256}`);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Export failed. No file was downloaded."); }
    finally { setPending(false); }
  }
  return <section className="space-y-3 rounded border p-5" aria-label="Business export">
    <h3 className="text-xl font-semibold">Business export</h3>
    <p>Current projections and retained history for selected company records created in the UTC date window. This is not a historical reconstruction.</p>
    <p>Original bytes, communication bodies, source excerpts, transcripts and recordings use separately authorized private downloads. Credentials and internal storage locations are excluded.</p>
    <p>Up to 5,000 records including history and 10 MiB per file. A larger selection is refused without a partial file.</p>
    <form onSubmit={event => { event.preventDefault(); void download(); }} className="space-y-3">
      <fieldset disabled={disabled || pending} className="space-y-3">
        <legend>Included records</legend>
        <div className="grid gap-2 sm:grid-cols-2">{exportResources.map(([key, label]) => <label key={key} className="flex gap-2 items-start">
          <input type="checkbox" checked={selected.includes(key)} onChange={event => setSelected(items => event.target.checked ? [...items, key] : items.filter(item => item !== key))} />{label}
        </label>)}</div>
        <div><label htmlFor={`${id}-from`}>Created from (UTC)</label><input className="ml-2 border p-2" id={`${id}-from`} type="date" required value={from} onChange={event => setFrom(event.target.value)} /></div>
        <div><label htmlFor={`${id}-through`}>Created through (UTC, inclusive)</label><input className="ml-2 border p-2" id={`${id}-through`} type="date" required value={through} min={from} onChange={event => setThrough(event.target.value)} /></div>
        <button disabled={selected.length === 0} type="submit">{pending ? "Preparing export…" : "Download business export"}</button>
      </fieldset>
    </form>
    {error && <p role="alert">{error}</p>}{receipt && <p role="status" className="break-all">{receipt}</p>}
  </section>;
}
