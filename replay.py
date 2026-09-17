"""Feed a recorded 8 kHz mu-law file through AAIStream, at true telephone pace.

This is the harness that makes the whole path testable with no API key and no phone
call. It exists because the alternative - dialling a real number to find out whether
a parser works - costs money, rings a human being, and cannot be run in a loop.

    python3 replay.py                 # against the local fake, no key needed
    python3 replay.py --live          # against the real AssemblyAI socket
    python3 replay.py --live --record # ...and overwrite the protocol fixture with
                                      #    what actually came back

PACING IS THE POINT. Frames go out one per 20 ms of wall clock, exactly as Twilio
delivers them. Blasting the file at the socket as fast as it reads would produce
latency numbers that mean nothing, because the service would receive four seconds of
speech in forty milliseconds. `--turbo` disables pacing for a fast structural check
and is refused in combination with any latency claim.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import aai_stream
from aai_stream import AAIStream, Turn

HERE = Path(__file__).parent
DEFAULT_AUDIO = HERE / "fixtures" / "arabic_caller_8k.ulaw"
FIXTURE = HERE / "fixtures" / "v3_session_arabic.jsonl"

FRAME = aai_stream.FRAME_BYTES          # 160 bytes = 20 ms
FRAME_S = 0.02


def load_audio(path: Path | str = DEFAULT_AUDIO) -> bytes:
    p = Path(path)
    if not p.exists():
        raise SystemExit(
            f"missing audio fixture: {p}\n"
            "Rebuild it from the real call_agent session with:\n"
            "  ffmpeg -y -i ../call_agent/out/caller_4.mp3 -ar 8000 -ac 1 -f mulaw "
            f"{p}"
        )
    data = p.read_bytes()
    if len(data) < FRAME:
        raise SystemExit(f"{p} is only {len(data)} bytes - that is under one 20 ms frame")
    return data


def frames_of(data: bytes) -> list[bytes]:
    """Split into 160-byte frames, exactly as Twilio would deliver them.

    A trailing partial frame is KEPT, not discarded. Real audio does not end on a
    frame boundary and silently dropping the last 19 ms is the kind of thing that
    clips the final word off a transcript.
    """
    out = [data[i:i + FRAME] for i in range(0, len(data), FRAME)]
    return [f for f in out if f]


class Collector:
    """Records every turn with the local clock, which is what latency.py reads."""

    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self.turns: list[Turn] = []
        self.events: list[tuple[str, float, dict]] = []
        self.audio_started: float | None = None
        self.first_partial_at: float | None = None
        self.end_of_turn_at: float | None = None

    def on_turn(self, t: Turn) -> None:
        self.turns.append(t)
        if t.transcript and self.first_partial_at is None:
            self.first_partial_at = t.received_at
        if t.end_of_turn and self.end_of_turn_at is None:
            self.end_of_turn_at = t.received_at
        if self.verbose:
            tag = "FINAL " if t.end_of_turn else "partial"
            rel = ""
            if self.audio_started is not None:
                rel = f" @{(t.received_at - self.audio_started) * 1000:7.1f}ms"
            print(f"  [{tag}]{rel} conf={t.end_of_turn_confidence:.3f}  {t.transcript}")

    def on_event(self, name: str, payload: dict) -> None:
        self.events.append((name, time.monotonic(), payload))
        if self.verbose and name in ("begin", "reconnecting", "reconnected",
                                     "reconnect_failed", "error", "termination"):
            print(f"  <{name}> {json.dumps(payload, ensure_ascii=False)[:160]}")


async def replay(
    *,
    audio: bytes,
    url: str | None = None,
    language: str = "ar",
    turbo: bool = False,
    verbose: bool = True,
    api_key: str | None = None,
    finish_timeout: float = 8.0,
) -> tuple[AAIStream, Collector]:
    """Push `audio` through a stream and return (stream, collector).

    `url` None means live AssemblyAI (and will demand a key). Pass a ws:// URL to
    point at the fake, which needs no credentials.
    """
    col = Collector(verbose=verbose)
    stream = AAIStream(
        language=language,
        url=url,
        api_key=api_key,
        on_turn=col.on_turn,
        on_event=col.on_event,
    )
    await stream.start()

    frames = frames_of(audio)
    t0 = time.monotonic()
    col.audio_started = t0
    try:
        for i, f in enumerate(frames):
            if not turbo:
                # Absolute schedule, not sleep(0.02) per frame. Cumulative sleep
                # drift over 200 frames would quietly stretch a 4.15 s utterance,
                # and every latency number downstream would inherit the error.
                due = t0 + i * FRAME_S
                d = due - time.monotonic()
                if d > 0:
                    await asyncio.sleep(d)
            await stream.feed(f)

        await stream.finish(timeout=finish_timeout)
    finally:
        # Always close. Billing is per connection-second.
        await stream.close()
    return stream, col


def report(stream: AAIStream, col: Collector, audio_len: int) -> None:
    print()
    print("  audio       :", f"{audio_len} bytes / {audio_len/8000:.2f}s "
                            f"/ {len(frames_of(b'x'*audio_len))} frames")
    print("  sent        :", f"{stream.frames_sent} frames, {stream.bytes_sent} bytes")
    print("  turns       :", len(stream.turns),
          f"({sum(1 for t in stream.turns if t.is_partial)} partial, "
          f"{sum(1 for t in stream.turns if t.end_of_turn)} final)")
    print("  reconnects  :", stream.reconnects)
    print("  errors      :", stream.errors or "none")
    final = stream.final_turn()
    if final:
        print("  formatted   :", final.turn_is_formatted,
              "  <- format_turns is documented as unavailable on universal-3-5-pro")
        print("  eot conf    :", final.end_of_turn_confidence)
        print("  transcript  :", final.transcript)
    else:
        print("  transcript  : (none)")


async def _main(a) -> int:
    audio = load_audio(a.audio)

    if a.live:
        url = None                                  # -> wss://streaming.assemblyai.com
        print(f"LIVE AssemblyAI  lang={a.language}  model={aai_stream.SPEECH_MODEL}")
        try:
            aai_stream.load_key()
        except aai_stream.AssemblyAIKeyMissing as exc:
            print(exc)
            return 2
        stream, col = await replay(audio=audio, url=url, language=a.language,
                                   turbo=a.turbo)
        if a.record:
            n = record_fixture(stream, len(audio))
            print(f"\nrecorded {n} real messages -> {FIXTURE}")
            print("fixtures/README.md must be updated: these are now CAPTURED, not authored.")
    else:
        import fake_aai_server
        server, fake = await fake_aai_server.run_server(
            drop_after=a.drop_after, slow_ms=a.slow_ms)
        url = fake_aai_server.server_url(server)
        print(f"REPLAY against local fake at {url}  (no API key used)")
        try:
            stream, col = await replay(audio=audio, url=url, language=a.language,
                                       turbo=a.turbo)
        finally:
            server.close()
            await server.wait_closed()

    report(stream, col, len(audio))
    return 0


def record_fixture(stream: AAIStream, audio_len: int) -> int:
    """Write the REAL messages we just received back out as a replay script.

    Byte gates are reconstructed from each turn's arrival time against the known
    20 ms pacing, which is the best available mapping - the service does not tell
    us how much audio it had consumed when it decided to speak.
    """
    if stream.first_audio_at is None:
        return 0
    recs = [{"after_bytes": 0, "message": {"type": "Begin", "id": "recorded"}}]
    for t in stream.turns:
        elapsed = t.received_at - stream.first_audio_at
        after = min(audio_len, max(0, int(elapsed / FRAME_S) * aai_stream.FRAME_BYTES))
        recs.append({"after_bytes": after, "message": t.raw})
    recs.append({"after_bytes": audio_len,
                 "message": {"type": "Termination",
                             "audio_duration_seconds": round(audio_len / 8000, 3)}})
    FIXTURE.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
        encoding="utf-8")
    return len(recs)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--audio", default=str(DEFAULT_AUDIO))
    ap.add_argument("--language", default="ar")
    ap.add_argument("--live", action="store_true", help="use the real AssemblyAI socket")
    ap.add_argument("--record", action="store_true",
                    help="with --live, overwrite the protocol fixture with real frames")
    ap.add_argument("--turbo", action="store_true",
                    help="skip 20 ms pacing (structural check only, NOT for latency)")
    ap.add_argument("--drop-after", type=int, default=None,
                    help="fake only: kill the socket after N bytes, to test reconnect")
    ap.add_argument("--slow-ms", type=int, default=0, help="fake only: delay each message")
    args = ap.parse_args()
    if args.record and not args.live:
        raise SystemExit("--record only makes sense with --live: there is nothing real "
                         "to record off the fake server.")
    raise SystemExit(asyncio.run(_main(args)))
