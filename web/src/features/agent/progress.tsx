"use client";
import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { agentPath } from "./api";
import { observeAgentEvents } from "./stream";
export function AgentProgress({
  workspace,
  runId,
  watermark,
}: {
  workspace: string;
  runId: string;
  watermark: number;
}) {
  const cache = useQueryClient();
  const [initialWatermark] = useState(watermark);
  const [connection, setConnection] = useState<
    "connecting" | "connected" | "polling"
  >("connecting");
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    return observeAgentEvents(
      `${agentPath(workspace, runId)}/events`,
      runId,
      initialWatermark,
      () => {
        void cache.invalidateQueries({
          queryKey: ["agent-run", workspace, runId],
        });
        void cache.invalidateQueries({
          queryKey: ["agent-steps", workspace, runId],
        });
      },
      setConnection,
    );
  }, [cache, workspace, runId, initialWatermark]);
  return (
    <p className="muted agent-refresh-note" role="status">
      {connection === "connected"
        ? "Live updates connected."
        : connection === "polling"
          ? "Live connection interrupted; saved progress is still refreshing."
          : "Connecting live updates."}{" "}
      Saved progress also refreshes every two seconds. Closing this page leaves
      the worker running.
    </p>
  );
}
