"""Does ONE v3 socket carry a whole conversation, or one turn?

NOTES.md listed multi-turn behaviour as unverified, and the demo's entire
architecture rests on the answer:

  If one socket carries many turns, a conversation is one connection. Billing is
  per socket-second, so that is also the cheap answer.

  If it does not, every turn needs a new socket - which on the free tier's cap of
  5 NEW connections per minute means a normal conversation rate-limits itself
  after five exchanges, and each new session starts with no memory of the audio
  that came before.

That is not a detail to assume in either direction, so this measures it. It
feeds the captured Arabic clip, a gap of silence, then the same clip again, on a
single socket, and reports how many turns came back and whether `turn_order`
advanced.

  python3 multiturn_probe.py            # live, costs a few socket-seconds
  python3 multiturn_probe.py --gap 2.5  # longer pause between utterances
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from aai_stream import AAIStream, MULAW_SILENCE, Turn, build_url

HERE = Path(__file__).resolve().parent
CLIP = HERE / "fixtures" / "arabic_caller_8k.ulaw"

FRAME = 160          # 20 ms at 8 kHz mu-law, exactly what Twilio sends
FRAME_S = 0.02


async def run(gap_s: float, repeats: int) -> int:
    audio = CLIP.read_bytes()
    gap = bytes([MULAW_SILENCE]) * int(gap_s * 8000)

    seen: list[Turn] = []
    finals: list[Turn] = []

    def on_turn(t: Turn) -> None:
        seen.append(t)
        if t.end_of_turn:
            finals.append(t)
            print(f"    [end_of_turn] order={t.turn_order}  {t.transcript}")

    def on_event(name: str, payload: dict) -> None:
        if name in ("error", "reconnecting", "reconnect_failed", "termination"):
            print(f"    [{name}] {payload}")

    stream = AAIStream(language="ar", on_turn=on_turn, on_event=on_event,
                       idle_timeout_s=120.0)
    await stream.start()
    print(f"  socket open, feeding {repeats} utterances with {gap_s:.1f}s of silence between")

    t0 = time.monotonic()
    try:
        for i in range(repeats):
            if i:
                # Real silence at true pace, so the service's endpointer has the
                # same information it would have on a live call. Firing the gap
                # in faster than real time would let it see a pause that never
                # happened at that length.
                for off in range(0, len(gap), FRAME):
                    await stream.feed(gap[off:off + FRAME])
                    await asyncio.sleep(FRAME_S)
            print(f"  utterance {i + 1}/{repeats}")
            for off in range(0, len(audio), FRAME):
                await stream.feed(audio[off:off + FRAME])
                await asyncio.sleep(FRAME_S)
            # Give the endpointer room to commit this turn before the next one.
            for _ in range(int(1.2 / FRAME_S)):
                await stream.feed(bytes([MULAW_SILENCE]) * FRAME)
                await asyncio.sleep(FRAME_S)
        await stream.finish(timeout=8.0)
    finally:
        await stream.close()

    elapsed = time.monotonic() - t0
    orders = sorted({t.turn_order for t in finals})

    print()
    print(f"  socket seconds      {elapsed:.1f}")
    print(f"  turn messages       {len(seen)}")
    print(f"  committed turns     {len(finals)}")
    print(f"  turn_order values   {orders}")
    print(f"  reconnects          {stream.reconnects}")
    print(f"  errors              {stream.errors or 'none'}")
    print()

    if len(finals) >= repeats and len(orders) >= repeats:
        print("  RESULT: one socket carries a multi-turn conversation, and")
        print("          turn_order advances. A conversation is one connection.")
        return 0
    if len(finals) >= 1 and len(orders) == 1:
        print("  RESULT: the socket committed ONE turn and did not start another.")
        print("          A conversation needs a socket per turn, which collides")
        print("          with the 5-new-connections-per-minute free tier cap.")
        return 1
    print("  RESULT: inconclusive. Fewer committed turns than utterances fed;")
    print("          this may be endpointing, not a session limit. Re-run with")
    print("          a longer --gap before drawing a conclusion.")
    return 2


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gap", type=float, default=1.5,
                   help="seconds of silence between utterances")
    p.add_argument("--repeats", type=int, default=2)
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args.gap, args.repeats)))


if __name__ == "__main__":
    main()
