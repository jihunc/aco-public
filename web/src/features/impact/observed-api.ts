import type {
  ChangeObserved,
  FreezeObserved,
  ObservedCohortView,
  ObservedList,
  ObservedPreview,
  ObservedSelection,
} from "@/lib/generated-api";
export type ObservedReview = {
  mode: "FREEZE" | "REFRESH" | "RESTATE";
  pairId: string | null;
  preview: ObservedPreview;
  command: FreezeObserved | ChangeObserved;
};
export type PendingObservedReview = {
  review: ObservedReview;
  phase: "captured" | "uncertain" | "conflict";
};
export function captureObservedReview(
  preview: ObservedPreview,
  evidence: string,
  requestId: string,
  parent?: ObservedCohortView,
  mode: "REFRESH" | "RESTATE" = "REFRESH",
): ObservedReview {
  if (!preview.selection.as_of_at || evidence.trim().length < 10)
    throw Error(
      "Review the actual source preview and provide its evidence note.",
    );
  const command: FreezeObserved = {
    request_id: requestId,
    selection: structuredClone(preview.selection),
    reviewed_hash: preview.reviewed_hash,
    evidence,
  };
  if (!parent)
    return structuredClone({ mode: "FREEZE", pairId: null, preview, command });
  const latest = parent.versions.at(-1),
    result = latest?.results.at(-1);
  if (!latest || !result)
    throw Error("Current persisted definition/result unavailable.");
  return structuredClone({
    mode,
    pairId: parent.id,
    preview,
    command: {
      ...command,
      expected_definition_id: latest.id,
      expected_result_id: result.id,
    },
  });
}
export function canSubmitObserved(
  review: ObservedReview,
  current: ObservedList | undefined,
  fresh: boolean,
): boolean {
  if (
    !current?.can_build ||
    !fresh ||
    !current.sources.some(
      (row) => row.connection_id === review.command.selection.connection_id,
    )
  )
    return false;
  if (review.mode === "FREEZE") return true;
  const latest = current.cohorts
    .find((row) => row.id === review.pairId)
    ?.versions.at(-1);
  return (
    !!latest &&
    "expected_definition_id" in review.command &&
    review.command.expected_definition_id === latest.id &&
    review.command.expected_result_id === latest.results.at(-1)?.id
  );
}
function exact(value: unknown) {
  if (
    value != null &&
    (typeof value !== "string" ||
      !/^[-+]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(value))
  )
    throw Error("Observed evidence requires exact decimal strings.");
}
function validateResult(row: ObservedPreview["result"]) {
  [
    row.opening_minor,
    row.raw_net_minor,
    row.recovery_numerator_minor,
    row.refunds_minor,
    row.reversals_minor,
    row.recovery_percent,
    row.paid_in_full_percent,
    row.days_to_collect,
    row.days_beyond_terms,
  ].forEach(exact);
}
export function validateObservedPreview(
  view: ObservedPreview,
): ObservedPreview {
  for (const row of view.members) {
    exact(row.opening_minor);
    exact(row.original_minor);
  }
  validateResult(view.result);
  return view;
}
export function readObservedReview(
  raw: string | null,
): PendingObservedReview | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as PendingObservedReview;
    const r = value.review;
    if (
      !["captured", "uncertain", "conflict"].includes(value.phase) ||
      !["FREEZE", "REFRESH", "RESTATE"].includes(r.mode) ||
      !r.command.request_id ||
      r.command.reviewed_hash !== r.preview.reviewed_hash ||
      JSON.stringify(r.command.selection) !==
        JSON.stringify(r.preview.selection)
    )
      return null;
    validateObservedPreview(r.preview);
    return value;
  } catch {
    return null;
  }
}
export class ObservedError extends Error {
  constructor(public status: number) {
    super(
      status === 409
        ? "The captured source, definition or result changed. Explicitly preview again; no automatic retry."
        : status === 403 || status === 404
          ? "Current Owner authority or source scope is unavailable."
          : status === 422
            ? "Check the due-date range, currency, terms and nonfuture UTC as-of."
            : "The publication outcome is uncertain. Reload saved cohorts or retry the same captured request.",
    );
  }
}
const base = (workspace: string) =>
  `/api/workspaces/${encodeURIComponent(workspace)}/impact/cohorts/observed`;
async function post<T>(
  path: string,
  command: object,
  csrf: string,
): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "content-type": "application/json", "x-csrf-token": csrf },
    body: JSON.stringify(command),
  });
  if (!response.ok) throw new ObservedError(response.status);
  return response.json();
}
export const observedApi = {
  async read(workspace: string, signal?: AbortSignal): Promise<ObservedList> {
    const response = await fetch(base(workspace), {
      credentials: "same-origin",
      cache: "no-store",
      signal,
    });
    if (!response.ok) throw new ObservedError(response.status);
    const view = (await response.json()) as ObservedList;
    for (const group of view.cohorts)
      for (const version of group.versions) {
        for (const row of [
          ...version.members,
          ...version.current_source_members,
        ]) {
          exact(row.opening_minor);
          exact(row.original_minor);
        }
        version.results.forEach(validateResult);
      }
    return view;
  },
  async preview(
    workspace: string,
    selection: ObservedSelection,
    csrf: string,
    parent?: ObservedCohortView,
    mode: "REFRESH" | "RESTATE" = "REFRESH",
  ): Promise<ObservedPreview> {
    const latest = parent?.versions.at(-1),
      result = latest?.results.at(-1);
    if (parent && (!latest || !result))
      throw Error("Current definition/result unavailable.");
    return validateObservedPreview(
      await post<ObservedPreview>(
        parent
          ? `${base(workspace)}/${encodeURIComponent(parent.id)}/preview`
          : `${base(workspace)}/preview`,
        parent
          ? {
              selection,
              expected_definition_id: latest!.id,
              expected_result_id: result!.id,
              mode,
            }
          : selection,
        csrf,
      ),
    );
  },
  submit: (workspace: string, review: ObservedReview, csrf: string) =>
    post<ObservedCohortView>(
      review.mode === "FREEZE"
        ? base(workspace)
        : `${base(workspace)}/${encodeURIComponent(review.pairId ?? "")}/${review.mode.toLowerCase()}`,
      review.command,
      csrf,
    ),
};
