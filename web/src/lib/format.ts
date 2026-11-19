// Human-readable display helpers shared by product screens. Exact integers stay exact:
// money is formatted from integer minor units without floating-point arithmetic.

const PRIORITY_LABELS = ["Low", "Normal", "High", "Urgent"];

export function formatMinor(minor: number | null | undefined, currency: string, exponent = 2): string {
  if (minor === null || minor === undefined || !Number.isFinite(minor)) return "Unavailable";
  const sign = minor < 0 ? "-" : "";
  const digits = String(Math.abs(Math.trunc(minor))).padStart(exponent + 1, "0");
  const major = exponent > 0 ? `${digits.slice(0, -exponent)}.${digits.slice(-exponent)}` : digits;
  return `${currency} ${sign}${major}`;
}

export function priorityLabel(rank: number): string {
  return PRIORITY_LABELS[rank] ?? `Priority ${rank}`;
}

export function dueLabel(iso: string | null | undefined, stamp: (value: string) => string): string {
  return iso ? stamp(iso) : "No due date";
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "Insufficient data";
  const total = Math.round(seconds);
  if (total < 60) return `${total} s`;
  if (total < 3600) return `${Math.floor(total / 60)} min ${total % 60} s`;
  return `${Math.floor(total / 3600)} h ${Math.floor((total % 3600) / 60)} min`;
}

export function shortInvoiceLabel(id: string, number?: string | null): string {
  return number ? number : `Invoice ${id.slice(0, 8)}`;
}
