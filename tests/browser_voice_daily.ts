/** Fixed external Daily/microphone double, imported ONLY by the test-only Next server. */
declare global {
  interface Window {
    __acoVoiceDaily: {
      objects: number;
      joins: number;
      destroys: number;
      recordings: number;
    };
  }
}
const observed = () =>
  (window.__acoVoiceDaily ??= {
    objects: 0,
    joins: 0,
    destroys: 0,
    recordings: 0,
  });
export default {
  createCallObject(options: { videoSource?: boolean }) {
    if (options.videoSource !== false)
      throw Error("Video must remain disabled");
    observed().objects++;
    return {
      iframe() {
        return null;
      },
      on() {
        return this;
      },
      once() {
        return this;
      },
      async join(options: {
        url: string;
        subscribeToTracksAutomatically: boolean;
      }) {
        if (
          options.url !==
            "https://vapi.daily.co/synthetic-owned-browser-room" ||
          options.subscribeToTracksAutomatically !== false
        )
          throw Error("Unexpected external Daily fixture capability");
        observed().joins++;
      },
      async destroy() {
        observed().destroys++;
      },
      startRemoteParticipantsAudioLevelObserver() {},
      async updateInputSettings() {},
      startRecording() {
        observed().recordings++;
        throw Error("Capture forbidden");
      },
      async stopRecording() {
        observed().recordings++;
        throw Error("Capture forbidden");
      },
    };
  },
};
