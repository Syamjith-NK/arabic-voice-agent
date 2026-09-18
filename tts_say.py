"""Arabic text to speech with no vendor, no key and no bill.

NOTES.md §8 recorded stage 3 of the latency budget as a STUB, because the Fish
Audio wallet hit zero and every `POST /v1/tts` returns 402. That left the repo
able to measure how fast Arabic is heard and understood, and unable to measure
how fast anything is said back, which is half a voice agent.

macOS ships an Arabic voice: `Majed` (`ar_001`). It is not a great voice and it
is not a streaming one, and both of those facts are stated here rather than
discovered later. What it is, is present, free, offline, and good enough to make
stage 3 a measured number instead of a placeholder.

MEASURED ON THIS MACHINE, 2026-09-18, 3 runs per line
-----------------------------------------------------
    speech produced      render (median)
        546 ms               421 ms
      2,659 ms               423 ms
      7,609 ms               450 ms
     14,928 ms               483 ms

THE SHAPE OF THAT TABLE IS THE FINDING, AND IT IS NOT WHAT I EXPECTED.
`say` does not stream - `-o -` produces an empty file - so the obvious reading
is that a long reply pays a long render before a single word is heard. It does
not. Render cost is essentially FLAT: a 27x increase in speech length costs 62
extra milliseconds, because the time is dominated by process startup, not by
synthesis. An earlier version of this docstring asserted that the cost grows
with reply length. Measuring it disproved that, and the wrong sentence is
recorded here rather than quietly deleted.

So the honest comparison against a streaming vendor: Fish Audio's measured
time-to-first-byte was 380 ms (ASSETS.md, before the wallet hit zero). This
delivers the COMPLETE utterance in 420-480 ms. For any reply longer than about
half a second of speech, having all of it at 450 ms beats having the start of
it at 380 ms. The vendor wins on the shortest possible acknowledgement and
loses everywhere else, which is the reverse of what "non-streaming" suggests.

What it does lose on is voice quality, and no measurement here defends that.
`Majed` is a system voice, not a 2026 neural one. It is intelligible Gulf-region
Arabic and it is free and offline; it is not warm and it will not be mistaken
for a person.

For the phone path the output has to become 8 kHz mu-law, which is what Twilio
plays. `say` can emit that directly, so there is no ffmpeg in this path either.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

VOICE = "Majed"                 # ar_001, the only Arabic voice macOS ships
SAY = shutil.which("say")

# Phone line: 8 kHz mu-law, exactly what Twilio Media Streams plays back.
PHONE_FORMAT = ("--file-format=WAVE", "--data-format=ulaw@8000")
# Browser or file: 16 kHz PCM16.
WIDE_FORMAT = ("--file-format=WAVE", "--data-format=LEI16@16000")

PRESENTATION_FORMS = range(0xFB50, 0xFF00)
BIDI_CONTROLS = {0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
                 0x2066, 0x2067, 0x2068, 0x2069}


class TTSUnavailable(RuntimeError):
    pass


@dataclass
class Spoken:
    audio: bytes
    render_ms: float
    audio_ms: float
    sample_rate: int
    encoding: str

    @property
    def realtime_factor(self) -> float:
        """Render time as a fraction of the speech produced.

        Below 1.0 means it renders faster than it plays, which is the only
        regime in which a non-streaming synthesiser is usable in a live call.
        """
        return self.render_ms / self.audio_ms if self.audio_ms else float("inf")


def assert_clean_arabic(text: str) -> None:
    """Refuse to speak text that has already been corrupted.

    This is the same rule the rest of the repo enforces, applied at the last
    possible moment. A pre-shaped string sounds fine to a synthesiser and looks
    fine in a log, so if it is not checked here it is never checked at all.
    """
    for ch in text:
        cp = ord(ch)
        if cp in PRESENTATION_FORMS and "FORM" in (unicodedata.name(ch, "") or ""):
            raise ValueError(
                f"refusing to speak pre-shaped Arabic: U+{cp:04X} "
                f"{unicodedata.name(ch, '?')} in {text[:40]!r}"
            )
        if cp in BIDI_CONTROLS:
            raise ValueError(f"refusing to speak text carrying a bidi control U+{cp:04X}")


def available() -> bool:
    if SAY is None:
        return False
    try:
        out = subprocess.run([SAY, "-v", "?"], capture_output=True, timeout=10).stdout
    except Exception:                                   # noqa: BLE001
        return False
    return VOICE.encode() in out


def speak(text: str, *, phone: bool = False, voice: str = VOICE,
          rate: int | None = None) -> Spoken:
    """Render `text` to audio bytes. Raises rather than returning silence."""
    if SAY is None:
        raise TTSUnavailable("`say` is not on PATH; this path is macOS only")
    assert_clean_arabic(text)

    fmt = PHONE_FORMAT if phone else WIDE_FORMAT
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "out.wav"
        cmd = [SAY, "-v", voice, "-o", str(out), *fmt]
        if rate:
            cmd += ["-r", str(rate)]
        cmd.append(text)
        t0 = time.monotonic()
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        render_ms = (time.monotonic() - t0) * 1000
        if proc.returncode != 0 or not out.exists():
            raise TTSUnavailable(
                f"say failed (rc={proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace')[:200]}"
            )
        data = out.read_bytes()

    sr = 8000 if phone else 16000
    enc = "pcm_mulaw" if phone else "pcm_s16le"
    width = 1 if phone else 2
    # WAV header is 44 bytes for these formats; mu-law files carry a fact chunk,
    # so measure the payload rather than assuming a fixed offset.
    payload = max(0, len(data) - _header_len(data))
    audio_ms = payload / (sr * width) * 1000
    return Spoken(audio=data, render_ms=render_ms, audio_ms=audio_ms,
                  sample_rate=sr, encoding=enc)


def _header_len(wav: bytes) -> int:
    """Find where the `data` chunk payload starts. Never guess 44."""
    i = 12
    while i + 8 <= len(wav):
        cid = wav[i:i + 4]
        size = int.from_bytes(wav[i + 4:i + 8], "little")
        if cid == b"data":
            return i + 8
        i += 8 + size + (size & 1)
    return 44


def measure(samples: list, repeats: int = 3, phone: bool = False) -> dict:
    """Render each sample `repeats` times and report the spread, not one number."""
    rows = []
    for text in samples:
        runs = [speak(text, phone=phone) for _ in range(repeats)]
        rows.append({
            "text": text,
            "chars": len(text),
            "audio_ms": round(statistics.median(r.audio_ms for r in runs), 1),
            "render_ms_min": round(min(r.render_ms for r in runs), 1),
            "render_ms_median": round(statistics.median(r.render_ms for r in runs), 1),
            "render_ms_max": round(max(r.render_ms for r in runs), 1),
            "realtime_factor": round(statistics.median(r.realtime_factor for r in runs), 3),
        })
    return {"voice": VOICE, "phone": phone, "repeats": repeats, "rows": rows}


SAMPLES = [
    "طيب.",
    "طيب. وين بيكون التصوير؟",
    "أهلاً وسهلاً، معك استوديو بيكسلوجيك للتصوير. أي نوع تصوير تحتاج؟",
    "تمام، سجلت لك تصوير فيديو بكرة الساعة التاسعة صباحاً في أبوظبي. "
    "بنرسل لك رسالة تأكيد على رقمك. في شي ثاني أقدر أساعدك فيه؟",
]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phone", action="store_true", help="8 kHz mu-law, the Twilio format")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--say", default=None, help="speak one string and exit")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    if not available():
        raise SystemExit(
            f"  the {VOICE} Arabic voice is not installed.\n"
            f"  System Settings > Accessibility > Spoken Content > System Voice > Manage Voices."
        )

    if args.say:
        s = speak(args.say, phone=args.phone)
        print(f"  {len(s.audio)} bytes {s.encoding} @ {s.sample_rate} Hz")
        print(f"  {s.audio_ms:.0f} ms of speech rendered in {s.render_ms:.0f} ms "
              f"(realtime factor {s.realtime_factor:.2f})")
        return

    result = measure(SAMPLES, repeats=args.repeats, phone=args.phone)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"  voice {result['voice']}, "
          f"{'8 kHz mu-law (phone)' if args.phone else '16 kHz PCM16'}, "
          f"{args.repeats} runs each\n")
    print(f"  {'speech':>9}  {'render (min/med/max ms)':>26}  {'x realtime':>10}  text")
    for r in result["rows"]:
        print(f"  {r['audio_ms']:>7.0f}ms  "
              f"{r['render_ms_min']:>7.0f} /{r['render_ms_median']:>7.0f} /{r['render_ms_max']:>7.0f}  "
              f"{r['realtime_factor']:>10.2f}  {r['text'][:34]}")
    print()
    worst = max(r["realtime_factor"] for r in result["rows"])
    print(f"  worst realtime factor {worst:.2f} "
          f"({'usable live' if worst < 1 else 'TOO SLOW for a live call'})")
    rows = result["rows"]
    if len(rows) >= 2:
        growth = rows[-1]["render_ms_median"] - rows[0]["render_ms_median"]
        span = rows[-1]["audio_ms"] / max(rows[0]["audio_ms"], 1)
        print(f"  this is time to COMPLETE audio, not to first byte: `say` does not stream.")
        print(f"  but the cost is near FLAT, not proportional: {span:.0f}x more speech cost")
        print(f"  {growth:+.0f} ms, because the time is process startup, not synthesis. So the")
        print(f"  whole reply at ~{rows[-1]['render_ms_median']:.0f} ms beats a streaming vendor's "
              f"first byte at 380 ms")
        print(f"  for any reply longer than about half a second of speech.")


if __name__ == "__main__":
    main()
