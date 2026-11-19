import type { VoiceJoinView } from "@/lib/generated-api";

type ExistingCall = {
  id: string;
  webCallUrl: string;
  artifactPlan: { videoRecordingEnabled: false };
};
export type BrowserVoiceClient = {
  reconnect(
    call: ExistingCall,
    options: { clientSideNoiseCancellationEnabled: false },
  ): Promise<void>;
  stop(): Promise<void>;
  on(event: "call-end" | "error", callback: () => void): unknown;
};
const createClient = async (): Promise<BrowserVoiceClient> => {
  const { default: Vapi } = await import("@vapi-ai/web");
  // Empty API token: the browser can only join the already-created private call.
  return new Vapi("", undefined, {}, { startAudioOff: false });
};

export async function connectExistingVoice(
  saved: VoiceJoinView,
  signal: AbortSignal,
  ended: () => void,
  failed: () => void,
  factory: () => Promise<BrowserVoiceClient> = createClient,
): Promise<{ disconnect(): Promise<void> }> {
  const url = new URL(saved.web_call_url);
  if (
    url.protocol !== "https:" ||
    url.hostname !== "vapi.daily.co" ||
    url.port ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    !/^\/[A-Za-z0-9_-]+$/.test(url.pathname) ||
    Date.parse(saved.expires_at) <= Date.now() ||
    !Number.isFinite(Date.parse(saved.expires_at))
  )
    throw new Error(
      "The saved browser capability is unavailable. Refresh the session.",
    );
  if (signal.aborted) throw new Error("Browser join cancelled.");
  const sdk = await factory();
  let disconnected = false;
  async function disconnect() {
    if (disconnected) return;
    disconnected = true;
    signal.removeEventListener("abort", abort);
    await sdk.stop();
  }
  const abort = () => {
    void disconnect().catch(() => {});
  };
  signal.addEventListener("abort", abort, { once: true });
  sdk.on("call-end", ended);
  sdk.on("error", failed);
  try {
    if (signal.aborted || Date.parse(saved.expires_at) <= Date.now())
      throw new Error("Browser join cancelled.");
    await sdk.reconnect(
      {
        id: saved.provider_call_id,
        webCallUrl: saved.web_call_url,
        artifactPlan: { videoRecordingEnabled: false },
      },
      { clientSideNoiseCancellationEnabled: false },
    );
    if (signal.aborted) {
      // A join already pending at unmount must not retain microphone/audio afterward.
      await sdk.stop();
      throw new Error("Browser join cancelled.");
    }
    return { disconnect };
  } catch {
    await disconnect().catch(() => {});
    throw new Error(
      "Browser connection is not confirmed. Refresh the saved session before retrying.",
    );
  }
}
