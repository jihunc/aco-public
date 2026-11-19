import { readApi } from "@/lib/api";
import type { InvoiceView, VoiceEvidenceView } from "@/lib/generated-api";
import type { Communication } from "@/features/communications/api";
// Current service DTOs: features/cases/dto.py; Root owns generated API types.
export type CaseEvidence = {
  id: string;
  communication_id: string | null;
  voice_source?: VoiceEvidenceView | null;
  excerpt: string;
  offset: number;
  kind: string;
  synthetic: boolean;
  source_state: string;
  created_at: string;
};
export type CollectionCase = {
  id: string;
  customer_id: string;
  issue_type: string;
  claim_key: string;
  substance: string;
  status:
    | "OPEN"
    | "INVESTIGATING"
    | "WAITING_CUSTOMER"
    | "WAITING_INTERNAL"
    | "AWAITING_APPROVAL"
    | "RESOLVED"
    | "CLOSED";
  version: number;
  opened_at: string;
  updated_at: string;
  resolved_at: string | null;
  closed_at: string | null;
  closure_reason: string | null;
  review_required: boolean;
  task_id: string;
  synthetic: boolean;
  invoice_links: {
    invoice_id: string;
    issue_state: string;
    impact: string;
    hold_id: string | null;
    resolution_action: string | null;
    resolution_evidence_id: string | null;
    resolved_at: string | null;
  }[];
  evidence: CaseEvidence[];
  history: {
    id: string;
    event: string;
    actor_id: string;
    substantive_human: boolean;
    details: Record<string, unknown>;
    created_at: string;
  }[];
};
const base = (workspace: string) =>
  `/api/workspaces/${encodeURIComponent(workspace)}`;
export const businessApi = {
  cases: (workspace: string, invoice: string, signal?: AbortSignal) =>
    readApi<{ items: CollectionCase[] }>(
      `${base(workspace)}/cases?invoice_id=${encodeURIComponent(invoice)}`,
      signal,
    ),
  case: (workspace: string, id: string, signal?: AbortSignal) =>
    readApi<CollectionCase>(
      `${base(workspace)}/cases/${encodeURIComponent(id)}`,
      signal,
    ),
};
export type PromiseMoney = {
  minor_units: string;
  currency: string;
  exponent: number;
};
export type PaymentPromise = {
  id: string;
  customer_id: string;
  status:
    | "CAPTURED"
    | "NEEDS_CONFIRMATION"
    | "ACTIVE"
    | "FULFILLED"
    | "MISSED"
    | "SUPERSEDED"
    | "CANCELLED";
  version: number;
  source_communication_id: string | null;
  voice_source?: VoiceEvidenceView | null;
  source_text: string;
  source_offset: number;
  source_at: string;
  source_state: string;
  synthetic: boolean;
  amount: PromiseMoney | null;
  due_date: string | null;
  timezone: string | null;
  cutoff_at: string | null;
  confirmed_at: string | null;
  confirmation_communication_id: string | null;
  needs_confirmation: boolean;
  confirmation_reasons: string[];
  invoice_links: {
    invoice_id: string;
    amount: PromiseMoney | null;
    hold_id: string | null;
  }[];
  candidate_invoice_ids: string[];
  installments: {
    id: string;
    due_date: string;
    cutoff_at: string;
    amount: PromiseMoney;
    allocations: { invoice_id: string; amount: PromiseMoney }[];
    status: string;
  }[];
  replaces_promise_id: string | null;
  superseded_by_id: string | null;
  replacement_lineage?: {
    id: string;
    version: number;
    status: "ACTIVE" | "NEEDS_CONFIRMATION";
  }[];
  review_task_id: string | null;
  created_at: string;
  updated_at: string;
  history: {
    id: string;
    event: string;
    actor_id: string;
    details: Record<string, unknown>;
    created_at: string;
  }[];
  verification_status: "UNVERIFIED" | "VERIFIED" | "PENDING_REVIEW";
  scheduling_status: PromiseSchedulingStatus;
};
export type PromiseSchedulingStatus =
  | "PENDING_ENGINE"
  | "PENDING_CONFIRMATION"
  | "QUEUED"
  | "RUNNING"
  | "COMPLETED"
  | "REVIEW_REQUIRED";
