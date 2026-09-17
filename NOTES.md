# NOTES — what is built, what is verified, what is not

Written 2026-09-17. Deadline 30 Sep 2026 19:00 GST.

Read this before believing anything in the README. The short version:

> **The client and the whole test harness are built and pass 81 checks. Nothing here
> has ever touched a live AssemblyAI socket, because there is no account yet. Every
> claim about how the real service behaves is unverified and labelled as such below.**

---

## 1. What is built

| File | Lines | State |
|---|---|---|
| `aai_stream.py` | ~470 | v3 streaming client. Persistent socket, callbacks, bounded reconnect, key loading. |
| `fake_aai_server.py` | ~200 | Local fake v3 endpoint with fault injection. |
| `replay.py` | ~250 | Paced replay driver + `--record` to capture real frames later. |
| `latency.py` | ~330 | Three-stage turn budget. |
| `selftest.py` | ~430 | 81 checks. Exits non-zero on failure. |
| `make_fixture.py` | ~110 | Regenerates the protocol fixture. |

Dependencies: **`numpy` and `websockets` only**, both already installed on this machine
(system python3.14.7 has websockets 16.0 / numpy 2.4.6; `call_agent/.venv` has 17.0.1 / 2.5.2).
Everything else is stdlib. **No new dependency was added.** `pytest` is deliberately not used —
it is not installed anywhere here, and a test suite that cannot run is worse than none.

## 2. Actual selftest output

```
$ python3 selftest.py
  fixtures ...
  url ...
  key ...
  parsing ...
  replay ...
  turbo ...
  cleanclose ...
  reconnect ...
  backoff ...
  auth ...
  guards ...
  frames ...
  adapter ...
  latency ...

  81/81 checks passed in 26.1s

  PASS
$ echo $?
0
```

The tests are mutation-tested, i.e. verified to actually fail when the bug they guard is
reintroduced. Four mutations were run, all correctly caught with exit 1:

| Mutation | Caught by | Symptom |
|---|---|---|
| Add `Bearer ` to the auth header | `url` | `Authorization has NO Bearer prefix` |
| Remove reconnect serialisation | `reconnect` | `2 reconnects - the feed/read race is back` |
| Remove the `_terminated` flag | `cleanclose` | `1 reconnect(s) - an expected close is being retried` |
| Restore the derived endpoint lag | `latency` | `endpoint_lag_ms is not negative -- -10.2` |

## 3. Three bugs found by running it

All three were found by executing the harness, not by reading code. They are the substance of
what this build produced.

**a. One socket drop opened three sockets.** `feed()`'s send-error path and `_read_loop()`'s
error path both called `_reconnect()` with no mutual exclusion. On a service billed per
connection-second with a free-tier cap of 5 new connections/minute, that is a cost and
rate-limit bug. Fixed with a lock plus a connection-generation check. Measured 3 → 1.

**b. Every clean turn opened a second, discarded, billable socket.** After `finish()` the
service closes normally; the read loop treated that expected close as a failure and reconnected.
This did **not** appear in the plain replay test, because there `close()` follows `finish()`
immediately. It needs a delay in between — which on a real call is *always*, since the LLM turn
sits in exactly that window. Surfaced only when `latency.py` ran with a 4 s answer stub.
Measured 1 → 0.

**c. The latency harness could report a negative number.** Endpoint lag was
`to_end_of_turn_ms - audio_seconds*1000` — a recorded event minus a *derived* duration. The
fixture's final frame is a 108-byte partial (13.5 ms) dispatched at t0+4.140 s while
`audio_seconds` says 4.1535 s, producing −10.2 ms. Underneath that sat a worse error: it
measured from end of *stream*, not end of *speech*, and the fixture has 200 ms of trailing
silence, so it **understated** endpoint lag — it flattered the number. Now both `speech_end_at`
and `audio_end_at` are stamped as real events, speech end located by an RMS gate using the same
0.01 threshold `call_agent/simulate_twilio.py` already uses. Measured −10.2 ms → +200.6 ms.

## 4. Verified vs unverified

### Verified — I ran this and watched it work

- The client speaks the documented v3 protocol: `Begin`, `Turn` (partial and final),
  `Termination`.
- Partials **replace** rather than concatenate. Asserted: each partial is a prefix of the next.
- `end_of_turn` fires exactly once and unblocks `finish()`.
- End-of-turn confidence is highest on the final turn (0.931 vs max partial 0.144).
- 8 kHz mu-law frames go out **byte-for-byte**, no resample: 33,228 bytes in, 33,228 received.
- Odd-sized, merged and empty payloads are all handled (Twilio does split and merge).
- A trailing partial frame is kept, not silently dropped.
- Pacing is accurate: 208 frames × 20 ms = 4.16 s, final turn at 4,142.8 ms.
- Reconnect works, is serialised, is bounded, and does **not** fire on an expected close.
- A missing key produces an actionable message, never a traceback.
- The three latency stages move independently (a 1.0 s stub → 1.0 s in stage 3 only).
- The Arabic fixture is genuinely speech: RMS 0.1249, peak 0.855, energy across t=0–4 s.

