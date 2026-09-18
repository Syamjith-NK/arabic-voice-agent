"""Demo server: browser microphone -> AssemblyAI v3 -> booking agent -> browser.

This is the piece a judge actually touches. It serves `web/` over plain HTTP and
accepts a websocket at `/ws`, so one process is the whole hosted demo.

WHY THE BROWSER PATH IS 16 kHz PCM AND THE PHONE PATH IS 8 kHz MU-LAW
---------------------------------------------------------------------
`aai_stream.py` was written for Twilio Media Streams: 8 kHz mu-law, forwarded
byte for byte. A browser microphone is not that. It gives float samples at the
device rate (usually 48 kHz), so something has to convert, and the honest place
to do it is the client, before the bytes ever hit the network. The browser
downsamples to 16 kHz PCM16 and sends 100 ms frames; we forward them unchanged.

16 kHz is also simply better input than a phone line, so the demo should not be
handicapped to 8 kHz to match a transport it is not using. Both paths share the
same client, the same session, the same agent. Only the wire format differs, and
that difference is two constructor arguments.

THE TRAP THAT GENERALISATION EXISTS TO AVOID: the chunk aggregator counts BYTES,
and bytes per millisecond is 8 on the phone line and 32 here. Reusing the phone
line's constant would buffer 25 ms while believing it buffered 100, which is
under the service's 50 ms floor and closes the socket with error_code 3007.

WHAT THIS COSTS, WHICH IS WHY THERE IS A BUDGET GOVERNOR
--------------------------------------------------------
AssemblyAI streaming bills by SOCKET DURATION, not by audio. A public demo URL
is therefore a page that spends money whenever a stranger opens it, including
while they sit there saying nothing. The free tier also caps new connections at
5 per minute, so a judging panel opening the link together can lock each other
out. `Budget` below caps concurrent sessions, per-session seconds and total
seconds per day, and when it refuses it SAYS the demo is out of budget rather
than looking broken.

TRANSCRIPT-ONLY MODE: if `agent.py` is absent the server still runs and still
streams transcripts. It reports `agent: none` in the status event rather than
inventing replies.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path

import numpy as np
from websockets.asyncio.server import serve
from websockets.http11 import Response
from websockets.datastructures import Headers

from aai_stream import (
    AAIStream,
    AssemblyAIKeyMissing,
    PCM16_BYTES_PER_MS_16K,
    PCM16_SILENCE,
    Turn,
    build_url,
)

try:
    from agent import BookingAgent
except ImportError:                     # transcript-only mode, stated not hidden
    BookingAgent = None                 # type: ignore[assignment]

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"

BROWSER_SAMPLE_RATE = 16000
BROWSER_ENCODING = "pcm_s16le"
BROWSER_CHUNK_MS = 100                  # 3200 bytes; the client sends exactly this

# Speech-onset gate. This is an ENERGY GATE, not a voice activity detector: it
# cannot tell a cough from a word. It exists for one reason, and only one -
# latency has to be measured from when the caller STARTED SPEAKING, not from
# when the socket opened or from when the previous turn ended. Measuring from
# either of those folds the caller's own thinking pause into "our" latency and
# produces a number that is wrong in the flattering direction if the pause is
# short and absurd if it is long. NOTES.md records an earlier harness that went
# NEGATIVE from exactly this class of mistake.
ONSET_RMS = 500.0                       # int16 units; ~ -36 dBFS


@dataclass
class Budget:
    """Spend governor for a public URL. All limits are wall-clock socket seconds."""
    max_concurrent: int = 2
    max_session_s: float = 180.0
    max_daily_s: float = 3600.0

    _open: int = 0
    _spent_s: float = 0.0
    _day: str = field(default_factory=lambda: time.strftime("%Y-%m-%d"))

    def _roll(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self._spent_s = today, 0.0

    def admit(self):
        """Return (ok, reason). Reason is shown to the user verbatim."""
        self._roll()
        if self._open >= self.max_concurrent:
            return False, (
                f"{self._open} sessions are already live and the demo allows "
                f"{self.max_concurrent} at once. AssemblyAI streaming bills by socket "
                f"duration, so this is a real cost limit, not a queue. Try again shortly."
            )
        if self._spent_s >= self.max_daily_s:
            return False, (
                f"The demo has used its {self.max_daily_s/60:.0f} minute daily budget. "
                f"Resets at midnight UTC+4. The repo runs offline with no key: "
                f"python3 selftest.py"
            )
        self._open += 1
        return True, ""

    def release(self, seconds: float) -> None:
        self._open = max(0, self._open - 1)
        self._spent_s += max(0.0, seconds)

    def snapshot(self) -> dict:
        self._roll()
        return {
            "open": self._open,
            "max_concurrent": self.max_concurrent,
            "spent_s": round(self._spent_s, 1),
            "max_daily_s": self.max_daily_s,
        }


class Session:
    """One browser connection: its socket, its ASR stream, its agent, its clocks."""

    def __init__(self, ws, *, budget: Budget, live: bool, url: str | None = None):
        self.ws = ws
        self.budget = budget
        self.live = live
        self.url = url
        self.opened_at = time.monotonic()

        self.stream: AAIStream | None = None
        self.agent = BookingAgent() if BookingAgent is not None else None

        # Per-turn clocks. Reset at each speech onset.
        self.onset_at: float | None = None
        self.first_partial_at: float | None = None
        self.turn_text: str = ""          # last partial text of the CURRENT turn
        self.turn_order: int = -1
        self.awaiting_onset = True

        self._outbox: asyncio.Queue = asyncio.Queue()
        self._closed = False

    # -- outbound -----------------------------------------------------------

    def post(self, **event) -> None:
        """Queue an event for the browser. Never awaits, so it is safe to call
        from inside an AAIStream callback without reordering the read loop."""
        self._outbox.put_nowait(event)

    async def pump(self) -> None:
        while not self._closed:
            event = await self._outbox.get()
            if event is None:
                return
            try:
                await self.ws.send(json.dumps(event, ensure_ascii=False))
            except Exception:                       # noqa: BLE001 - browser went away
                return

    # -- inbound audio ------------------------------------------------------

    def note_energy(self, pcm: bytes) -> None:
        """Mark speech onset for this turn from raw PCM16 energy."""
        if not self.awaiting_onset or len(pcm) < 2:
            return
        samples = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype="<i2")
        if samples.size == 0:
            return
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        if rms >= ONSET_RMS:
            self.onset_at = time.monotonic()
            self.first_partial_at = None
            self.awaiting_onset = False

    # -- transcripts --------------------------------------------------------

    def on_turn(self, t: Turn) -> None:
        now = time.monotonic()

        if t.turn_order != self.turn_order:
            self.turn_order = t.turn_order
            self.turn_text = ""

        # REVISION DETECTION. v3 resends the whole turn each time, and it does
        # not only grow it: measured live, `... إلى السنة` ("to the year") came
        # back as `... إلى الساعة تسعة` ("to nine o'clock"). Nothing in the
        # protocol announces that. The only way to know is to check whether what
        # you had is still a prefix of what you were just given.
        revised = bool(self.turn_text) and not t.transcript.startswith(self.turn_text)
        previous = self.turn_text
        self.turn_text = t.transcript

        if self.first_partial_at is None and t.transcript:
            self.first_partial_at = now

        if not t.end_of_turn:
            self.post(type="partial", text=t.transcript, turn=t.turn_order,
                      revised=revised, replaced=previous if revised else "")
            return

        # Turn committed.
        self.post(type="final", text=t.transcript, turn=t.turn_order)
        self.post(type="latency", **self._latency(now))
        self.awaiting_onset = True          # next speech starts a new measurement

        if self.agent is None:
            self.post(type="status", state="listening",
                      detail="transcript only: agent.py is not present")
            return

        self.post(type="status", state="thinking", detail="")
        reply = self.agent.handle(t.transcript)
        self.post(type="slots", slots=reply.slots, extracted=reply.debug)
        self.post(type="reply", text=reply.text, speak=True, done=reply.done,
                  used_llm=reply.used_llm, agent_ms=round(reply.ms, 2))

    def _latency(self, end_of_turn_at: float) -> dict:
        """Three numbers, never blended into one.

        Reported relative to SPEECH ONSET. A turn where the energy gate never
        fired reports -1 rather than a number measured from the wrong origin.
        """
        if self.onset_at is None:
            return {"first_partial_ms": -1, "end_of_turn_ms": -1,
                    "note": "no speech onset detected; timings would be measured "
                            "from the wrong origin, so they are withheld"}
        fp = (self.first_partial_at - self.onset_at) * 1000 if self.first_partial_at else -1
        return {
            "first_partial_ms": round(fp, 1),
            "end_of_turn_ms": round((end_of_turn_at - self.onset_at) * 1000, 1),
            "endpoint_lag_ms": -1,      # needs end-of-speech, which the gate cannot give
        }

    def on_event(self, name: str, payload: dict) -> None:
        if name == "error":
            self.post(type="status", state="error",
                      detail=str(payload.get("error") or payload))
        elif name in ("reconnecting", "reconnect_failed"):
            self.post(type="status", state="error", detail=f"asr {name}: {payload}")

    # -- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        ok, reason = self.budget.admit()
        if not ok:
            self.post(type="status", state="error", detail=reason)
            pump = asyncio.create_task(self.pump())
            await asyncio.sleep(0.25)
            self._closed = True
            pump.cancel()
            return

        pump = asyncio.create_task(self.pump())
        try:
            await self._run_inner()
        finally:
            self._closed = True
            self.budget.release(time.monotonic() - self.opened_at)
            if self.stream is not None:
                await self.stream.close()
            pump.cancel()

    async def _run_inner(self) -> None:
        url = self.url or build_url(
            language="ar",
            sample_rate=BROWSER_SAMPLE_RATE,
            encoding=BROWSER_ENCODING,
        )
        try:
            self.stream = AAIStream(
                language="ar",
                url=url,
                on_turn=self.on_turn,
                on_event=self.on_event,
                chunk_ms=BROWSER_CHUNK_MS,
                bytes_per_ms=PCM16_BYTES_PER_MS_16K,
                pad_byte=PCM16_SILENCE,
                idle_timeout_s=self.budget.max_session_s,
            )
            await self.stream.start()
        except AssemblyAIKeyMissing as exc:
            self.post(type="status", state="error", detail=str(exc).split("\n")[0])
            return
        except Exception as exc:                    # noqa: BLE001
            self.post(type="status", state="error", detail=f"asr connect failed: {exc!r}")
            return

        self.post(type="status", state="connected",
                  detail=f"universal-3-5-pro, ar, {BROWSER_SAMPLE_RATE} Hz {BROWSER_ENCODING}",
                  budget=self.budget.snapshot(),
                  agent="booking" if self.agent else "none")

        if self.agent is not None:
            greet = self.agent.greet()
            self.post(type="reply", text=greet.text, speak=True, done=False,
                      used_llm=False, agent_ms=round(greet.ms, 2))

        self.post(type="status", state="listening", detail="")

        deadline = self.opened_at + self.budget.max_session_s
        async for message in self.ws:
            if time.monotonic() > deadline:
                self.post(type="status", state="error",
                          detail=f"session capped at {self.budget.max_session_s:.0f}s "
                                 f"because streaming is billed per socket-second")
                break
            if isinstance(message, (bytes, bytearray)):
                self.note_energy(bytes(message))
                await self.stream.feed(bytes(message))
                continue
            try:
                m = json.loads(message)
            except json.JSONDecodeError:
                continue
            kind = m.get("type")
            if kind == "stop":
                break
            if kind == "reset" and self.agent is not None:
                self.agent.reset()
                greet = self.agent.greet()
                self.post(type="slots", slots=self.agent.slots, extracted={})
                self.post(type="reply", text=greet.text, speak=True, done=False,
                          used_llm=False, agent_ms=round(greet.ms, 2))


# ---------------------------------------------------------------------------
# HTTP: serve web/ from the same port, so the demo is one URL and one process
# ---------------------------------------------------------------------------

def _static(path: str) -> Response | None:
    rel = "index.html" if path in ("/", "") else path.lstrip("/")
    if ".." in rel:
        return Response(HTTPStatus.FORBIDDEN, "Forbidden", Headers(), b"nope\n")
    f = WEB / rel
    if not f.is_file():
        return Response(HTTPStatus.NOT_FOUND, "Not Found", Headers(), b"not found\n")
    body = f.read_bytes()
    ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        ctype += "; charset=utf-8"
    headers = Headers({
        "Content-Type": ctype,
        "Content-Length": str(len(body)),
        "Cache-Control": "no-store",
    })
    return Response(HTTPStatus.OK, "OK", headers, body)


def make_app(budget: Budget, *, live: bool, url: str | None):
    async def process_request(connection, request):
        if request.path.split("?")[0] != "/ws":
            return _static(request.path.split("?")[0])
        return None                                  # let it upgrade

    async def handler(ws):
        await Session(ws, budget=budget, live=live, url=url).run()

    return handler, process_request


async def main_async(args) -> None:
    budget = Budget(max_concurrent=args.max_concurrent,
                    max_session_s=args.max_session_s,
                    max_daily_s=args.max_daily_s)
    handler, process_request = make_app(budget, live=not args.fake, url=args.url)
    print(f"  web    http://{args.host}:{args.port}/")
    print(f"  ws     ws://{args.host}:{args.port}/ws")
    print(f"  agent  {'booking' if BookingAgent else 'NONE (transcript only)'}")
    print(f"  budget {budget.max_concurrent} concurrent, "
          f"{budget.max_session_s:.0f}s/session, {budget.max_daily_s/60:.0f}min/day")
    async with serve(handler, args.host, args.port, process_request=process_request):
        await asyncio.get_running_loop().create_future()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8811)
    p.add_argument("--url", default=None,
                   help="override the ASR websocket URL (point at fake_aai_server.py)")
    p.add_argument("--fake", action="store_true",
                   help="informational; pair with --url ws://... for a keyless demo")
    p.add_argument("--max-concurrent", type=int, default=2)
    p.add_argument("--max-session-s", type=float, default=180.0)
    p.add_argument("--max-daily-s", type=float, default=3600.0)
    args = p.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
