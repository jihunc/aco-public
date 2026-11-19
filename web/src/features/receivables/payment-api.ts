import type { InvoiceView, MoneyJson } from "@/lib/generated-api";
export type SourcePayment = {
  provider_connection_id: string;
  external_invoice_id: string;
  amount: MoneyJson;
  operation_id: string;
};
export type PaymentReconciliation = {
  event_id: string;
  invoice_id: string;
  financial_status: string;
  collection_lifecycle: string;
  source_revision: string;
  synthetic: boolean;
};
export function paymentAmount(
  decimal: string,
  outstanding: MoneyJson,
): MoneyJson {
  const text = decimal.trim();
  if (
    text.length > 80 ||
    !/^\d+(?:\.\d+)?$/.test(text) ||
    !Number.isInteger(outstanding.exponent) ||
    outstanding.exponent < 0 ||
    outstanding.exponent > 6
  )
    throw new Error(
      "Enter a positive decimal amount without separators or scientific notation.",
    );
  const [whole, fraction = ""] = text.split(".");
  if (fraction.length > outstanding.exponent)
    throw new Error(
      `Use at most ${outstanding.exponent} decimal places for ${outstanding.currency}.`,
    );
  const minor =
    BigInt(whole) * 10n ** BigInt(outstanding.exponent) +
    BigInt(fraction.padEnd(outstanding.exponent, "0") || "0");
  if (minor <= 0n || minor > BigInt(outstanding.minor_units))
    throw new Error(
      "Enter an amount greater than zero and no larger than the current source balance.",
    );
  return {
    minor_units: minor.toString(),
    currency: outstanding.currency,
    exponent: outstanding.exponent,
  };
}
export function preparePayment(
  previous: SourcePayment | null,
  invoice: InvoiceView,
  decimal: string,
  newId: () => string = () => crypto.randomUUID(),
): SourcePayment {
  const amount = paymentAmount(decimal, invoice.outstanding);
  if (
    previous &&
    previous.provider_connection_id === invoice.source.connection_id &&
    previous.external_invoice_id === invoice.source.external_id &&
    previous.amount.minor_units === amount.minor_units &&
    previous.amount.currency === amount.currency &&
    previous.amount.exponent === amount.exponent
  )
    return previous;
  return {
    provider_connection_id: invoice.source.connection_id,
    external_invoice_id: invoice.source.external_id,
    amount,
    operation_id: newId(),
  };
}
// The existing API requires strict Money.int. Encode validated decimal digits as a JSON
// integer token directly; converting to Number or quoting the value would change its contract.
export function paymentBody(command: SourcePayment): string {
  if (
    !/^[1-9]\d*$/.test(command.amount.minor_units) ||
    command.amount.minor_units.length > 80 ||
    !Number.isInteger(command.amount.exponent) ||
    command.amount.exponent < 0 ||
    command.amount.exponent > 6 ||
    !/^[A-Z]{3}$/.test(command.amount.currency)
  )
    throw new Error(
      "The captured payment amount is invalid. Review the source before retrying.",
    );
  return `{"provider_connection_id":${JSON.stringify(command.provider_connection_id)},"external_invoice_id":${JSON.stringify(command.external_invoice_id)},"amount":{"minor_units":${command.amount.minor_units},"currency":${JSON.stringify(command.amount.currency)},"exponent":${command.amount.exponent}},"operation_id":${JSON.stringify(command.operation_id)}}`;
}
export class SourcePaymentError extends Error {
  constructor(
    message: string,
    public readonly rejected: boolean,
  ) {
    super(message);
    this.name = "SourcePaymentError";
  }
}
export const paymentApi = {
  async simulate(
    workspace: string,
    command: SourcePayment,
    csrf: string,
  ): Promise<PaymentReconciliation> {
    const response = await fetch(
      `/api/workspaces/${encodeURIComponent(workspace)}/demo/accounting/simulate-payment`,
      {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "content-type": "application/json", "x-csrf-token": csrf },
        body: paymentBody(command),
      },
    );
    if (!response.ok) {
      const rejected = [400, 401, 403, 404, 409, 422].includes(response.status);
      throw new SourcePaymentError(
        response.status === 401
          ? "Sign in again before retrying this captured operation."
          : response.status === 403 || response.status === 404
            ? "Your current membership or accounting source does not permit this demo payment."
            : response.status === 409 || response.status === 422
              ? "The source payment was rejected. Refresh current source data and review the amount and source binding."
              : "The payment outcome could not be confirmed. Refresh source data or retry the same captured operation; no new operation has been created.",
        rejected,
      );
    }
    return response.json();
  },
};
