"""
live_concall_stt.py — pluggable real-time speech-to-text for the Live Concall
Stream applet (Tier 2). Currently backed by Deepgram, configured for linear16 /
8 kHz mono to match Exotel's raw PCM media frames.

Kept in a separate module and imported lazily/guardedly by live_concall.py so the
telephony flow (dial, passcode, recording, post-call transcript) never depends on
STT being installed or working.

To swap providers (e.g. Azure AI Speech), implement the same tiny interface:

    stream = open_stt_stream(on_transcript)   # on_transcript(text: str, final: bool)
    stream.send(pcm_bytes)                     # 16-bit signed mono PCM @ 8 kHz
    stream.finish()
"""

import os
import sys


class _DeepgramStream:
    """Thin wrapper over a Deepgram live-transcription WebSocket connection."""

    def __init__(self, on_transcript):
        self._on = on_transcript
        self._conn = None

    def start(self):
        from deepgram import (DeepgramClient, LiveOptions,
                              LiveTranscriptionEvents)

        api_key = os.getenv("DEEPGRAM_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPGRAM_API_KEY not set")

        client = DeepgramClient(api_key)
        conn = client.listen.websocket.v("1")

        def _on_message(*args, **kwargs):
            # Deepgram SDK v3 passes the result either as a kwarg or positionally.
            result = kwargs.get("result")
            if result is None:
                for a in args:
                    if hasattr(a, "channel"):
                        result = a
                        break
            if result is None:
                return
            try:
                text = (result.channel.alternatives[0].transcript or "").strip()
                if text:
                    self._on(text, bool(getattr(result, "is_final", False)))
            except Exception:
                pass

        conn.on(LiveTranscriptionEvents.Transcript, _on_message)

        options = LiveOptions(
            model="nova-2",
            language="en-IN",      # Indian English (Deepgram degrades gracefully)
            encoding="linear16",   # Exotel streams raw 16-bit signed PCM
            sample_rate=8000,
            channels=1,
            punctuate=True,
            interim_results=True,
            smart_format=True,
        )
        if not conn.start(options):
            raise RuntimeError("Deepgram connection failed to start")
        self._conn = conn
        print("LIVE_CONCALL_STT: Deepgram stream started", file=sys.stderr)
        return self

    def send(self, audio_bytes):
        if self._conn is not None and audio_bytes:
            self._conn.send(audio_bytes)

    def finish(self):
        if self._conn is not None:
            try:
                self._conn.finish()
            finally:
                self._conn = None


def open_stt_stream(on_transcript):
    """
    Open a real-time STT stream. `on_transcript(text, is_final)` is invoked for
    each interim/final result. Returns an object exposing .send(bytes)/.finish().
    Raises if the STT backend is unavailable (caller handles it defensively).
    """
    return _DeepgramStream(on_transcript).start()
