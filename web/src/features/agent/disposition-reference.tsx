"use client";
import { useId, useState } from "react";
import type {
  DispositionReference,
  DispositionReferences,
} from "@/lib/generated-api";
import { caseApi } from "./case-api";

export function DispositionReferencePicker({
  workspace,
  caseId,
  caseVersion,
  invoiceId,
  onSelect,
}: {
  workspace: string;
  caseId: string;
  caseVersion: number;
  invoiceId: string;
  onSelect: (reference: DispositionReference | null) => void;
}) {
  const field = useId();
  const [snapshot, setSnapshot] = useState<DispositionReferences | null>(null);
  const [selected, setSelected] = useState<DispositionReference | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <fieldset>
      <legend>Verified disposition reference</legend>
      <p>
        Review the current source and explicitly select its captured version.
        Saving rechecks this exact reference. Contact restrictions remain
        enforced; no accounting or contact authority is changed.
      </p>
      <button
        type="button"
        disabled={busy || !invoiceId}
        onClick={async () => {
          setBusy(true);
          setError("");
          setSelected(null);
          setSnapshot(null);
          onSelect(null);
          try {
            const value = await caseApi.dispositionReferences(
              workspace,
              caseId,
              invoiceId,
            );
            if (
              value.case_id !== caseId ||
              value.case_version !== caseVersion ||
              value.invoice_id !== invoiceId
            )
              throw new Error(
                "The case changed. Refresh and review the current case before selecting a reference.",
              );
            setSnapshot(value);
          } catch (failure) {
            setError(
              failure instanceof Error
                ? failure.message
                : "Current references unavailable.",
            );
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy
          ? "Loading verified references…"
          : "Review current verified references"}
      </button>
      {error && <p role="alert">{error}</p>}
      {snapshot &&
        (snapshot.items.length ? (
          <>
            <label htmlFor={field}>Reviewed disposition reference</label>
            <select
              id={field}
              required
              value={selected?.id ?? ""}
              onChange={(event) => {
                const value =
                  snapshot.items.find(
                    (item) => item.id === event.target.value,
                  ) ?? null;
                setSelected(value);
                onSelect(value);
              }}
            >
              <option value="">Choose a verified source reference</option>
              {snapshot.items.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label} · version {item.version} · {item.id}
                </option>
              ))}
            </select>
          </>
        ) : (
          <p>
            No current verified disposition is available. Obtain the required
            source refund, enforced restriction, or independent Owner contact
            review first.
          </p>
        ))}
      {selected && (
        <article aria-label="Captured disposition source">
          <p>
            {selected.synthetic
              ? "SYNTHETIC source"
              : "Source-backed reference"}{" "}
            · {selected.source_type} · version {selected.version}
          </p>
          <p>Reference {selected.id}</p>
          <pre className="task-notes">
            {JSON.stringify(selected.details, null, 2)}
          </pre>
          <p>Captured material hash: {selected.material_hash}</p>
        </article>
      )}
    </fieldset>
  );
}
