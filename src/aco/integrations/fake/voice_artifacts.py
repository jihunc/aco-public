"""Fixed synthetic originals, never customer audio or a claimed Vapi recording capability."""

import io
import json
import wave
from uuid import uuid5

from aco.integrations.ports.voice_artifacts import VoiceArtifactAuthority, VoiceArtifactRequest, VoiceOriginal


class SyntheticVoiceArtifacts:
    async def fetch(self, request: VoiceArtifactRequest, authorize: VoiceArtifactAuthority) -> VoiceOriginal:
        await authorize(request)
        if request.kind == "transcript":
            body = json.dumps(
                {
                    "synthetic": True,
                    "evidence_mode": "FAKE",
                    "turns": [
                        {
                            "speaker": "synthetic_assistant",
                            "offset_ms": 0,
                            "text": "Synthetic privacy fixture. This is not a customer recording.",
                        },
                        {
                            "speaker": "synthetic_participant",
                            "offset_ms": 500,
                            "text": "This fixed fixture verifies explicitly permitted transcript storage.",
                        },
                    ],
                },
                separators=(",", ":"),
            ).encode()
            content_type = "application/json"
        else:
            output = io.BytesIO()
            with wave.open(output, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b"\0" * 16000)
            body, content_type = output.getvalue(), "audio/wav"
        return VoiceOriginal(
            request=request,
            reference=str(uuid5(request.session_id, "synthetic-original:" + request.kind)),
            kind=request.kind,
            content_type=content_type,
            body=body,
            captured_at=request.captured_at,
        )

    async def purge(self, request: VoiceArtifactRequest, authorize: VoiceArtifactAuthority) -> str:
        await authorize(request)
        # This adapter has no remote storage. The application persists this terminal receipt and
        # refuses future fetches using its durable artifact tombstone, including after restart.
        return "synthetic-no-remote-retention:" + str(uuid5(request.session_id, request.kind))
