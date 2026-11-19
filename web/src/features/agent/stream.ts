import type { AgentEvent } from "./api";
const families = [
  "queued",
  "running",
  "step.started",
  "step.completed",
  "step.failed",
  "approval_required",
  "completed",
  "partially_completed",
  "failed",
  "cancelled",
];
/** Native EventSource retains Last-Event-ID during reconnect. The initial replay
 * is discarded through the consistent GET watermark, closing the fetch/subscribe gap. */
export function observeAgentEvents(
  url: string,
  runId: string,
  watermark: number,
  onAdvance: (event: AgentEvent) => void,
  onConnection: (state: "connected" | "polling") => void,
): () => void {
  const source = new EventSource(url, { withCredentials: true });
  let cursor = watermark;
  let closed = false;
  source.onopen = () => {
    if (!closed) onConnection("connected");
  };
  source.onerror = () => {
    if (!closed) onConnection("polling");
  };
  function receive(message: MessageEvent<string>) {
    if (closed) return;
    try {
      const event: AgentEvent = JSON.parse(message.data);
      if (
        event.run_id !== runId ||
        !Number.isSafeInteger(event.event_sequence) ||
        event.event_sequence <= cursor ||
        event.id !== `${runId}:${event.event_sequence}` ||
        (message.lastEventId && message.lastEventId !== event.id)
      )
        return;
      cursor = event.event_sequence;
      onAdvance(event);
    } catch {
      /* A malformed notification cannot replace persisted state. Polling continues. */
    }
  }
  for (const family of families) {
    source.addEventListener(family, receive as EventListener);
    source.addEventListener(`agent.${family}`, receive as EventListener);
  }
  source.onmessage = receive;
  return () => {
    closed = true;
    source.close();
  };
}
