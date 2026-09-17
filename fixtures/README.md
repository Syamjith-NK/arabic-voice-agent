# Fixtures — provenance

## `arabic_caller_8k.ulaw` — REAL AUDIO

4.15 s, 33,228 bytes, 8 kHz mono G.711 mu-law, 207.7 frames of 20 ms.
Measured RMS 0.1249, peak 0.855, speech energy across t=0–4 s with a silent tail
(speech actually ends at frame 197, so there are 200 ms of trailing silence).

**Where it came from:** `call_agent/out/caller_4.mp3`, written 2026-09-17 10:11 by a real run
of `call_agent/selftest.py` on this machine. That is turn 4 of the selftest, the Arabic turn:

> هل يمكنكم تأجيل التصوير إلى الساعة 9 صباحاً؟
> *("Can you postpone the shoot to 9 in the morning?")*

Converted with `ffmpeg -ar 8000 -ac 1 -f mulaw`. Nothing else was done to it — no trim, no
normalisation, no denoise.

It is synthesised speech, not a recording of a human. That is a real limitation: TTS Arabic is
cleaner than a live caller on a mobile in a car.

## `v3_session_arabic.jsonl` — **CAPTURED FROM THE LIVE API 2026-09-17**

**This file used to be hand-authored. It is not any more.** It was replaced by
`python3 replay.py --live --record` against the real endpoint, and the recording corrected
three things the authored version had wrong.

| | Authored guess | What the API actually returns |
|---|---|---|
| `turn_is_formatted` | `false` | **`true`, on every turn including partials** |
| `end_of_turn_confidence` | ramped 0.069 → 0.144 → 0.931 | **binary: exactly `0.0` on partials, `1.0` on the final** |
| Partial progression | strict prefixes | **revised** — words get retracted and replaced |
| Number rendering | digit `9` | **the Arabic word `تسعة`** |

Captured content:

```
after_bytes  type         eot    fmt   conf   transcript
0            Begin         -      -     -
11840        Turn        false   true   0.0   هل يمكنكم؟
22880        Turn        false   true   0.0   هل يمكنكم تأجيل التصوير إلى السنة؟
33120        Turn        false   true   0.0   هل يمكنكم تأجيل التصوير إلى الساعة تسعة صباحاً؟
33228        Turn        true    true   1.0   هل يمكنكم تأجيل التصوير إلى الساعة تسعة صباحاً؟
33228        Termination   -      -     -
```

Look at partials 2 and 3. `إلى السنة` ("to the year") becomes `إلى الساعة تسعة` ("to nine
o'clock"). The service **retracted a word it had already emitted**. See NOTES.md §5 for why
that matters for barge-in.

The `after_bytes` field makes replay deterministic: the fake server holds each message until it
has received that many bytes of audio. Since the driver paces frames at true 20 ms, byte counts
are a proxy for position in the utterance that does not depend on machine speed. On a recorded
fixture these are reconstructed from each turn's arrival time — the service does not tell us how
much audio it had consumed when it decided to speak.

### What the replay tests now prove

Because these frames are real, the replay suite is a genuine regression test of the service's
observed behaviour, not a rehearsal. It still does **not** prove anything about timing — the
fake replays on byte gates, so all latency numbers from replay are the instrument, not the
service. Live timings are in NOTES.md §3.
