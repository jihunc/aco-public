import { readApi } from "@/lib/api";
import type {
  PrepareVoice,
  StartVoice,
  VoicePage,
  VoiceJoinView,
  VoiceSessionView,
} from "@/lib/generated-api";
export type { PrepareVoice, VoiceSessionView } from "@/lib/generated-api";
export const captureVoice = (session: VoiceSessionView): VoiceSessionView =>
  structuredClone(session);
export function sameVoiceSnapshot(
  a: VoiceSessionView,
  b: VoiceSessionView,
): boolean {
  return (
    a.id === b.id &&
    a.version === b.version &&
    a.approval_id === b.approval_id &&
    a.connection_id === b.connection_id &&
    a.objective === b.objective &&
    a.demo_scenario === b.demo_scenario &&
    a.expires_at === b.expires_at &&
    a.evidence_mode === b.evidence_mode &&
    a.participant_id === b.participant_id &&
    a.participant_email === b.participant_email &&
    JSON.stringify(a.consent) === JSON.stringify(b.consent) &&
    JSON.stringify(a.invoice_ids) === JSON.stringify(b.invoice_ids) &&
    JSON.stringify(a.source_scope) === JSON.stringify(b.source_scope)
  );
}
export function canStartVoice(
  reviewed: VoiceSessionView | null,
  current: VoiceSessionView | undefined,
  fresh: boolean,
): boolean {
  return (
    !!reviewed &&
    !!current &&
    fresh &&
    current.can_start === true &&
    sameVoiceSnapshot(reviewed, current)
  );
}
export const voiceStartCommand = (reviewed: VoiceSessionView): StartVoice => ({
  expected_version: reviewed.version,
});
const path = (workspace: string) =>
  `/api/workspaces/${encodeURIComponent(workspace)}/voice-sessions`;
async function post<T>(
  url: string,
  command: PrepareVoice | StartVoice | { request_id: string },
  csrf: string,
): Promise<T> {
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "content-type": "application/json", "x-csrf-token": csrf },
    body: JSON.stringify(command),
  });
  if (response.status === 422) {
    const detail = (await response.json().catch(() => null)) as {
      detail?: unknown;
    } | null;
    if (detail?.detail === "VOICE_FIXED_FACTS_REQUIRES_AUD40_OUTSTANDING")
      throw new Error(
        "This fixed synthetic example requires a primary AUD invoice with at least AUD 40 outstanding.",
      );
  }
  if (!response.ok)
    throw new Error(
      response.status === 409
        ? "The saved session or its authority changed. Refresh and review the current session before trying again."
        : response.status === 403
          ? "Current server checks do not permit this session. Review its restrictions."
          : response.status === 401
            ? "Sign in again before continuing."
            : response.status === 422
              ? "Choose one to three invoices, a connection and an objective of up to 1,000 characters."
              : "The result is not confirmed. Refresh saved sessions before trying again.",
    );
  return response.json();
}
export const voiceApi = {
  setup: (workspace: string, command: { request_id: string }, csrf: string) =>
    post<VoicePage>(`${path(workspace)}/setup`, command, csrf),
  list: (workspace: string, signal?: AbortSignal) =>
    readApi<VoicePage>(path(workspace), signal),
  get: (workspace: string, id: string, signal?: AbortSignal) =>
    readApi<VoiceSessionView>(
      `${path(workspace)}/${encodeURIComponent(id)}`,
      signal,
    ),
  prepare: (workspace: string, command: PrepareVoice, csrf: string) =>
    post<VoiceSessionView>(path(workspace), command, csrf),
  join: (workspace: string, id: string, command: StartVoice, csrf: string) =>
    post<VoiceJoinView>(
      `${path(workspace)}/${encodeURIComponent(id)}/join`,
      command,
      csrf,
    ),
  end: (workspace: string, id: string, command: StartVoice, csrf: string) =>
    post<VoiceSessionView>(
      `${path(workspace)}/${encodeURIComponent(id)}/end`,
      command,
      csrf,
    ),
  start: (workspace: string, id: string, command: StartVoice, csrf: string) =>
    post<VoiceSessionView>(
      `${path(workspace)}/${encodeURIComponent(id)}/start`,
      command,
      csrf,
    ),
};
