# Arabic realtime voice agent on AssemblyAI streaming

Built for the **AssemblyAI Voice Agent Hackathon** (lablab.ai, submission 30 Sep 2026).

The premise in one line: **AssemblyAI's managed Voice Agent API cannot speak Arabic yet**, so
this project does not use it. It uses the piece that *does* handle Arabic — the v3 realtime
streaming transcription socket on `universal-3-5-pro` — and wires it into a telephony loop
that already works end to end over Twilio Media Streams.

## Why not the Voice Agent API

Verified 2026-09-17:

| | Voice Agent API (managed) | v3 streaming (`/v3/ws`) |
|---|---|---|
| Arabic **input** | yes (18 input languages) | yes, on `universal-3-5-pro` only |
| Arabic **output voice** | **no** — 6 output voices, all European, Arabic "coming soon" | n/a, transcription only |
| Auth header | `Authorization: Bearer <key>` | `Authorization: <key>` — **no `Bearer` prefix** |
| Accepts 8 kHz mu-law | — | **yes**, `encoding=pcm_mulaw&sample_rate=8000` |

That last row is the reason this is worth building. Twilio Media Streams already delivers
8 kHz mu-law in 160-byte frames. AssemblyAI v3 accepts exactly that. There is no resample, no
ffmpeg subprocess, and no format conversion anywhere in the hot path — the bytes off the phone
line go onto the AssemblyAI socket unchanged.

`universal-streaming` (the default model) has **no Arabic**. Arabic works only on
`speech_model=universal-3-5-pro`.

## What is in here

| File | What it does |
|---|---|
| `aai_stream.py` | v3 streaming websocket client. Persistent socket, callbacks, reconnection, key loading. |
| `fake_aai_server.py` | Local websocket server that replays captured v3 protocol frames. Lets the whole path run with **no API key and no phone call**. |
| `replay.py` | Feeds a recorded 8 kHz mu-law file through the client at true 20 ms pace. |
| `selftest.py` | Runs the whole thing. **Exits non-zero on failure.** |
| `latency.py` | Splits the turn budget into three separately measured numbers. |
| `fixtures/` | Real Arabic phone-band audio + a recorded protocol script. |
| `NOTES.md` | What is verified, what is not, and what needs a live key. |

## Run it

No API key needed for any of this:

```sh
python3 selftest.py          # full replay path, exit 0 = pass
python3 latency.py --replay  # three-stage turn budget against the fake server
```

With a real key (owner must create the account and drop the key at
`~/jarvis/.credentials/assemblyai_key`):

```sh
python3 replay.py --live     # same audio, real AssemblyAI socket
python3 latency.py --live
```

## The latency claim, stated honestly

An earlier README in this workspace quoted **483 ms** turn latency. That number is real but it
is the **acknowledgement** latency — the agent plays a pre-baked "One moment." from memory in
~0.2 ms, and the endpointing pause accounts for most of the rest. Measured end-of-speech to a
real *answer*, across 7 logged turns on live calls, is **3,878–16,917 ms**, and essentially all
of it is the LLM turn.

`latency.py` therefore reports three separate numbers and refuses to blend them:

1. **time-to-first-partial** — how fast the transcript starts appearing
2. **time-to-end-of-turn** — when the ASR commits the turn as finished
3. **time-to-first-TTS-byte** — when the caller actually hears the answer

Stages 1 and 2 are what AssemblyAI is responsible for. Stage 3 minus stage 2 is the LLM, and
that gap is the entire remaining problem.

## Dependencies

`numpy` and `websockets` only, both already installed. Everything else is stdlib. See NOTES.md.

## Status

See `NOTES.md`. Short version: the client and the whole replay harness are built and tested
against a fake server; **nothing has touched a live AssemblyAI socket yet, because there is no
account.** Every claim about real API behaviour in this repo is labelled as unverified.

MIT licensed.
