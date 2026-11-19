"use client";
import { useEffect, useRef, useState } from "react";
import { connectExistingVoice } from "./voice-browser";
import {
  captureVoice,
  sameVoiceSnapshot,
  voiceApi,
  voiceStartCommand,
  type VoiceSessionView,
} from "./voice-api";

export function VoiceLiveControls({
  session,
  workspace,
  csrf,
  fresh,
  refresh,
}: {
  session: VoiceSessionView;
  workspace: string;
  csrf: string;
  fresh: boolean;
  refresh: () => Promise<unknown>;
}) {
  const [joinReview, setJoinReview] = useState<VoiceSessionView | null>(null);
  const [endReview, setEndReview] = useState<VoiceSessionView | null>(null);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [connected, setConnected] = useState(false);
  const connection = useRef<{ disconnect(): Promise<void> } | null>(null);
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      controller.current?.abort();
      void connection.current?.disconnect().catch(() => {});
    };
  }, [session.id]);
  useEffect(() => {
    if (["completed", "failed", "unknown"].includes(session.state)) {
      controller.current?.abort();
      void connection.current?.disconnect().catch(() => {});
      connection.current = null;
    }
  }, [session.state]);
  const reviewedJoin = !!joinReview && sameVoiceSnapshot(joinReview, session);
  const reviewedEnd = !!endReview && sameVoiceSnapshot(endReview, session);
  async function leave() {
    controller.current?.abort();
    await connection.current?.disconnect().catch(() => {});
    connection.current = null;
    if (mounted.current) setConnected(false);
  }
  async function join() {
    if (
      !fresh ||
      busy ||
      connected ||
      !session.can_join ||
      !reviewedJoin ||
      !joinReview
    )
      return;
    setBusy(true);
    setError("");
    const active = new AbortController();
    controller.current = active;
    try {
      const saved = await voiceApi.join(
        workspace,
        session.id,
        voiceStartCommand(joinReview),
        csrf,
      );
      connection.current = await connectExistingVoice(
        saved,
        active.signal,
        () => {
          if (mounted.current) setConnected(false);
          void refresh();
        },
        () => {
          if (mounted.current)
            setError(
              "Browser audio is unavailable. Leave the browser connection and refresh the saved session.",
            );
        },
      );
      if (mounted.current) setConnected(true);
    } catch (reason) {
      if (mounted.current)
        setError(
          reason instanceof Error
            ? reason.message
            : "Browser join is not confirmed.",
        );
    } finally {
      if (mounted.current) {
        setJoinReview(null);
        setBusy(false);
        await refresh();
      }
    }
  }
  async function end() {
    if (!fresh || busy || !session.can_end || !reviewedEnd || !endReview)
      return;
    setBusy(true);
    setError("");
    try {
      await voiceApi.end(
        workspace,
        session.id,
        voiceStartCommand(endReview),
        csrf,
      );
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "The stop result is not confirmed. Refresh before any further action.",
      );
    } finally {
      await leave();
      setEndReview(null);
      await refresh();
      if (mounted.current) setBusy(false);
    }
  }
  return (
    <section className="panel" aria-label="Browser call controls">
      <h3>Browser call controls</h3>
      <p>
        Only the saved participant can join this existing call. Joining uses
        your microphone; recording and transcript capture remain disabled.
        Leaving browser audio does not prove provider completion.
      </p>
      <p>Saved participant: {session.participant_email ?? "Unavailable"}</p>
      {session.can_join && !connected && (
        <>
          <label>
            <input
              type="checkbox"
              checked={reviewedJoin}
              disabled={busy || !fresh}
              onChange={(e) =>
                setJoinReview(e.target.checked ? captureVoice(session) : null)
              }
            />
            I am the saved participant and agree to join this reviewed browser
            call with my microphone, capture off.
          </label>
          <button
            type="button"
            disabled={busy || !fresh || !reviewedJoin}
            onClick={join}
          >
            {busy ? "Connecting…" : "Join existing browser call"}
          </button>
        </>
      )}
      {!session.can_join && (
        <p>
          Join unavailable:{" "}
          {(session.join_reasons ?? [])
            .map((r) => r.replaceAll("_", " "))
            .join(", ") || "Current server checks do not allow joining."}
        </p>
      )}
      {connected &&
        !["completed", "failed", "unknown"].includes(session.state) && (
          <>
            <p role="status">Browser audio connected.</p>
            <button type="button" onClick={leave}>
              Leave browser audio
            </button>
          </>
        )}
      {session.can_end && (
        <>
          <label>
            <input
              type="checkbox"
              checked={reviewedEnd}
              disabled={busy || !fresh}
              onChange={(e) =>
                setEndReview(e.target.checked ? captureVoice(session) : null)
              }
            />
            I reviewed this saved call and request its one bounded safety stop.
          </label>
          <button
            type="button"
            disabled={busy || !fresh || !reviewedEnd}
            onClick={end}
          >
            Request end call
          </button>
        </>
      )}
      {!session.can_end && (
        <p>
          End request unavailable:{" "}
          {(session.end_reasons ?? [])
            .map((r) => r.replaceAll("_", " "))
            .join(", ") || "No current stop authority."}
        </p>
      )}
      {((joinReview && !reviewedJoin) || (endReview && !reviewedEnd)) && (
        <p role="alert">
          The saved call changed. Review the current version again.
        </p>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
