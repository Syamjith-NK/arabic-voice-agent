# Arabic realtime voice agent, on AssemblyAI streaming

Built for the **AssemblyAI Voice Agent Hackathon** (lablab.ai, submission 30 Sep 2026).

**An Arabic voice agent is not an English one with the language code changed.**
This repo is the measurements that prove it, and a working agent built around
them.

Everything below was measured against the live API and is encoded as a test, so
it cannot silently stop being true. Nothing in this README is quoted from
documentation.

```sh
python3 selftest.py            #  97 checks, no API key needed
python3 test_arabic_numbers.py # 232 checks
python3 test_agent.py          # 352 checks
```

---

## Why this does not use the Voice Agent API

AssemblyAI's managed Voice Agent API is the right call for English and the wrong
call here, for a reason that is checkable in their own docs and was re-verified
on 2026-09-18:

| | Voice Agent API (managed) | v3 streaming (`/v3/ws`) |
|---|---|---|
| Arabic **input** | yes, 18 input languages | yes, on `universal-3-5-pro` only |
| Arabic **output voice** | **no.** 16 voices, 11 English and 5 European. Arabic is "coming soon" | n/a, transcription only |
| Auth header | `Authorization: Bearer <key>` | `Authorization: <key>`, **no Bearer prefix** |

So a managed Arabic agent hears Arabic and answers in a European voice. A judge
who tests it hears exactly that. That single fact forces a different
architecture, and everything else here follows from it.

`universal-streaming`, the default model, has **no Arabic at all**. Leaving the
model unset gives you an English-only socket that returns confident nonsense for
Arabic speech, with no error.

## The four findings

| # | Finding | Consequence |
|---|---|---|
| 1 | **Numbers come back as Arabic words**, never digits. The audio said nine; the transcript says `تسعة`. | Any agent matching `\d` finds nothing, books no time, and raises no error. |
| 2 | **Partials are revised, not extended.** `إلى السنة` ("to the year") became `إلى الساعة تسعة` ("to nine o'clock"), with no retraction event. | Prefix-matched barge-in fires on words the caller never said, and cannot be undone. |
| 3 | **Twilio's native 20 ms frame is rejected**, `error_code 3007`, and the socket closes. Legal range is 50 to 1000 ms. | "Forward the phone bytes unchanged" does not work. Aggregation is mandatory and costs up to 100 ms. |
| 4 | **Arabic returns fully punctuated and formatted** on every partial, although `format_turns` is documented as unavailable on this model and was never sent. | The doc reads backwards: there is no toggle because formatting is always on. |

Two more, measured since:

- **One socket carries a whole conversation.** Two utterances, `turn_order` 0 then 1, zero reconnects. This matters because the free tier caps *new* connections at 5 per minute, so a socket-per-turn design rate-limits itself after five exchanges. `multiturn_probe.py`.
- **A browser can hold the Arabic socket directly**, with a 60 second token and no `Authorization` header. That is why the demo can be hosted with no server at all.

And one the docs do not mention: the service emits an undocumented
**`SpeechStarted`** event, which is a better origin for a latency measurement
than socket-open, because it is the service's own opinion of when speech began.

## The latency, as three numbers that are never blended

An earlier README in this workspace quoted **483 ms** turn latency. That number
is real and it is the wrong number to quote: it is *acknowledgement* latency, the
time to play a pre-baked "one moment" from memory. Real answers took **3,878 to
16,917 ms**, essentially all of it the LLM.

So this repo reports three separate stages and refuses to produce a single
figure. Measured live on the captured Arabic:

```
  1. to first partial      ~1,240 ms     AssemblyAI
  2. to end of turn        ~4,600 ms     AssemblyAI (bounded below by how long the caller spoke)
     endpoint lag            575-775 ms  caller stops -> turn committed
  3. LLM + TTS               474-586 ms  ours
```

**End of speech to a spoken Arabic answer is about 1.05 to 1.36 seconds.** Not
from a faster model: from not calling one. The agent is deterministic, so the
rules path answers in **0.018 ms** median, and the voice costs a flat ~450 ms
regardless of reply length.

