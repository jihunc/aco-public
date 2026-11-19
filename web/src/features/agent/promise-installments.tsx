"use client";
import type { InvoiceView } from "@/lib/generated-api";
export type InstallmentDraft = {
  key: string;
  dueDate: string;
  decimal: string;
  portions: Record<string, string>;
};
export function PromiseInstallmentInputs({
  invoices,
  rows,
  onChange,
  disabled,
}: {
  invoices: InvoiceView[];
  rows: InstallmentDraft[];
  onChange: (rows: InstallmentDraft[]) => void;
  disabled: boolean;
}) {
  function update(index: number, fields: Partial<InstallmentDraft>) {
    onChange(rows.map((row, i) => (i === index ? { ...row, ...fields } : row)));
  }
  const currency = invoices[0]?.original.currency ?? "source currency";
  return (
    <fieldset disabled={disabled} className="promise-installments">
      <legend>Optional source-stated installment plan</legend>
      <p>
        Add installments only when the incoming statement explicitly sets each
        amount and date. Dates, totals and invoice portions are never inferred
        or equally split.
      </p>
      {rows.map((row, index) => (
        <section key={row.key} aria-label={`Installment ${index + 1} terms`}>
          <h3>Installment {index + 1}</h3>
          <label>
            Installment {index + 1} date
            <input
              required
              type="date"
              value={row.dueDate}
              onChange={(event) =>
                update(index, { dueDate: event.target.value })
              }
            />
          </label>
          <label>
            Installment {index + 1} amount ({currency})
            <input
              required
              inputMode="decimal"
              value={row.decimal}
              onChange={(event) =>
                update(index, { decimal: event.target.value })
              }
            />
          </label>
          {invoices.length > 1 &&
            invoices.map((invoice) => (
              <label key={invoice.id}>
                Installment {index + 1} allocation for {invoice.number} (
                {currency})
                <input
                  required
                  inputMode="decimal"
                  value={row.portions[invoice.id] ?? ""}
                  onChange={(event) =>
                    update(index, {
                      portions: {
                        ...row.portions,
                        [invoice.id]: event.target.value,
                      },
                    })
                  }
                />
              </label>
            ))}
          {invoices.length > 1 && (
            <p>
              Enter an explicit amount for every selected invoice; 0 means this
              installment has no portion for that invoice. Each row must equal
              its amount, and invoice portions across all installments must
              match the overall allocations.
            </p>
          )}
          <button
            type="button"
            onClick={() => onChange(rows.filter((_, i) => i !== index))}
          >
            Remove installment {index + 1}
          </button>
        </section>
      ))}
      <button
        type="button"
        disabled={disabled || rows.length >= 100 || !invoices.length}
        onClick={() =>
          onChange([
            ...rows,
            {
              key: crypto.randomUUID(),
              dueDate: "",
              decimal: "",
              portions: {},
            },
          ])
        }
      >
        Add source installment
      </button>
      {rows.length > 0 && (
        <p>
          The overall promise date and every installment date must follow the
          source statement. Confirmation remains a separate reviewed action.
        </p>
      )}
    </fieldset>
  );
}
