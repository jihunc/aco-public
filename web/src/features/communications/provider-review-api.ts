import type { ProviderEmailConfiguration, ReviewProviderEmailExecution } from "@/lib/generated-api";

export function captureProviderReview(configuration: ProviderEmailConfiguration): ProviderEmailConfiguration {
  return structuredClone(configuration);
}
export function providerReviewCommand(reviewed: ProviderEmailConfiguration, evidence: string): ReviewProviderEmailExecution {
  if (!evidence.trim()) throw new Error("Record your review evidence before saving.");
  return { connection_id: reviewed.connection_id, expected_activation_id: reviewed.expected_activation_id,
    reviewed_hash: reviewed.reviewed_hash, evidence: evidence.trim() };
}
export function providerReviewIsCurrent(reviewed: ProviderEmailConfiguration | null, current: ProviderEmailConfiguration[]) {
  return !!reviewed && current.some(candidate => candidate.connection_id === reviewed.connection_id && candidate.reviewed_hash === reviewed.reviewed_hash);
}