export type PromiseCheckPlan = {
  promise_id: string;
  promise_version: number;
  event_id: string;
  due_at: string;
  invoice_ids: string[];
  installment_id: string | null;
  status: Exclude<PromiseSchedulingStatus, "PENDING_CONFIRMATION">;
  action_ids: string[];
};
export type PromiseCapture = {
  invoice: InvoiceView;
  source: Communication;
  excerpt: string;
  decimal: string;
  dueDate: string;
  timezone: string;
  invoices?: InvoiceView[];
  allocations?: { invoice_id: string; decimal: string }[];
  replaces?: PaymentPromise;
  installments?: {
    dueDate: string;
    decimal: string;
    allocations: { invoice_id: string; decimal: string }[];
  }[];
};
export type PreparedPromiseCapture = {
  body: string;
  amount: PromiseMoney;
  dueDate: string;
  timezone: string;
  evidence: { communication_id: string; excerpt: string; offset: number };
  allocations: { invoice_id: string; number: string; amount: PromiseMoney }[];
  replaces: { id: string; version: number; status?: string } | null;
  installments: {
    dueDate: string;
    amount: PromiseMoney;
    allocations: { invoice_id: string; number: string; amount: PromiseMoney }[];
  }[];
};
export function promiseMinor(
  decimal: string,
  exponent: number,
  allowZero = false,
): bigint {
  const text = decimal.trim();
  if (
    text.length > 80 ||
    !/^\d+(?:\.\d+)?$/.test(text) ||
    !Number.isInteger(exponent) ||
    exponent < 0 ||
    exponent > 6
  )
    throw new Error("Enter an exact positive decimal promise amount.");
  const [whole, fraction = ""] = text.split(".");
  if (fraction.length > exponent)
    throw new Error(`Use at most ${exponent} decimal places.`);
  const minor =
    BigInt(whole) * 10n ** BigInt(exponent) +
    BigInt(fraction.padEnd(exponent, "0") || "0");
  if (minor < 0n || (!allowZero && minor === 0n))
    throw new Error("Every promise amount and allocation must be positive.");
  return minor;
}
function validPromiseDate(value: string): boolean {
  return (
    /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    !Number.isNaN(Date.parse(`${value}T00:00:00Z`)) &&
    new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value
  );
}
function moneyJSON(amount: PromiseMoney): string {
  if (!/^\d+$/.test(amount.minor_units))
    throw new Error("Exact integer minor units required.");
  return `{"minor_units":${BigInt(amount.minor_units)},"currency":${JSON.stringify(amount.currency)},"exponent":${amount.exponent}}`;
}
export function preparePromiseCapture(
  command: PromiseCapture,
): PreparedPromiseCapture {
  const { invoice, source, excerpt, decimal, dueDate, timezone, replaces } =
    command;
  const invoices = [...(command.invoices ?? [invoice])].sort((a, b) =>
    a.id.localeCompare(b.id),
  );
  const offset = source.content.text_body?.indexOf(excerpt) ?? -1;
  if (
    source.direction !== "incoming" ||
    source.source_state !== "stored" ||
    !excerpt.trim() ||
    offset < 0 ||
    !invoices.length ||
    invoices.length > 100 ||
    new Set(invoices.map((row) => row.id)).size !== invoices.length ||
    invoices.some(
      (row) => !source.invoice_links.some((link) => link.invoice_id === row.id),
    )
  )
    throw new Error(
      "Select an exact span from a stored incoming message linked to every selected invoice.",
    );
  if (
    invoices.some(
      (row) =>
        row.customer.id !== invoice.customer.id ||
        row.original.currency !== invoice.original.currency ||
        row.original.exponent !== invoice.original.exponent,
    )
  )
    throw new Error(
      "Promise scope requires one actual customer, currency and exponent; no currency conversion is inferred.",
    );
  if (!validPromiseDate(dueDate) || !timezone.trim())
    throw new Error("Enter the source commitment date and recipient timezone.");
  new Intl.DateTimeFormat("en", { timeZone: timezone.trim() }).format();
  const basis = {
    currency: invoice.original.currency,
    exponent: invoice.original.exponent,
  };
  const minor = promiseMinor(decimal, basis.exponent);
  const amount = { ...basis, minor_units: minor.toString() };
  const provided = command.allocations ?? [];
  if (
    new Set(provided.map((row) => row.invoice_id)).size !== provided.length ||
    provided.some((row) => !invoices.some((item) => item.id === row.invoice_id))
  )
    throw new Error(
      "Provide one explicit allocation for each selected invoice.",
    );
  const allocations = invoices.map((row) => {
    const part = provided.find((item) => item.invoice_id === row.id);
    if (invoices.length > 1 && !part)
      throw new Error(
        "Explicit per-invoice allocations are required; the total is never equally split.",
      );
    return {
      invoice_id: row.id,
      number: row.number,
      amount: {
        ...basis,
        minor_units: part
          ? promiseMinor(part.decimal, basis.exponent).toString()
          : minor.toString(),
      },
    };
  });
  if (
    allocations.reduce(
      (total, row) => total + BigInt(row.amount.minor_units),
      0n,
    ) !== minor
  )
    throw new Error(
      "Explicit invoice allocations must add up exactly to the total promise amount.",
    );
  if (replaces) {
    const pending = replaces.status === "NEEDS_CONFIRMATION";
    const exactScope =
      replaces.invoice_links.length === invoices.length &&
      replaces.invoice_links.every((link) =>
        invoices.some((row) => row.id === link.invoice_id),
      );
    if (
      (!pending && replaces.status !== "ACTIVE") ||
      replaces.customer_id !== invoice.customer.id ||
      replaces.source_communication_id === source.id ||
      ((!pending || replaces.invoice_links.length > 0) && !exactScope) ||
      (replaces.amount &&
        (replaces.amount.currency !== basis.currency ||
          replaces.amount.exponent !== basis.exponent))
    )
      throw new Error(
        "An amendment or clarification needs a new exact incoming source for the same customer and known invoice/currency scope.",
      );
    if (
      pending &&
      ((replaces.amount &&
        replaces.amount.minor_units !== amount.minor_units) ||
        (replaces.due_date && replaces.due_date !== dueDate) ||
        (replaces.timezone && replaces.timezone !== timezone.trim()) ||
        replaces.invoice_links.some(
          (link) =>
            link.amount &&
            allocations.find((part) => part.invoice_id === link.invoice_id)
              ?.amount.minor_units !== link.amount.minor_units,
        ))
    )
      throw new Error(
        "Clarification must preserve known amount, date, timezone and allocations; only unknown terms may be resolved.",
      );
  }
  if ((command.installments?.length ?? 0) > 100)
    throw new Error("Use no more than 100 explicit source installments.");
  const installments = (command.installments ?? []).map((row) => {
    if (!validPromiseDate(row.dueDate))
      throw new Error("Enter each exact source installment date.");
    const rowMinor = promiseMinor(row.decimal, basis.exponent);
    let parts = row.allocations;
    if (!parts.length && invoices.length === 1)
      parts = [{ invoice_id: invoices[0].id, decimal: row.decimal }];
    if (
      parts.length !== invoices.length ||
      new Set(parts.map((part) => part.invoice_id)).size !== parts.length ||
      parts.some(
        (part) => !invoices.some((item) => item.id === part.invoice_id),
      )
    )
      throw new Error(
        "Explicitly allocate every installment across the selected invoices; enter 0 only when the source assigns no portion.",
      );
    const portions = allocations
      .map((part) => ({
        invoice_id: part.invoice_id,
        number: part.number,
        amount: {
          ...basis,
          minor_units: promiseMinor(
            parts.find((item) => item.invoice_id === part.invoice_id)!.decimal,
            basis.exponent,
            true,
          ).toString(),
        },
      }))
      .filter((part) => BigInt(part.amount.minor_units) > 0n);
    if (
      portions.reduce(
        (sum, part) => sum + BigInt(part.amount.minor_units),
        0n,
      ) !== rowMinor
    )
      throw new Error(
        "Each installment's invoice portions must add up exactly to that installment amount.",
      );
    return {
      dueDate: row.dueDate,
      amount: { ...basis, minor_units: rowMinor.toString() },
      allocations: portions,
    };
  });
  if (installments.length) {
    if (
      installments.reduce(
        (sum, row) => sum + BigInt(row.amount.minor_units),
        0n,
      ) !== minor
    )
      throw new Error(
        "Installment amounts must add up exactly to the total commitment.",
      );
    for (const part of allocations)
      if (
        installments.reduce(
          (sum, row) =>
            sum +
            BigInt(
              row.allocations.find(
                (item) => item.invoice_id === part.invoice_id,
              )?.amount.minor_units ?? "0",
            ),
          0n,
        ) !== BigInt(part.amount.minor_units)
      )
        throw new Error(
          "Installment portions for each invoice must match its overall explicit allocation.",
        );
  }
  if (
    replaces?.status === "NEEDS_CONFIRMATION" &&
    replaces.installments?.length
  ) {
    if (
      replaces.installments.length !== installments.length ||
      replaces.installments.some((old, index) => {
        const next = installments[index];
        return (
          old.due_date !== next.dueDate ||
          old.amount.minor_units !== next.amount.minor_units ||
          old.allocations.some(
            (part) =>
              next.allocations.find(
                (value) => value.invoice_id === part.invoice_id,
              )?.amount.minor_units !== part.amount.minor_units,
          )
        );
      })
    )
      throw new Error(
        "Clarification must preserve known installment dates and portions.",
      );
  }
  const evidence = {
    communication_id: source.id,
    excerpt,
    offset: Array.from((source.content.text_body ?? "").slice(0, offset))
      .length,
  };
  const fields = JSON.stringify({
    customer_id: invoice.customer.id,
    invoice_ids: invoices.map((row) => row.id),
    evidence,
    due_date: dueDate,
    timezone: timezone.trim(),
    ...(replaces
      ? {
          replaces_promise_id: replaces.id,
          replacement_expected_version: replaces.version,
        }
      : {}),
  });
  const partsJSON = (parts: PreparedPromiseCapture["allocations"]) =>
    `[${parts.map((row) => `{"invoice_id":${JSON.stringify(row.invoice_id)},"amount":${moneyJSON(row.amount)}}`).join(",")}]`;
  const installmentsJSON = installments.length
    ? `,"installments":[${installments.map((row) => `{"due_date":${JSON.stringify(row.dueDate)},"amount":${moneyJSON(row.amount)},"allocations":${partsJSON(row.allocations)}}`).join(",")}]`
    : "";
  const body = `${fields.slice(0, -1)},"amount":${moneyJSON(amount)},"allocations":${partsJSON(allocations)}${installmentsJSON}}`;
  return {
    body,
    amount,
    dueDate,
    timezone: timezone.trim(),
    evidence,
    allocations,
    replaces: replaces
      ? { id: replaces.id, version: replaces.version, status: replaces.status }
      : null,
    installments,
  };
}
export function samePromiseReview(
  captured: PaymentPromise,
  current: PaymentPromise,
) {
  return (
    captured.id === current.id &&
    captured.version === current.version &&
    JSON.stringify(captured.replacement_lineage) ===
      JSON.stringify(current.replacement_lineage) &&
    captured.source_communication_id === current.source_communication_id &&
    captured.source_text === current.source_text &&
    captured.source_offset === current.source_offset &&
    JSON.stringify(captured.voice_source) ===
      JSON.stringify(current.voice_source)
  );
}
function promiseEvidence(record: PaymentPromise) {
  if (record.voice_source)
    return {
      kind: "voice_operational" as const,
      voice_evidence_id: record.voice_source.id,
      facts_hash: record.voice_source.facts_hash,
    };
  if (!record.source_communication_id)
    throw new Error("The retained commitment source is unavailable.");
  return {
    communication_id: record.source_communication_id,
    excerpt: record.source_text,
    offset: record.source_offset,
  };
}
async function mutatePromise(
  workspace: string,
  path: string,
  body: string,
  csrf: string,
): Promise<PaymentPromise> {
  if (!csrf) throw new Error("Sign in again before changing this commitment.");
  const response = await fetch(`${base(workspace)}/promises${path}`, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "content-type": "application/json", "x-csrf-token": csrf },
    body,
  });
  if (!response.ok) {
    const message =
      response.status === 409
        ? "The promise or source changed. Refresh and review before trying again."
        : response.status === 401
          ? "Sign in again before recording this commitment."
          : response.status === 403 || response.status === 404
            ? "Your current membership or source does not permit this change."
            : response.status === 422
              ? "Review the exact source span, amount, date, timezone and invoice scope."
              : "The result could not be confirmed. Refresh the promise history before trying again.";
    throw new Error(message);
  }
  return response.json();
}
export const promiseApi = {
  async capture(
    workspace: string,
    command: PromiseCapture,
    csrf: string,
  ): Promise<PaymentPromise> {
    return mutatePromise(
      workspace,
      "",
      preparePromiseCapture(command).body,
      csrf,
    );
  },
  capturePrepared(
    workspace: string,
    review: PreparedPromiseCapture,
    csrf: string,
  ) {
    return mutatePromise(workspace, "", review.body, csrf);
  },
  confirm(
    workspace: string,
    record: PaymentPromise,
    reason: string,
    csrf: string,
    replacement?: PaymentPromise,
    lineage: PaymentPromise[] = [],
  ) {
    if (
      record.replaces_promise_id &&
      (!replacement ||
        replacement.id !== record.replaces_promise_id ||
        !["ACTIVE", "NEEDS_CONFIRMATION"].includes(replacement.status))
    )
      return Promise.reject(
        new Error(
          "Review the current original promise before confirming this replacement.",
        ),
      );
    const snapshots = record.replacement_lineage ?? [];
    if (
      snapshots.some(
        (snapshot) =>
          !lineage.some(
            (current) =>
              current.id === snapshot.id &&
              current.version === snapshot.version &&
              current.status === snapshot.status,
          ),
      )
    )
      return Promise.reject(
        new Error(
          "Review the exact captured lineage; a predecessor or active ancestor changed.",
        ),
      );
    const activeAncestor = lineage.find(
      (current) =>
        current.status === "ACTIVE" &&
        current.id !== record.replaces_promise_id,
    );
    if (!reason.trim())
      return Promise.reject(
        new Error("Explain your documentary confirmation."),
      );
    return mutatePromise(
      workspace,
      `/${encodeURIComponent(record.id)}/confirm`,
      JSON.stringify({
        promise_id: record.id,
        expected_version: record.version,
        ...(record.replaces_promise_id
          ? { replacement_expected_version: replacement!.version }
          : {}),
        ...(activeAncestor
          ? { active_predecessor_expected_version: activeAncestor.version }
          : {}),
        confirmation_evidence: promiseEvidence(record),
        reason: reason.trim(),
      }),
      csrf,
    );
  },
  cancel(
    workspace: string,
    record: PaymentPromise,
    reason: string,
    csrf: string,
  ) {
    if (!reason.trim())
      return Promise.reject(new Error("Explain the cancellation."));
    return mutatePromise(
      workspace,
      `/${encodeURIComponent(record.id)}/cancel`,
      JSON.stringify({
        promise_id: record.id,
        expected_version: record.version,
        evidence: promiseEvidence(record),
        reason: reason.trim(),
      }),
      csrf,
    );
  },
  checkPlans: (workspace: string, id: string, signal?: AbortSignal) =>
    readApi<PromiseCheckPlan[]>(
      `${base(workspace)}/promises/${encodeURIComponent(id)}/check-plans`,
      signal,
    ),
  list: (workspace: string, invoice: string, signal?: AbortSignal) =>
    readApi<{ items: PaymentPromise[] }>(
      `${base(workspace)}/promises?invoice_id=${encodeURIComponent(invoice)}`,
      signal,
    ).then((page) => {
      page.items.forEach(assertExactPromiseMoney);
      return page;
    }),
  get: (workspace: string, id: string, signal?: AbortSignal) =>
    readApi<PaymentPromise>(
      `${base(workspace)}/promises/${encodeURIComponent(id)}`,
      signal,
    ).then(assertExactPromiseMoney),
};

function assertExactPromiseMoney(record: PaymentPromise): PaymentPromise {
  const amounts = [
    record.amount,
    ...record.invoice_links.map((link) => link.amount),
    ...record.installments.flatMap((item) => [
      item.amount,
      ...item.allocations.map((allocation) => allocation.amount),
    ]),
  ];
  if (
    amounts.some(
      (amount) =>
        amount !== null &&
        (typeof amount.minor_units !== "string" ||
          !/^-?[0-9]+$/.test(amount.minor_units)),
    )
  )
    throw new Error(
      "Promise amounts require exact minor-unit strings. Refresh after the service response is corrected.",
    );
  return record;
}
