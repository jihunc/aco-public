import type { MoneyJson } from "./generated-api";
// BigInt all the way to display. Intl formats integer grouping; minor fractions never become Number.
export function formatMoney(value: MoneyJson): string {
  const raw = BigInt(value.minor_units);
  const negative = raw < 0n;
  const absolute = negative ? -raw : raw;
  const scale = 10n ** BigInt(value.exponent);
  const integer = new Intl.NumberFormat("en-AU", {
    maximumFractionDigits: 0,
  }).format(absolute / scale);
  const fraction = value.exponent
    ? "." + (absolute % scale).toString().padStart(value.exponent, "0")
    : "";
  return `${negative ? "-" : ""}${value.currency} ${integer}${fraction}`;
}
