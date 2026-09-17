# Fixtures — provenance

## `arabic_caller_8k.ulaw` — REAL AUDIO

4.15 s, 33,228 bytes, 8 kHz mono G.711 mu-law, 207.7 frames of 20 ms.
Measured RMS 0.1249, peak 0.855, speech energy across t=0–4 s with a silent tail.

**Where it came from:** `call_agent/out/caller_4.mp3`, written 2026-09-17 10:11 by a real run
of `call_agent/selftest.py` on this machine. That is turn 4 of the selftest, the Arabic turn:

> هل يمكنكم تأجيل التصوير إلى الساعة 9 صباحاً؟
> *("Can you postpone the shoot to 9 in the morning?")*

Converted with `ffmpeg -ar 8000 -ac 1 -f mulaw`. Nothing else was done to it — no trim, no
normalisation, no denoise. It is Fish Audio TTS pushed down to telephone bandwidth, which is
the same degradation a real phone line applies, and it is the audio the existing agent's own
selftest transcribed successfully.

It is synthesised speech, not a recording of a human. That is a real limitation and it is
stated in NOTES.md: TTS Arabic is cleaner than a live caller on a mobile in a car.

## `v3_session_arabic.jsonl` — HAND-AUTHORED, **NOT CAPTURED**

Be clear about this one. These are **not** frames captured from a live AssemblyAI socket,
because there is no AssemblyAI account yet. They are written to the **documented** v3 message
schema (`Begin` / `Turn` / `Termination`) so the client's parsing, partial-vs-final handling,
end-of-turn confidence and callback plumbing can all be exercised offline.

What this fixture **does** prove:
- the client parses the documented schema correctly
- partials replace rather than concatenate
- `end_of_turn` fires exactly once and unblocks `finish()`
- the adapter's `quick()`/`careful()` behave as documented
- the three latency stages are measured from the right events

What it **does not** prove:
- that AssemblyAI's real Arabic output looks like this
- that Arabic returns punctuated (`format_turns` is documented as unavailable on
  `universal-3-5-pro`, so this is genuinely unknown)
- any real-world timing whatsoever

**Once a key exists, `replay.py --live --record` overwrites this file with genuinely captured
frames, and every test here becomes a real regression test.** Until then, treat the timings in
it as a rehearsal, not a measurement.

The `after_bytes` field is what makes replay deterministic: the fake server holds each message
until it has received that many bytes of audio. Since the audio is fed at true 20 ms pace,
byte counts are a proxy for wall-clock position in the utterance without depending on the
machine's speed.
