"""Does the number parser actually recover what the API actually returns?

CONSTRAINT 2 says AssemblyAI returns numbers as Arabic words. `arabic_numbers.py`
parses Arabic number words. Both were verified separately, and separately is not
the same as together: the parser's test suite is authored strings, and the
constraint was measured on exactly ONE captured sentence. Nothing had ever run
the whole path.

This runs it. For each utterance it knows the ground truth for, it synthesises
the Arabic with the system voice, streams it to the live v3 socket, and asks
whether the parser recovers the value that was spoken.

    python3 number_e2e.py            # the full set, live
    python3 number_e2e.py --only 3   # one case, for iterating cheaply

WHAT THIS MEASURES AND WHAT IT DOES NOT
---------------------------------------
It measures whether the transcription-to-value path survives end to end across
many numbers instead of one. That is worth knowing, because a parser tuned to a
single captured sentence proves almost nothing.

It does NOT measure real-world accuracy, and no number out of this file should
ever be quoted as if it did. The audio is a macOS system voice reading clean
text into a virtual line: no dialect, no noise, no mobile codec, no overlapping
speakers, no hesitation. A synthetic speaker is also, plausibly, EASIER for an
ASR model than a human, so a good score here is a floor on the failure modes
rather than evidence of robustness.

What it does catch, and what nothing else in the repo would: a number the
service spells in a form the parser does not know. That failure is silent in
production, because the transcript looks perfectly correct to a reader and the
booking simply has no time in it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import arabic_numbers as an
import tts_say
from aai_stream import (
    AAIStream,
    PCM16_BYTES_PER_MS_16K,
    PCM16_SILENCE,
    Turn,
    build_url,
)

FRAME_MS = 20
BYTES_PER_FRAME = FRAME_MS * PCM16_BYTES_PER_MS_16K      # 640 bytes of 16 kHz PCM16


@dataclass
class Case:
    text: str
    kind: str                 # "time" or "quantity"
    expect: object            # (hour, minute) for time, a number for quantity
    note: str = ""


CASES = [
    Case("أبغى أحجز تصوير الساعة تسعة صباحاً", "time", (9, 0)),
    Case("خلها الساعة الثالثة بعد الظهر", "time", (15, 0)),
    Case("نبدأ الساعة السابعة والنصف مساءً", "time", (19, 30)),
    Case("الموعد الساعة الخامسة إلا ربعاً مساءً", "time", (16, 45)),
    Case("تعال الساعة العاشرة والربع صباحاً", "time", (10, 15)),
    Case("الساعة الحادية عشرة والنصف ليلاً", "time", (23, 30)),
    Case("نحتاج ثلاث كاميرات", "quantity", 3),
    Case("عندنا خمسة عشر شخصاً في الاستوديو", "quantity", 15),
    Case("الفريق خمسة وأربعين شخصاً", "quantity", 45, "units-first waw compound"),
    Case("التصوير يحتاج ساعتين", "quantity", None, "dual form, known gap"),
    Case("الميزانية ثلاثة آلاف وخمسمئة درهم", "quantity", 3500),
    Case("رقم الغرفة مئتين وثلاثين", "quantity", 230),
]


async def transcribe(pcm: bytes, *, pace: bool) -> tuple:
    """Stream one utterance and return (final_transcript, elapsed_s, errors)."""
    finals: list = []

    def on_turn(t: Turn) -> None:
        if t.end_of_turn:
            finals.append(t)

    stream = AAIStream(
        language="ar",
        url=build_url(language="ar", sample_rate=16000, encoding="pcm_s16le"),
        on_turn=on_turn,
        chunk_ms=100,
        bytes_per_ms=PCM16_BYTES_PER_MS_16K,
        pad_byte=PCM16_SILENCE,
        idle_timeout_s=60.0,
    )
    t0 = time.monotonic()
    await stream.start()
    try:
        for off in range(0, len(pcm), BYTES_PER_FRAME):
            await stream.feed(pcm[off:off + BYTES_PER_FRAME])
            if pace:
                # Real time. Sending faster is cheaper and is a different test:
                # the service's endpointer sees a pause length that never
                # happened, which is exactly the kind of flattered measurement
                # this repo has been bitten by before.
                await asyncio.sleep(FRAME_MS / 1000)
        await stream.finish(timeout=10.0)
    finally:
        await stream.close()
    text = finals[-1].transcript if finals else ""
    return text, time.monotonic() - t0, list(stream.errors)


def evaluate(case: Case, transcript: str) -> tuple:
    """Return (ok, got, detail). `ok` is None when the case is a known gap."""
    if case.kind == "time":
        pt = an.parse_time(transcript)
        got = (pt.hour, pt.minute) if pt else None
        return got == case.expect, got, "" if pt else "parse_time returned None"
    matches = an.parse_numbers(transcript)
    got = matches[0].value if matches else None
    if case.expect is None:
        # A documented gap. Recovering nothing is the CORRECT outcome; the
        # failure mode that matters is recovering something wrong.
        return (got is None), got, "expected no value (documented gap)"
    return (got == float(case.expect)), got, ""


async def run(only: int | None, pace: bool, min_gap_s: float = 13.0) -> int:
    if not tts_say.available():
        print("  the Majed Arabic voice is not installed; cannot synthesise the audio")
        return 2

    cases = [CASES[only - 1]] if only else CASES
    rows = []
    print(f"  {len(cases)} utterances, synthesised with {tts_say.VOICE}, "
          f"streamed {'at real time' if pace else 'as fast as possible'} "
          f"to universal-3-5-pro\n")

    last_connect = 0.0
    for i, case in enumerate(cases, 1):
        # Each case is a NEW socket, and the free tier caps new connections at
        # 5 per minute. Twelve cases back to back would trip that and the run
        # would report transcription failures that are really rate limiting,
        # which is the worst kind of wrong result: it looks like a finding.
        wait = min_gap_s - (time.monotonic() - last_connect)
        if last_connect and wait > 0:
            print(f"          (waiting {wait:.0f}s: 5 new connections/minute cap)\n")
            await asyncio.sleep(wait)
        last_connect = time.monotonic()

        spoken = tts_say.speak(case.text)          # 16 kHz PCM16 WAV
        pcm = spoken.audio[tts_say._header_len(spoken.audio):]
        transcript, elapsed, errors = await transcribe(pcm, pace=pace)
        ok, got, detail = evaluate(case, transcript)

        exact = transcript.strip().rstrip("؟.!") == case.text.strip()
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] {i:>2}. {case.text}")
        if not exact:
            print(f"          heard: {transcript or '(nothing)'}")
        print(f"          expected {case.expect!r}, parsed {got!r}"
              + (f"  ({detail})" if detail else "")
              + (f"  [{case.note}]" if case.note else ""))
        if errors:
            print(f"          errors: {errors}")
        rows.append({
            "text": case.text, "kind": case.kind, "expect": case.expect,
            "transcript": transcript, "parsed": got, "ok": ok,
            "transcript_exact": exact, "elapsed_s": round(elapsed, 2),
            "note": case.note,
        })
        print()

    ok_n = sum(1 for r in rows if r["ok"])
    exact_n = sum(1 for r in rows if r["transcript_exact"])
    print(f"  value recovered correctly   {ok_n}/{len(rows)}")
    print(f"  transcript matched verbatim {exact_n}/{len(rows)}")
    print()
    print("  This is a SYNTHETIC speaker on a clean line. It shows the")
    print("  transcription-to-value path holds across many numbers rather than one.")
    print("  It is not evidence of accuracy on real callers, dialect or noise, and")
    print("  must never be quoted as if it were.")

    out = Path(__file__).resolve().parent / "fixtures" / "number_e2e.json"
    out.write_text(json.dumps({"rows": rows, "voice": tts_say.VOICE,
                               "paced": pace}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n  wrote {out.name}")
    return 0 if ok_n == len(rows) else 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--only", type=int, default=None, help="1-based case number")
    p.add_argument("--gap", type=float, default=13.0,
                   help="min seconds between new sockets (free tier: 5/min)")
    p.add_argument("--fast", action="store_true",
                   help="do not pace to real time (cheaper, different endpointing)")
    a = p.parse_args()
    sys.exit(asyncio.run(run(a.only, pace=not a.fast, min_gap_s=a.gap)))


if __name__ == "__main__":
    main()
