"""AssemblyAI v3 realtime streaming client, shaped for a Twilio phone line.

    wss://streaming.assemblyai.com/v3/ws

Four things about this endpoint drove the design, and all four are worth stating
because each one is a place the obvious implementation is wrong:

1.  ARABIC ONLY EXISTS ON ONE MODEL. `speech_model=universal-3-5-pro` with
    `language_codes=["ar"]`. The default `universal-streaming` model has no Arabic
    at all, so leaving the model unset silently gives you an English-only socket
    that will happily return confident nonsense for Arabic speech.

2.  AUTH HAS NO `Bearer` PREFIX. The header is `Authorization: <key>` bare. This
    differs from AssemblyAI's own Voice Agent API, which DOES use Bearer - so
    copying an auth snippet from the wrong page in their docs produces a 4001
    close that reads like a bad key rather than a bad header.

3.  IT TAKES MU-LAW DIRECTLY. `encoding=pcm_mulaw&sample_rate=8000` is exactly what
    Twilio Media Streams sends. We forward the caller's frames byte-for-byte. No
    decode, no resample, no int16 round trip. The existing local-Whisper path had
    to upsample 8k->16k; this one must NOT, and doing it anyway would both cost
    latency and hand the model audio it did not ask for.

4.  BILLING IS BY CONNECTION DURATION, NOT AUDIO VOLUME. An idle open socket costs
    money. So `close()` is not optional hygiene, the reconnect logic has a bounded
    retry budget rather than an infinite loop, and there is an `idle_timeout_s`
    that shuts the socket if no audio has been forwarded for a while.
    The free tier also caps NEW connections at 5/minute, which is why reconnects
    back off instead of hammering.

-------------------------------------------------------------------------------
NOT A DROP-IN FOR call_agent/stt_stream.py - READ THIS BEFORE WIRING IT UP
-------------------------------------------------------------------------------
The existing local STT is REQUEST/RESPONSE OVER A BUFFER:

    await transcriber.quick(pcm8k_numpy_array) -> str      # poll, ~every 150 ms
    await transcriber.careful(pcm8k_numpy_array) -> str

The caller owns a growing audio buffer and asks "what is in it now?" on a loop.

This client is the opposite shape: a PERSISTENT SOCKET WITH CALLBACKS. You push
frames in as they arrive and transcripts arrive whenever the service decides. The
partial-transcript poll loop in call_agent/server.py (Call.partials()) has to become
a subscriber rather than a poller. That is the real integration work and it is not a
signature-level swap.

`TranscriberAdapter` at the bottom of this file bridges the two so the existing code
can be migrated incrementally: it subscribes to this client and maintains a
last-known-transcript string, then exposes the old `quick()`/`careful()` signatures
reading off that string. BE HONEST ABOUT WHAT THAT ADAPTER IS: it makes the old call
sites compile and run, it does NOT make them correct. `careful()` in particular
cannot do what its name promises - there is no second, more accurate pass to run,
so it waits for end-of-turn and returns the final transcript. The numpy array
passed to the adapter's methods is IGNORED, because the audio already went to the
socket via feed(). Anything that relies on re-transcribing an arbitrary buffer after
the fact has to be redesigned, not adapted.
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

# websockets >= 13 ships the asyncio implementation under this path.
# Verified present in both interpreters on this machine: system python3.14
# (websockets 16.0) and call_agent/.venv (17.0.1).
try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError as exc:  # pragma: no cover - environment problem, not logic
    raise SystemExit(
        "aai_stream needs the `websockets` package (>=13, for websockets.asyncio).\n"
        f"Import failed under {sys.executable}: {exc}\n"
        "Install it, or run with call_agent/.venv/bin/python which already has it."
    ) from exc

# ---------------------------------------------------------------------------
# Constants. Anything here that is a claim about AssemblyAI's API rather than a
# choice of ours is marked, because none of it has been confirmed against a live
# socket - there is no account yet. See NOTES.md.
# ---------------------------------------------------------------------------

ENDPOINT = "wss://streaming.assemblyai.com/v3/ws"        # API fact
SPEECH_MODEL = "universal-3-5-pro"                       # API fact: only model with Arabic
ENCODING = "pcm_mulaw"                                   # API fact: accepted directly
SAMPLE_RATE = 8000                                       # Twilio line rate
FRAME_BYTES = 160                                        # 20 ms of 8 kHz mu-law

KEY_PATH = Path.home() / "jarvis" / ".credentials" / "assemblyai_key"

# Our choices, not the API's.
DEFAULT_IDLE_TIMEOUT_S = 30.0    # shut an unused socket; billing is per connection-second
DEFAULT_MAX_RETRIES = 4
RETRY_BACKOFF_S = (0.5, 2.0, 6.0, 15.0)   # free tier allows 5 new connections/min


class AssemblyAIKeyMissing(RuntimeError):
    """Raised instead of letting a FileNotFoundError traceback escape."""


def load_key(path: Path | str | None = None, *, env_first: bool = True) -> str:
    """Return the API key, or raise with something a human can act on.

    Order: ASSEMBLYAI_API_KEY env var, then the credentials file. The env var wins
    so a test run can use a throwaway key without touching the real credentials.
    """
    if env_first:
        env = os.environ.get("ASSEMBLYAI_API_KEY", "").strip()
        if env:
            return env

    p = Path(path) if path else KEY_PATH
    if not p.exists():
        raise AssemblyAIKeyMissing(
            f"No AssemblyAI API key.\n"
            f"  Looked for: {p}\n"
            f"  And the env var ASSEMBLYAI_API_KEY (unset or empty).\n"
            f"\n"
            f"To fix:\n"
            f"  1. Create an account at https://www.assemblyai.com/ and copy the API key.\n"
            f"  2. mkdir -p {p.parent}\n"
            f"  3. printf %s 'YOUR_KEY' > {p}\n"
            f"  4. chmod 600 {p}\n"
            f"\n"
            f"No key is needed to run the replay tests:  python3 selftest.py"
        )
    key = p.read_text(encoding="utf-8").strip()
    if not key:
        raise AssemblyAIKeyMissing(f"{p} exists but is empty. Put the API key in it.")
    return key


def build_url(
    *,
    endpoint: str = ENDPOINT,
    language: str = "ar",
    speech_model: str = SPEECH_MODEL,
    sample_rate: int = SAMPLE_RATE,
    encoding: str = ENCODING,
    format_turns: bool | None = None,
    end_of_turn_confidence_threshold: float | None = None,
    extra: dict | None = None,
) -> str:
    """Compose the v3 websocket URL.

    `format_turns` defaults to None = do not send the parameter at all. AssemblyAI
    documents it as NOT available on universal-3-5-pro, so sending it is at best
    ignored and at worst a 400. Whether Arabic comes back punctuated is therefore
    UNKNOWN and is one of the first things to test on a live key.
    """
    q: dict[str, str] = {
        "sample_rate": str(sample_rate),
        "encoding": encoding,
        "speech_model": speech_model,
    }
    if language:
        # v3 takes a JSON array for language_codes.
        q["language_codes"] = json.dumps([language], separators=(",", ":"))
    if format_turns is not None:
        q["format_turns"] = "true" if format_turns else "false"
    if end_of_turn_confidence_threshold is not None:
        q["end_of_turn_confidence_threshold"] = str(end_of_turn_confidence_threshold)
    if extra:
        q.update({k: str(v) for k, v in extra.items()})
    return endpoint + "?" + urllib.parse.urlencode(q)


# ---------------------------------------------------------------------------
# Transcript model
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    """One `Turn` message off the v3 socket.

    v3 sends the SAME turn repeatedly as it grows - each message carries the full
    transcript of the turn so far, not a delta. `end_of_turn` flips true on the last
    one. So a consumer should REPLACE its current turn text, never append, or every
    partial concatenates into a stutter.
    """
    turn_order: int = 0
    transcript: str = ""
    end_of_turn: bool = False
    turn_is_formatted: bool = False
    end_of_turn_confidence: float = 0.0
    words: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    # Local clock, set on arrival. Not from the service.
    received_at: float = 0.0

    @property
    def is_partial(self) -> bool:
        return not self.end_of_turn

    @classmethod
    def from_message(cls, m: dict) -> "Turn":
        return cls(
            turn_order=int(m.get("turn_order", 0)),
            transcript=m.get("transcript", "") or "",
            end_of_turn=bool(m.get("end_of_turn", False)),
            turn_is_formatted=bool(m.get("turn_is_formatted", False)),
            end_of_turn_confidence=float(m.get("end_of_turn_confidence", 0.0) or 0.0),
            words=m.get("words", []) or [],
            raw=m,
            received_at=time.monotonic(),
        )


OnTurn = Callable[[Turn], None | Awaitable[None]]
OnEvent = Callable[[str, dict], None | Awaitable[None]]


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

class AAIStream:
    """Persistent v3 streaming socket.

    Usage:

        s = AAIStream(language="ar", on_turn=handle)
        await s.start()
        await s.feed(mulaw_160_bytes)     # straight off Twilio, as often as it arrives
        ...
        await s.finish()                  # flush, wait for the last turn
        await s.close()

    Everything is bytes in, `Turn` objects out. It never sees numpy and never
    touches the audio content.
    """

    def __init__(
        self,
        *,
        language: str = "ar",
        api_key: str | None = None,
        url: str | None = None,
        on_turn: Optional[OnTurn] = None,
        on_event: Optional[OnEvent] = None,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        format_turns: bool | None = None,
        end_of_turn_confidence_threshold: float | None = None,
        insecure: bool = False,
        auto_load_key: bool = True,
    ):
        self.language = language
        self.on_turn = on_turn
        self.on_event = on_event
        self.idle_timeout_s = idle_timeout_s
        self.max_retries = max_retries
        self.insecure = insecure

        self._url = url or build_url(
            language=language,
            format_turns=format_turns,
            end_of_turn_confidence_threshold=end_of_turn_confidence_threshold,
        )
        # A ws:// URL means the fake replay server, which wants no credentials.
        self._needs_auth = self._url.startswith("wss://")
        if api_key is not None:
            self._key = api_key
        elif self._needs_auth and auto_load_key:
            self._key = load_key()          # raises AssemblyAIKeyMissing, not a traceback
        else:
            self._key = ""

        self._ws = None
        self._reader_task: asyncio.Task | None = None
        self._closing = False
        self._session_id: str | None = None

        # Reconnect serialisation. BOTH the reader loop and feed()'s send-error
        # handler notice a dropped socket, and without this they each open their
        # own replacement: measured 3 reconnects for 1 injected drop. Against a
        # service billed per connection-second and capped at 5 NEW connections per
        # minute on the free tier, that is a live cost and rate-limit bug, not a
        # tidiness issue. `_conn_gen` lets a latecomer see that somebody else
        # already replaced the socket and skip its own attempt.
        self._reconnect_lock = asyncio.Lock()
        self._conn_gen = 0

        # Observability. latency.py reads these; nothing else mutates them.
        self.connected_at: float | None = None
        self.first_audio_at: float | None = None
        self.last_audio_at: float | None = None
        self.bytes_sent = 0
        self.frames_sent = 0
        self.turns: list[Turn] = []
        self.current: Turn | None = None
        self.errors: list[str] = []
        self.reconnects = 0

        self._turn_done = asyncio.Event()

    # -- connection ---------------------------------------------------------

    def _headers(self) -> dict:
        # NO "Bearer". The v3 streaming endpoint takes the raw key. Their Voice
        # Agent API does use Bearer, which is exactly how this gets got wrong.
        return {"Authorization": self._key} if self._needs_auth else {}

    async def start(self) -> None:
        await self._connect()
        self._reader_task = asyncio.create_task(self._read_loop(), name="aai-read")

    async def _connect(self) -> None:
        kw: dict = {"additional_headers": self._headers(), "max_size": 2 ** 22}
        if self._url.startswith("wss://") and self.insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            kw["ssl"] = ctx
        self._ws = await ws_connect(self._url, **kw)
        self.connected_at = time.monotonic()
        self._conn_gen += 1

    async def _reconnect(self, seen_gen: int | None = None) -> bool:
        """Bounded, backed-off, and SERIALISED reconnect.

        Bounded on purpose. An unbounded retry loop against a per-connection-billed
        service with a 5-new-connections-per-minute cap is a way to spend money and
        get rate-limited at the same time.

        Serialised on purpose too. Pass the connection generation you were using
        when you saw the failure; if the socket has already been replaced by
        whoever got here first, this returns True without opening a second one.
        """
        if self._closing:
            return False
        async with self._reconnect_lock:
            # Somebody already fixed it while we waited for the lock.
            if seen_gen is not None and self._conn_gen > seen_gen:
                return True
            if self._closing:
                return False
            for attempt in range(self.max_retries):
                delay = RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)]
                await self._emit("reconnecting", {"attempt": attempt + 1, "delay_s": delay})
                await asyncio.sleep(delay)
                if self._closing:
                    return False
                try:
                    await self._connect()
                    self.reconnects += 1
                    await self._emit("reconnected", {"attempt": attempt + 1})
                    return True
                except Exception as exc:                  # noqa: BLE001
                    self.errors.append(f"reconnect {attempt + 1}: {exc!r}")
            await self._emit("reconnect_failed", {"attempts": self.max_retries})
            return False

    # -- audio in -----------------------------------------------------------

    async def feed(self, payload: bytes) -> None:
        """Send one chunk of 8 kHz mu-law.

        Accepts EXACTLY the shape Twilio Media Streams delivers: the base64-decoded
        `media.payload`, normally 160 bytes. Deliberately does not require 160 -
        Twilio can merge or split payloads, and the existing call_agent code already
        refuses to assume the frame size for the same reason.
        """
        if not payload:
            return
        if self._ws is None:
            raise RuntimeError("feed() before start()")
        now = time.monotonic()
        if self.first_audio_at is None:
            self.first_audio_at = now
        self.last_audio_at = now
        self.bytes_sent += len(payload)
        self.frames_sent += 1
        gen = self._conn_gen
        try:
            await self._ws.send(payload)            # binary frame, raw mu-law
        except Exception as exc:                    # noqa: BLE001
            self.errors.append(f"send: {exc!r}")
            if self._closing:
                raise
            if not await self._reconnect(seen_gen=gen):
                raise
            try:
                await self._ws.send(payload)
            except Exception as exc2:               # noqa: BLE001
                # One retry only. Retrying forever here would resend the same
                # 20 ms of audio into every new socket we open.
                self.errors.append(f"send after reconnect: {exc2!r}")

    async def feed_twilio_media(self, message: dict) -> None:
        """Convenience for the media-stream server: takes the decoded Twilio JSON
        event and forwards the payload if it is inbound audio."""
        import base64
        if message.get("event") != "media":
            return
        media = message.get("media") or {}
        if media.get("track") not in (None, "inbound"):
            return
        await self.feed(base64.b64decode(media["payload"]))

    async def finish(self, timeout: float = 5.0) -> Turn | None:
        """Tell the service the turn is over and wait for the final transcript.

        v3 takes a JSON control message. We send Terminate to force the flush; the
        service replies with the formatted/final Turn and then a Termination.
        """
        if self._ws is None:
            return None
        self._turn_done.clear()
        try:
            await self._ws.send(json.dumps({"type": "Terminate"}))
        except Exception as exc:                    # noqa: BLE001
            self.errors.append(f"terminate: {exc!r}")
        try:
            await asyncio.wait_for(self._turn_done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.errors.append(f"no final turn within {timeout}s")
        return self.final_turn()

    # -- transcripts out ----------------------------------------------------

    async def _read_loop(self) -> None:
        while not self._closing:
            gen = self._conn_gen
            ws = self._ws
            if ws is None:
                break
            try:
                async for raw in ws:
                    await self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                # noqa: BLE001
                self.errors.append(f"read: {exc!r}")
            if self._closing:
                break
            # Socket dropped mid-call. A phone call is still live, so try to get
            # back - but pass the generation we were reading, so if feed() already
            # replaced the socket we just pick up the new one instead of opening
            # a third.
            if not await self._reconnect(seen_gen=gen):
                break

    async def _handle(self, raw) -> None:
        if isinstance(raw, (bytes, bytearray)):
            return                                   # v3 does not send us binary
        try:
            m = json.loads(raw)
        except json.JSONDecodeError:
            self.errors.append(f"non-JSON message: {raw[:120]!r}")
            return

        mtype = m.get("type", "")
        if mtype == "Begin":
            self._session_id = m.get("id")
            await self._emit("begin", m)
        elif mtype == "Turn":
            t = Turn.from_message(m)
            self.current = t
            self.turns.append(t)
            await self._fire_turn(t)
            if t.end_of_turn:
                self._turn_done.set()
        elif mtype == "Termination":
            await self._emit("termination", m)
            self._turn_done.set()
        elif mtype == "Error" or "error" in m:
            self.errors.append(str(m.get("error") or m))
            await self._emit("error", m)
        else:
            await self._emit(mtype.lower() or "unknown", m)

    async def _fire_turn(self, t: Turn) -> None:
        if self.on_turn is None:
            return
        r = self.on_turn(t)
        if asyncio.iscoroutine(r):
            await r

    async def _emit(self, name: str, payload: dict) -> None:
        if self.on_event is None:
            return
        r = self.on_event(name, payload)
        if asyncio.iscoroutine(r):
            await r

    def final_turn(self) -> Turn | None:
        for t in reversed(self.turns):
            if t.end_of_turn:
                return t
        return self.turns[-1] if self.turns else None

    def text(self) -> str:
        t = self.final_turn()
        return t.transcript if t else ""

    def idle_for(self) -> float:
        if self.last_audio_at is None:
            return 0.0
        return time.monotonic() - self.last_audio_at

    # -- shutdown -----------------------------------------------------------

    async def close(self) -> None:
        """Close the socket. Not optional - billing is per connection-second."""
        self._closing = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):     # noqa: BLE001
                pass
            self._reader_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:                                # noqa: BLE001
                pass
            self._ws = None

    async def __aenter__(self) -> "AAIStream":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# The bridge to call_agent/stt_stream.py
# ---------------------------------------------------------------------------

class TranscriberAdapter:
    """Presents the old poll-a-buffer interface on top of the new push socket.

    THIS IS A MIGRATION SHIM, NOT AN EQUIVALENCE. Read the module docstring. The
    numpy array argument is ignored: by the time the old code asks "what is in this
    buffer?", those bytes have already gone to AssemblyAI through feed(), and asking
    again would mean paying to transcribe the same audio twice.

    Drop-in for:
        Transcriber.quick(pcm8k, language) -> str     # now: latest partial, no wait
        Transcriber.careful(pcm8k, language) -> str   # now: waits for end_of_turn
        Transcriber.warm()                            # now: a no-op, nothing to warm
    """

    def __init__(self, stream: AAIStream, careful_timeout_s: float = 6.0):
        self.stream = stream
        self.careful_timeout_s = careful_timeout_s

    async def quick(self, pcm8k=None, language: str | None = None) -> str:
        """Latest partial, immediately. Returns "" if nothing has arrived yet.

        The old `quick()` blocked for ~240 ms doing real work. This one returns in
        microseconds because the work is happening on the socket. Any caller that
        used quick()'s duration as a de-facto poll interval will now spin - that is
        the loop that has to become a subscriber.
        """
        t = self.stream.current
        return t.transcript if t else ""

    async def careful(self, pcm8k=None, language: str | None = None) -> str:
        """Wait for the turn to be committed, then return the final transcript.

        There is no second more-accurate model to run here. The accuracy the old
        `careful()` bought by switching base->small is instead bought by waiting for
        AssemblyAI's own end-of-turn, which is a different trade and should be
        measured, not assumed equivalent.
        """
        cur = self.stream.final_turn()
        if cur is not None and cur.end_of_turn:
            return cur.transcript
        try:
            await asyncio.wait_for(self.stream._turn_done.wait(), self.careful_timeout_s)
        except asyncio.TimeoutError:
            pass
        t = self.stream.final_turn()
        return t.transcript if t else ""

    def warm(self) -> None:
        """No-op. The old one paid a multi-second first-inference cost up front.
        The equivalent cost here is the websocket handshake, which start() pays."""
        return None


__all__ = [
    "AAIStream", "Turn", "TranscriberAdapter", "AssemblyAIKeyMissing",
    "load_key", "build_url", "ENDPOINT", "SPEECH_MODEL", "FRAME_BYTES", "SAMPLE_RATE",
]
