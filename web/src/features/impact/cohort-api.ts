// Authoritative generated DTOs: features/impact/cohort_dto.py. No client arithmetic or source edits.
import type {
  CohortListView,
  CohortPairView,
  CohortResultView,
} from "@/lib/generated-api";
export type ReviewedResult = {
  id: string;
  version: number;
  side: string;
  as_of_at: string;
};
export type CohortReview = {
  kind: "seed" | "refresh";
  request_id: string;
  evidence: string;
  pair_id: string | null;
  expected_result_ids: string[];
  reviewed: ReviewedResult[];
};
export type PendingCohortReview = {
  review: CohortReview;
  phase: "captured" | "uncertain" | "conflict";
};
export function latestResult(
  results: CohortResultView[],
): CohortResultView | undefined {
  return results.reduce<CohortResultView | undefined>(
    (latest, row) => (!latest || row.version > latest.version ? row : latest),
    undefined,
  );
}
export function captureCohortReview(
  kind: "seed" | "refresh",
  evidence: string,
  requestId: string,
  pair?: CohortPairView,
): CohortReview {
  const reason = evidence.trim();
  if (reason.length < 10 || reason.length > 1000)
    throw new Error("Enter a review note between 10 and 1,000 characters.");
  const reviewed: ReviewedResult[] = [];
  if (kind === "refresh") {
    if (!pair)
      throw new Error(
        "Reload the saved cohort pair before reviewing a source refresh.",
      );
    for (const side of ["BASELINE_SYNTHETIC", "AFTER_SYNTHETIC"] as const) {
      const cohort = pair.cohorts.find((row) => row.side === side);
      const result = cohort && latestResult(cohort.results);
      if (!result)
        throw new Error(
          "Both saved result versions are required for a source refresh.",
        );
      reviewed.push({
        id: result.id,
        version: result.version,
        side,
        as_of_at: result.as_of_at,
      });
    }
  }
  return {
    kind,
    request_id: requestId,
    evidence: reason,
    pair_id: kind === "refresh" ? pair!.id : null,
    expected_result_ids: reviewed.map((row) => row.id),
    reviewed,
  };
}
export function readPendingCohortReview(
  raw: string | null,
): PendingCohortReview | null {
  if (!raw) return null;
  try {
    const item = JSON.parse(raw) as PendingCohortReview;
    const review = item.review;
    if (
      !["captured", "uncertain", "conflict"].includes(item.phase) ||
      !review ||
      !["seed", "refresh"].includes(review.kind) ||
      typeof review.request_id !== "string" ||
      typeof review.evidence !== "string" ||
      review.evidence.length < 10 ||
      review.evidence.length > 1000 ||
      !Array.isArray(review.reviewed) ||
      !Array.isArray(review.expected_result_ids) ||
      !review.expected_result_ids.every((id) => typeof id === "string") ||
      review.reviewed.some(
        (row) =>
          !row ||
          typeof row.id !== "string" ||
          typeof row.version !== "number" ||
          typeof row.side !== "string" ||
          typeof row.as_of_at !== "string",
      ) ||
      (review.kind === "seed" &&
        (review.pair_id !== null ||
          review.expected_result_ids.length !== 0 ||
          review.reviewed.length !== 0)) ||
      (review.kind === "refresh" &&
        (typeof review.pair_id !== "string" ||
          review.expected_result_ids.length !== 2 ||
          review.reviewed.length !== 2 ||
          review.reviewed.some(
            (row, index) =>
              row.id !== review.expected_result_ids[index] ||
              !Number.isInteger(row.version) ||
              row.version < 1 ||
              row.side !== ["BASELINE_SYNTHETIC", "AFTER_SYNTHETIC"][index],
          ) ||
          review.expected_result_ids[0] === review.expected_result_ids[1]))
    )
      return null;
    return item;
  } catch {
    return null;
  }
}
export class CohortCommandError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}
const base = (workspace: string) =>
  `/api/workspaces/${encodeURIComponent(workspace)}/impact/cohorts`;
function exact(value: unknown, decimal = false) {
  if (
    value !== null &&
    (typeof value !== "string" ||
      !(decimal ? /^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/ : /^-?\d+$/).test(value))
  )
    throw new Error(
      "Cohort source evidence requires exact decimal strings; refresh after the service precision is corrected.",
    );
}
function validate(value: CohortListView): CohortListView {
  for (const pair of value.pairs) {
    [
      pair.recovery_change_pp,
      pair.recovery_relative_change_percent,
      pair.paid_in_full_change_pp,
    ].forEach((v) => exact(v, true));
    for (const cohort of pair.cohorts) {
      for (const member of cohort.members) {
        exact(member.opening_minor);
        exact(member.original_minor);
      }
      for (const result of cohort.results) {
        [
          result.opening_minor,
          result.raw_net_minor,
          result.recovery_numerator_minor,
          result.refunds_minor,
          result.reversals_minor,
        ].forEach((v) => exact(v));
        [
          result.recovery_percent,
          result.paid_in_full_percent,
          result.days_to_collect,
          result.days_beyond_terms,
        ].forEach((v) => exact(v, true));
      }
    }
  }
  return value;
}
async function request(
  path: string,
  signal?: AbortSignal,
  review?: CohortReview,
  csrf?: string,
): Promise<CohortListView> {
  const response = await fetch(path, {
    method: review ? "POST" : "GET",
    credentials: "same-origin",
    cache: "no-store",
    signal,
    ...(review
      ? {
          headers: {
            "content-type": "application/json",
            "x-csrf-token": csrf ?? "",
          },
          body: JSON.stringify({
            request_id: review.request_id,
            evidence: review.evidence,
            ...(review.kind === "refresh"
              ? { expected_result_ids: review.expected_result_ids }
              : {}),
          }),
        }
      : {}),
  });
  if (!response.ok)
    throw new CohortCommandError(
      response.status === 401
        ? "Sign in again to read or review cohort source evidence."
        : response.status === 403 || response.status === 404
          ? "Cohort source actions are unavailable to your current workspace membership."
          : response.status === 409
            ? "The reviewed cohort source or results changed. Reload saved results and explicitly review a new source action."
            : response.status === 422
              ? "Review the note and both captured result references before submitting."
              : "The source action could not be confirmed. Reload saved results or retry the same captured request.",
      response.status,
    );
  return validate((await response.json()) as CohortListView);
}
export const cohortApi = {
  read: (workspace: string, signal?: AbortSignal) =>
    request(base(workspace), signal),
  submit: (workspace: string, review: CohortReview, csrf: string) =>
    request(
      review.kind === "seed"
        ? `${base(workspace)}/synthetic`
        : `${base(workspace)}/${encodeURIComponent(review.pair_id!)}/refresh`,
      undefined,
      review,
      csrf,
    ),
};