Stage 3 marks *complete* audio rather than a first byte, because the macOS voice
does not stream. That is a harder bar than a streaming vendor's first-byte
figure, so the number is pessimistic rather than flattering, and the two should
not be compared without saying which is which.

## What is in here

| | |
|---|---|
| `aai_stream.py` | v3 streaming client. Persistent socket, frame aggregation, bounded serialised reconnect, fatal-error handling. |
| `agent.py` | Arabic booking agent. Deterministic slot filling; a local LLM only for off-script turns, fenced by shape. |
| `arabic_numbers.py` | Arabic number-word parser. Clock Arabic, quantity Arabic, ordinals, waw compounds. |
| `tts_say.py` | Arabic speech with no vendor, no key and no bill. |
| `server.py` | Browser mic to ASR to agent, and serves `web/` from the same port. |
| `api/token.py`, `api/agent.py` | The serverless shape: mint a token, take one turn. |
| `web/` | The demo. No npm, no build step, no framework, no CDN. |
| `latency.py` | The three-stage turn budget. |
| `number_e2e.py` | Does the parser recover what the API actually returns? |
| `multiturn_probe.py` | Does one socket carry a conversation? |
| `NOTES.md` | What is verified, what is not, and what does not work. |
| `SUBMISSION.md` | The submission plan, including what is deliberately admitted. |

Dependencies: **`numpy` and `websockets`**. Everything else is stdlib. The agent,
the parser and both serverless functions are stdlib only.

## The demo

```sh
python3 api_local.py            # the hosted shape, locally, on :8812
python3 api_local.py --check    # exercise both endpoints for real, exit non-zero on failure
python3 server.py               # the websocket shape, on :8811
```

Open `?mock=1` for the whole UI with no server, no key and no microphone, driven
by the real captured session. Open `?direct=1` for the browser holding the
AssemblyAI socket itself.

The page shows the conversation, and beside it the three findings happening
live: the partial stream with **revisions marked**, the raw Arabic beside the
parsed slots so `تسعة` becoming `9` is visible, and the latency strip as three
numbers. Nothing on that panel is decoration, and anything not measured on that
turn is tagged `sim`.

## Proof that the pieces work together

`CONSTRAINT 2` was measured on exactly one sentence and the parser's suite is
authored strings. Separately verified is not verified together, so
`number_e2e.py` synthesises twelve utterances, streams them to the live socket,
and asks whether the parser recovers the value that was spoken.

**12 of 12 values recovered. Only 7 of 12 transcripts came back verbatim.**

That gap is the interesting half. The service returned `إلا ربع` for `إلا ربعاً`,
`وربع` for `والربع`, `خمسمائة` for `خمسمئة`, and `واربعين` without the hamza:
real orthographic variation, and the parser recovered the right value through all
of it. A parser written against the one captured spelling would have looked
perfect on the fixture and failed here.

This is a **synthetic speaker on a clean line**. It shows the path holds across
many numbers rather than one. It is not evidence of accuracy on real callers,
dialect or noise, and is not quoted as if it were.

## What does not work

- **Accuracy beyond synthetic audio is unmeasured.** No real callers, no dialect variety, no noise, no mobile codec.
- **Real microphone capture is unverified.** Chrome's fake device exercised the worklet, the resampler and the framing; it emits a tone, not speech. No Arabic has gone through a real microphone on the browser path.
- **The agent has no calendar.** It will happily confirm a slot that is already booked, and `done` writes nothing anywhere.
- **The agent still sounds like an agent.** It returns the salaam and echoes what it heard, which moved it from being clocked as a machine in two turns to four or five. The residual tell is sentence shape, and the clock speaks formal MSA ordinals inside an otherwise Gulf-colloquial script.
- **Mid-turn reconnect loses the first half of a sentence**, because a new session has no memory of the old one. Untested live.
- **The voice is a system voice.** Intelligible, not warm.
- **The token mint is public and cannot be made private**, because a secret shipped to a browser is not a secret. TTL and rate limits are speed bumps; the account spend cap is the only real bound.

More detail, including everything that was tried and did not work, is in
`NOTES.md`.

MIT licensed.