### Unverified — needs a live API key

**None of the following has been tested. Do not state any of it as fact to a judge.**

1. **That the URL is accepted at all.** Parameter names (`speech_model`, `language_codes`,
   `encoding`, `sample_rate`) are taken from documentation. A single wrong name is a 400.
2. **That `Authorization: <key>` with no Bearer is correct.** This is the single highest-risk
   assumption in the repo. It is asserted in the tests, which means the tests encode a *belief*,
   not a measurement. If it is wrong, one line changes and one test flips.
3. **Whether Arabic comes back punctuated.** `format_turns` is documented as unavailable on
   `universal-3-5-pro`, so the fixture sets `turn_is_formatted: false`. If the live service
   formats anyway, the fixture is wrong. **This is explicitly listed as unknown, not assumed.**
4. **Arabic transcription accuracy on 8 kHz phone-band audio.** Completely unmeasured. Arabic
   ASR on a narrowband line is materially harder than on studio audio, and dialect (Gulf vs MSA)
   is a further unknown. The fixture's "correct" transcript is the text we synthesised, so the
   replay proves plumbing, **not accuracy**.
5. **Real timing for stages 1 and 2.** Every millisecond in this repo is the fake server's
   scripted timing. They measure the instrument, not the service.
6. **End-of-turn behaviour.** Whether AssemblyAI's endpointer is faster or slower than the
   existing 500 ms VAD pause in `call_agent/vad.py` decides whether this is an upgrade or a
   regression for barge-in. Unknown.
7. **Reconnect semantics mid-turn.** A reconnect opens a **new session with no memory of the
   turn so far**, so the first half of a caller's sentence is lost. The fake papers over this
   by restarting its script. This is a real product consequence and it is not solved.
8. **Billing rate and idle cost.** "Billed per connection-duration" shaped the design; the
   actual rate is unconfirmed.

## 5. What does not work yet

Blunt list.

- **There is no API key**, so nothing has run against AssemblyAI. This is the top blocker and
  it is owner-only: creating an account is an external action behind the confirmation gate.
- **Nothing is wired into `call_agent/server.py`.** The adapter exists and is tested, but
  `Call.partials()` is still a poll loop. Converting it to a subscriber is the real integration
  work and it has not been started. **I did not modify anything outside this directory.**
- **`TranscriberAdapter` is a shim, not an equivalence.** `careful()` has no second, more
  accurate model to run — it waits for end-of-turn instead. That is a different trade and it
  has not been measured against the existing `base`/`small` Whisper pair.
- **The protocol fixture is hand-authored, not captured.** It is written to the documented
  schema. `replay.py --live --record` replaces it with real frames the moment a key exists, and
  `fixtures/README.md` says so plainly.
- **The Arabic fixture is TTS, not a human.** It is Fish Audio output pushed to 8 kHz. Real
  callers have accents, background noise, and mobile codecs. Cleaner than reality.
- **One turn only.** No multi-turn conversation, no barge-in integration, no interruption
  handling against the new socket.
- **No TTS in the loop.** Stage 3 is a labelled stub (`stub_llm_tts`). It does not call an LLM
  or a voice. Any budget produced with it is tagged `replay+stub` for exactly that reason.
- **The LLM turn is untouched**, and per ASSETS.md it is 3,878–16,917 ms — the entire remaining
  latency problem. This project measures it; it does not fix it.
- **No auth on the media-stream websocket, and no Twilio signature validation.** Pre-existing
  in `call_agent`, not addressed here, and a real hole if the tunnel URL leaks.
- **Nothing is deployed.** No public host, no tunnel, no remote created, nothing pushed.

## 6. Honest positioning

The strongest true claim: *AssemblyAI v3 takes Twilio's 8 kHz mu-law directly, so Arabic
realtime transcription drops into an existing telephony agent with no resampling in the hot
path, and the turn budget is instrumented so the real bottleneck is visible rather than
averaged away.*

The claim to avoid: **do not quote 483 ms as response time.** It is acknowledgement latency.
Real answers take 4–17 s. A judge who dials the number will discover that in one call, and
overclaiming it would cost more than the number ever bought.

Also worth being straight about: the Arabic angle here is *input*, not output. This agent hears
Arabic via AssemblyAI and speaks it via Fish Audio. AssemblyAI does not speak Arabic at all yet.

## 7. Next actions, in order

1. **Owner creates the AssemblyAI account** and writes the key to
   `~/jarvis/.credentials/assemblyai_key` (`chmod 600`). Everything below is blocked on this.
2. `python3 replay.py --live --record` — first real contact. Confirms or refutes unverified
   items 1, 2 and 3 in one run, and captures a real fixture.
3. `python3 latency.py --live --n 5` — real numbers for stages 1 and 2. Mind the
   5-connections/minute cap; `--gap` defaults to 13 s for that reason.
4. Listen to the Arabic transcript and judge accuracy by reading it, not by exit code.
5. Only then convert `Call.partials()` in `call_agent/server.py` to a subscriber.
