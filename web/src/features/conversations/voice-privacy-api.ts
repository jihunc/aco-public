import { readApi } from "@/lib/api";
import type {
  ArtifactPrivacyView,
  CaptureScopes,
  PrepareConsentStorage,
  PrivacyCommand,
  RecordVoiceConsent,
  RevokeVoiceConsent,
  VoiceConsentPage,
  VoiceConsentView,
  VoiceSessionView,
} from "@/lib/generated-api";
export const emptyCaptureScopes = (): CaptureScopes => ({
  audio_capture: false,
  transcript: false,
  private_storage: false,
  playback: false,
  provider_retention: false,
});
export const samePrivacyReview = (
  a: ArtifactPrivacyView,
  b: ArtifactPrivacyView,
) =>
  a.artifact.id === b.artifact.id &&
  a.version === b.version &&
  a.legal_hold_state === b.legal_hold_state &&
  JSON.stringify(a.events) === JSON.stringify(b.events);
export type TranscriptTurn = {
  speaker: string;
  offset_ms: number;
  text: string;
};
export function transcriptTurns(value: unknown): TranscriptTurn[] {
  if (
    !value ||
    typeof value !== "object" ||
    !("synthetic" in value) ||
    value.synthetic !== true ||
    !("evidence_mode" in value) ||
    value.evidence_mode !== "FAKE" ||
    !("turns" in value) ||
    !Array.isArray(value.turns)
  )
    throw Error("Private transcript format unavailable");
  return value.turns.map((turn: unknown) => {
    if (
      !turn ||
      typeof turn !== "object" ||
      !("speaker" in turn) ||
      typeof turn.speaker !== "string" ||
      !("text" in turn) ||
      typeof turn.text !== "string" ||
      !("offset_ms" in turn) ||
      typeof turn.offset_ms !== "number" ||
      !Number.isSafeInteger(turn.offset_ms) ||
      turn.offset_ms < 0
    )
      throw Error("Private transcript format unavailable");
    return {
      speaker: turn.speaker,
      offset_ms: turn.offset_ms,
      text: turn.text,
    };
  });
}
export const privatePath = (workspace: string, id: string) =>
  `/api/workspaces/${encodeURIComponent(workspace)}/artifacts/${encodeURIComponent(id)}`;
const base = (workspace: string) =>
  `/api/workspaces/${encodeURIComponent(workspace)}/voice-consents`;
async function post<T>(url: string, command: object, csrf: string): Promise<T> {
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "content-type": "application/json", "x-csrf-token": csrf },
    body: JSON.stringify(command),
  });
  if (!response.ok)
    throw Error(
      response.status === 409
        ? "The saved privacy scope changed. Refresh and explicitly review it again."
        : response.status === 403
          ? "Current permission or consent does not allow this action."
          : "Privacy action is not confirmed. Refresh saved history before trying again.",
    );
  return response.json();
}
export const voicePrivacyApi = {
  list: (workspace: string, signal?: AbortSignal) =>
    readApi<VoiceConsentPage>(base(workspace), signal),
  setup: (workspace: string, command: PrepareConsentStorage, csrf: string) =>
    post<VoiceConsentPage>(`${base(workspace)}/storage/setup`, command, csrf),
  record: (workspace: string, command: RecordVoiceConsent, csrf: string) =>
    post<VoiceConsentView>(base(workspace), command, csrf),
  revoke: (
    workspace: string,
    id: string,
    command: RevokeVoiceConsent,
    csrf: string,
  ) =>
    post<VoiceConsentView>(
      `${base(workspace)}/${encodeURIComponent(id)}/revoke`,
      command,
      csrf,
    ),
  reconcile: (workspace: string, id: string, csrf: string) =>
    post<VoiceSessionView>(
      `/api/workspaces/${encodeURIComponent(workspace)}/voice-sessions/${encodeURIComponent(id)}/reconcile`,
      {},
      csrf,
    ),
  privacy: (workspace: string, id: string, signal?: AbortSignal) =>
    readApi<ArtifactPrivacyView>(
      `${privatePath(workspace, id)}/privacy`,
      signal,
    ),
  disposition: (
    workspace: string,
    id: string,
    command: PrivacyCommand,
    csrf: string,
  ) =>
    post<ArtifactPrivacyView>(
      `${privatePath(workspace, id)}/privacy`,
      command,
      csrf,
    ),
  transcript: async (
    workspace: string,
    id: string,
  ): Promise<TranscriptTurn[]> => {
    const response = await fetch(`${privatePath(workspace, id)}/content`, {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok)
      throw Error(
        "Private transcript is unavailable or its access was revoked.",
      );
    return transcriptTurns(await response.json());
  },
};
