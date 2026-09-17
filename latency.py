"""The turn budget, as three separately measured numbers.

WHY THIS FILE EXISTS
    The README of the existing agent in this workspace quotes 483 ms turn latency.
    That number is real and it is also the wrong number to quote. Measured against
    call_agent/latency_after.jsonl - 8 real call sessions between 2026-08-18 and
    2026-09-16 - the picture is:

        pre-baked line to first frame ........  0.1-0.2 ms   (bytes already in RAM)
        end-of-speech to hearing SOMETHING ...  ~483 ms      (mostly the endpoint pause)
        end-of-speech to hearing an ANSWER ...  3,878-16,917 ms  (n=7)

    483 ms is ACKNOWLEDGEMENT latency. The agent says "One moment." from pre-baked
    audio in roughly zero time and then takes four to seventeen seconds to actually
    answer. A single blended figure hides exactly the thing that is broken.

    So this module refuses to produce one. It reports:

        1. to_first_partial_ms   - audio start -> first transcript text on screen
        2. to_end_of_turn_ms     - audio start -> ASR commits the turn
        3. to_first_tts_byte_ms  - audio start -> caller hears the first byte of answer

    and the derived gaps between them, of which llm_gap_ms (3 minus 2) is the one
    that matters. Stages 1 and 2 are AssemblyAI's responsibility. The gap after
    stage 2 is ours, and on the evidence it is the entire remaining problem.

NOTHING HERE IS HARDCODED. Every number is computed from timestamps recorded when
the events actually happened. The figures quoted in this docstring come from a log
file and are context for the reader, never inputs to the measurement.

    python3 latency.py              # against the local fake, no key
    python3 latency.py --live       # against the real AssemblyAI socket
    python3 latency.py --n 5        # repeat and report the spread
    python3 latency.py --llm-stub 4.0   # simulate a 4 s LLM to see stage 3 move
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

import aai_stream
import replay
from aai_stream import AAIStream, Turn

HERE = Path(__file__).parent


def _ms(a: float | None, b: float | None) -> float | None:
    """Milliseconds between two monotonic stamps, or None if either never happened.

    None is a real answer here. A stage that did not occur must NOT be reported as
    0 ms - that is how a broken pipeline reads as an infinitely fast one.
    """
    if a is None or b is None:
        return None
    return round((b - a) * 1000.0, 1)


@dataclass
class TurnBudget:
    """One turn, split into stages. Deliberately has no single 'total' field."""
    # Absolute monotonic stamps, kept so a caller can re-derive anything.
    # Every one of these is RECORDED when the event happened. Nothing here is
    # derived from a duration - that is what produced a negative endpoint lag in
    # the first version, and a harness that can report a negative latency cannot
    # be trusted on the positive ones either.
    audio_start: float | None = None
    speech_end_at: float | None = None      # last frame containing actual speech
    audio_end_at: float | None = None       # last frame of any kind
    first_partial_at: float | None = None
    end_of_turn_at: float | None = None
    first_tts_byte_at: float | None = None

    # Context, not timing.
    transcript: str = ""
    end_of_turn_confidence: float = 0.0
    turn_is_formatted: bool = False
    partials: int = 0
    audio_seconds: float = 0.0
    reconnects: int = 0
    errors: list = None
    source: str = "replay"

    def __post_init__(self):
        if self.errors is None:
            self.errors = []

    # -- the three numbers ---------------------------------------------------

    @property
    def to_first_partial_ms(self) -> float | None:
        """Audio start -> the first transcript text exists. This is what makes the
        agent feel awake; it is also what a live-captions UI shows."""
        return _ms(self.audio_start, self.first_partial_at)

    @property
    def to_end_of_turn_ms(self) -> float | None:
        """Audio start -> the ASR says the turn is finished. Includes the whole
        utterance, so it is bounded below by how long the caller spoke. Compare it
        against audio_seconds, never against zero."""
        return _ms(self.audio_start, self.end_of_turn_at)

    @property
    def to_first_tts_byte_ms(self) -> float | None:
        """Audio start -> the caller hears the first byte of the answer. The only
        one of the three the person on the phone can perceive."""
        return _ms(self.audio_start, self.first_tts_byte_at)

    # -- derived gaps --------------------------------------------------------

    @property
    def endpoint_lag_ms(self) -> float | None:
        """Caller stopped SPEAKING -> ASR committed the turn.

        Measured between two recorded stamps. It used to be computed as
        `to_end_of_turn_ms - audio_seconds*1000` and that was wrong twice over:

          1. It compared a recorded event against a DERIVED duration. The final
             frame of this fixture is a 108-byte partial (13.5 ms) dispatched at
             t0+4.140 s, while audio_seconds says 4.1535 s because it assumes the
             frame played out. That 13.5 ms accounting gap made the result NEGATIVE.

          2. Worse and quieter: it measured from the end of the AUDIO STREAM, not
             the end of SPEECH. This fixture has 200 ms of trailing silence, so the
             old number understated endpoint lag by 200 ms - and a real caller who
             pauses before hanging up would understate it by far more. It flattered
             the figure, which is the dangerous direction.

        Now it is end_of_turn_at minus speech_end_at, both stamped when they
        happened. It can legitimately be near zero if the endpointer fires while
        trailing silence is still streaming, but it cannot be an artifact.
        """
        return _ms(self.speech_end_at, self.end_of_turn_at)

    @property
    def trailing_silence_ms(self) -> float | None:
        """Silence streamed after the last speech. Context for endpoint_lag_ms:
        an endpointer is entitled to commit during this window."""
        return _ms(self.speech_end_at, self.audio_end_at)

    @property
    def llm_gap_ms(self) -> float | None:
        """End of turn -> first byte of audible answer. THE NUMBER THAT MATTERS.

        Everything in here is ours: the LLM call, sentence assembly, and TTS
        time-to-first-byte. On the measured history this is 4-17 s while the two
        ASR stages are sub-second."""
        return _ms(self.end_of_turn_at, self.first_tts_byte_at)

    @property
    def asr_stream_ms(self) -> float | None:
        """First partial -> end of turn. How long the ASR kept refining."""
        return _ms(self.first_partial_at, self.end_of_turn_at)

    def as_dict(self) -> dict:
        d = {
            "source": self.source,
            "audio_seconds": round(self.audio_seconds, 3),
            "to_first_partial_ms": self.to_first_partial_ms,
            "to_end_of_turn_ms": self.to_end_of_turn_ms,
            "to_first_tts_byte_ms": self.to_first_tts_byte_ms,
            "endpoint_lag_ms": self.endpoint_lag_ms,
            "trailing_silence_ms": self.trailing_silence_ms,
            "asr_stream_ms": self.asr_stream_ms,
            "llm_gap_ms": self.llm_gap_ms,
            "partials": self.partials,
            "end_of_turn_confidence": self.end_of_turn_confidence,
            "turn_is_formatted": self.turn_is_formatted,
            "reconnects": self.reconnects,
            "errors": list(self.errors),
            "transcript": self.transcript,
        }
        return d

    def render(self) -> str:
        def f(v, unit="ms"):
            return "     n/a" if v is None else f"{v:8.1f}{unit}"
        L = []
        L.append("  TURN BUDGET  (three measurements, deliberately not summed)")
        L.append(f"    source                    : {self.source}")
        L.append(f"    caller spoke for          : {self.audio_seconds*1000:8.1f}ms")
        L.append("")
        L.append(f"    1. to first partial       : {f(self.to_first_partial_ms)}"
                 "   <- ASR (AssemblyAI)")
        L.append(f"    2. to end of turn         : {f(self.to_end_of_turn_ms)}"
                 "   <- ASR (AssemblyAI)")
        L.append(f"    3. to first TTS byte      : {f(self.to_first_tts_byte_ms)}"
                 "   <- what the caller hears")
        L.append("")
        L.append(f"       endpoint lag (2 - speech end): {f(self.endpoint_lag_ms)}"
                 f"   [+{self.trailing_silence_ms or 0:.0f}ms trailing silence]")
        L.append(f"       asr refine   (2 - 1)     : {f(self.asr_stream_ms)}")
        L.append(f"       LLM + TTS    (3 - 2)     : {f(self.llm_gap_ms)}"
                 "   <- THE REMAINING PROBLEM")
        L.append("")
        L.append(f"    partials {self.partials}   eot_conf {self.end_of_turn_confidence:.3f}"
                 f"   formatted {self.turn_is_formatted}   reconnects {self.reconnects}")
        if self.errors:
            L.append(f"    errors: {self.errors}")
        L.append(f"    transcript: {self.transcript}")
        return "\n".join(L)


# ---------------------------------------------------------------------------
# Stage 3 - the answer side
# ---------------------------------------------------------------------------

async def stub_llm_tts(transcript: str, delay_s: float) -> None:
    """Stand-in for brain.reply() + TTS time-to-first-byte.

    A STUB, AND LABELLED ONE. It does not call an LLM. Its only job is to make
    stage 3 a real measured event in the replay path rather than a missing number,
    so the harness shape can be verified end to end offline. Any budget produced
    with a stub is marked `source="replay+stub"` and must never be quoted as a
    latency result.

    Wire the real thing by passing your own coroutine as `on_final` to measure():
    it should return the moment the FIRST byte of TTS audio is ready to go on the
    wire, not when the whole reply has been synthesised.
    """
    await asyncio.sleep(delay_s)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

async def measure(
    *,
    audio: bytes,
    url: str | None,
    language: str = "ar",
    on_final: Optional[Callable[[str], Awaitable[None]]] = None,
    llm_stub_s: float | None = None,
    verbose: bool = True,
    source: str = "replay",
) -> TurnBudget:
    """Run one turn and return its budget.

    `on_final` is awaited the instant end-of-turn arrives, and stage 3 is stamped
    when it returns. That is the honest definition: the caller hears something when
    the answer side has produced its first byte.
    """
    b = TurnBudget(source=source, audio_seconds=len(audio) / 8000.0)

    def on_turn(t: Turn) -> None:
        if t.transcript and b.first_partial_at is None:
            b.first_partial_at = t.received_at
        if t.is_partial:
            b.partials += 1
        if t.end_of_turn and b.end_of_turn_at is None:
            b.end_of_turn_at = t.received_at
            b.transcript = t.transcript
            b.end_of_turn_confidence = t.end_of_turn_confidence
            b.turn_is_formatted = t.turn_is_formatted

    stream = AAIStream(language=language, url=url, on_turn=on_turn)
    await stream.start()

    frames = replay.frames_of(audio)
    # Where speech actually stops, computed from the audio itself rather than
    # assumed to be the end of the file. See replay.speech_end_frame().
    last_speech = replay.speech_end_frame(audio)
    t0 = time.monotonic()
    b.audio_start = t0
    try:
        for i, f in enumerate(frames):
            due = t0 + i * replay.FRAME_S
            d = due - time.monotonic()
            if d > 0:
                await asyncio.sleep(d)
            await stream.feed(f)
            # Stamp AFTER the send, so it marks when the bytes were actually
            # handed to the socket, not when we intended to.
            if i == last_speech:
                b.speech_end_at = time.monotonic()
        b.audio_end_at = time.monotonic()
        await stream.finish(timeout=8.0)

        # Stage 3. Only meaningful if the turn actually committed.
        if b.end_of_turn_at is not None:
            if on_final is not None:
                await on_final(b.transcript)
                b.first_tts_byte_at = time.monotonic()
            elif llm_stub_s is not None:
                await stub_llm_tts(b.transcript, llm_stub_s)
                b.first_tts_byte_at = time.monotonic()
            # else: left as None. A missing answer stage reports n/a, not 0.
    finally:
        b.reconnects = stream.reconnects
        b.errors = list(stream.errors)
        if not b.transcript:
            b.transcript = stream.text()
        await stream.close()

    if verbose:
        print(b.render())
    return b


async def measure_replay(
    *,
    llm_stub_s: float = 0.25,
    verbose: bool = True,
    audio: bytes | None = None,
) -> TurnBudget:
    """Measure against the local fake. No API key, no phone call.

    Uses a stub for stage 3 so the shape is complete; the result is tagged
    `replay+stub` precisely so it cannot be mistaken for a real measurement.
    """
    import fake_aai_server
    data = audio if audio is not None else replay.load_audio()
    server, _ = await fake_aai_server.run_server()
    try:
        return await measure(
            audio=data, url=fake_aai_server.server_url(server),
            llm_stub_s=llm_stub_s, verbose=verbose,
            source=f"replay+stub({llm_stub_s}s)")
    finally:
        server.close()
        await server.wait_closed()


def summarise(budgets: list[TurnBudget]) -> str:
    """Spread across runs. A single run of a network measurement is an anecdote."""
    if not budgets:
        return "  no runs"
    rows = [
        ("1. to first partial", "to_first_partial_ms"),
        ("2. to end of turn", "to_end_of_turn_ms"),
        ("3. to first TTS byte", "to_first_tts_byte_ms"),
        ("   endpoint lag", "endpoint_lag_ms"),
        ("   LLM + TTS gap", "llm_gap_ms"),
    ]
    out = [f"  ACROSS {len(budgets)} RUNS (ms)",
           f"    {'stage':<22} {'min':>9} {'median':>9} {'max':>9}"]
    for label, attr in rows:
        vals = [v for v in (getattr(b, attr) for b in budgets) if v is not None]
        if not vals:
            out.append(f"    {label:<22} {'n/a':>9} {'n/a':>9} {'n/a':>9}")
            continue
        out.append(f"    {label:<22} {min(vals):>9.1f} "
                   f"{statistics.median(vals):>9.1f} {max(vals):>9.1f}")
    missing = [b for b in budgets if b.to_first_tts_byte_ms is None]
    if missing:
        out.append(f"    ({len(missing)} run(s) never reached stage 3 - reported as n/a, "
                   f"NOT as zero)")
    return "\n".join(out)


async def _main(a) -> int:
    audio = replay.load_audio(a.audio)
    budgets: list[TurnBudget] = []

    if a.live:
        try:
            aai_stream.load_key()
        except aai_stream.AssemblyAIKeyMissing as exc:
            print(exc)
            return 2

    for i in range(a.n):
        if a.n > 1:
            print(f"\n=== run {i+1}/{a.n} ===")
        if a.live:
            b = await measure(audio=audio, url=None, language=a.language,
                              llm_stub_s=a.llm_stub, source="live+stub"
                              if a.llm_stub is not None else "live")
        else:
            b = await measure_replay(llm_stub_s=a.llm_stub if a.llm_stub is not None
                                     else 0.25, audio=audio)
        budgets.append(b)
        if i + 1 < a.n:
            # The free tier caps NEW streaming connections at 5/minute, and every
            # run opens one. Space them out rather than getting rate-limited.
            await asyncio.sleep(a.gap)

    if a.n > 1:
        print()
        print(summarise(budgets))

    if a.json:
        p = Path(a.json)
        p.write_text(json.dumps([b.as_dict() for b in budgets],
                                ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  wrote {p}")

    print()
    print("  Reminder: stages 1 and 2 are AssemblyAI. Stage 3 minus stage 2 is ours.")
    if not a.live:
        print("  These are REPLAY numbers against a local fake with a stubbed answer")
        print("  side. They verify the instrument, not the service. Nothing here has")
        print("  touched a live AssemblyAI socket - there is no account yet.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--audio", default=str(replay.DEFAULT_AUDIO))
    ap.add_argument("--language", default="ar")
    ap.add_argument("--live", action="store_true", help="real AssemblyAI socket")
    ap.add_argument("--replay", action="store_true",
                    help="local fake (the default; accepted for symmetry with --live)")
    ap.add_argument("--n", type=int, default=1, help="repeat N times and show the spread")
    ap.add_argument("--gap", type=float, default=13.0,
                    help="seconds between runs (free tier: 5 new connections/min)")
    ap.add_argument("--llm-stub", type=float, default=None,
                    help="simulate an LLM+TTS of N seconds so stage 3 is populated")
    ap.add_argument("--json", default=None, help="write the budgets to this path")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_main(args)))
