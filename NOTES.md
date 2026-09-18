# NOTES — what is built, what is verified, what is not

Written 2026-09-17. Deadline 30 Sep 2026 19:00 GST.

**Status changed during this build.** An API key arrived mid-session, so this is no longer a
doc-driven guess: the client has run against the live AssemblyAI v3 endpoint, transcribed real
Arabic, and the fixtures are now captured traffic rather than authored guesses.

> **Arabic realtime transcription over a Twilio-shaped 8 kHz mu-law stream WORKS, measured, on
> `universal-3-5-pro`. Three live findings changed the design. The LLM turn remains untouched
> and is still the whole latency problem. Fish TTS is out of credit, so stage 3 cannot be
> measured today.**

---

## 1. What is built

| File | State |
|---|---|
| `aai_stream.py` | v3 streaming client. Persistent socket, callbacks, frame aggregation, bounded reconnect, fatal-error handling. |
| `fake_aai_server.py` | Local fake v3 endpoint replaying **captured** frames, with fault injection. |
| `replay.py` | Paced replay driver + `--live` + `--record`. |
| `latency.py` | Three-stage turn budget. |
| `selftest.py` | **91 checks. Exits non-zero on failure.** |
| `make_fixture.py` | Regenerates the authored fixture (superseded by `--record`, kept for history). |

Dependencies: **`numpy` and `websockets` only**, both already installed. Everything else is
stdlib. **No new dependency was added.** `pytest` is deliberately unused — it is not installed
on this machine, and a suite that cannot run is worse than none.

## 2. Actual selftest output

```
$ python3 selftest.py
  fixtures ...
  url ...
  key ...
  parsing ...
  replay ...
  revision ...
  turbo ...
  cleanclose ...
  reconnect ...
  backoff ...
  auth ...
  guards ...
  frames ...
  adapter ...
  latency ...

  91/91 checks passed in 29.7s

  PASS
$ echo $?
0
```

Mutation-tested — verified to actually fail when the guarded bug is reintroduced. Four
mutations run, all caught with exit 1:

| Mutation | Caught by | Symptom |
|---|---|---|
| Add `Bearer ` to the auth header | `url` | `Authorization has NO Bearer prefix` |
| Remove reconnect serialisation | `reconnect` | `2 reconnects - the feed/read race is back` |
| Remove the `_terminated` flag | `cleanclose` | `1 reconnect(s) - an expected close is being retried` |
| Restore derived endpoint lag | `latency` | `endpoint_lag_ms is not negative -- -10.2` |

## 3. Live measurements

Three consecutive live runs, same 4.15 s Arabic clip. **Stages 1 and 2 are real.** Stage 3 is a
stub — see §6.

```
  ACROSS 3 RUNS (ms)
    stage                        min    median       max
    1. to first partial       1152.2    1263.6    1474.7
    2. to end of turn         4410.2    4432.9    4480.8
    3. to first TTS byte      4410.4    4433.1    4481.0   <- STUB, not real
       endpoint lag            469.5     490.8     539.7
       LLM + TTS gap             0.1       0.2       0.2   <- STUB, not real
```

Reading these honestly:

- **Time-to-first-partial ≈ 1.26 s.** Slower than the 100 ms aggregation buffer would suggest,
  so the bulk of it is the service. This is the number that decides whether live captions feel
  responsive.
- **Time-to-end-of-turn ≈ 4.43 s** against 4.15 s of audio. Bounded below by how long the caller
  spoke, so compare it to the speech duration, never to zero.
- **Endpoint lag ≈ 490 ms** (last speech → turn committed). **This answers an open question:
  `call_agent/vad.py` uses a 500 ms endpointing pause, so AssemblyAI's endpointer is
  effectively a wash — neither an upgrade nor a regression.** Note the clip has 200 ms of
  trailing silence, which the endpointer is entitled to use.
- Every run: **0 reconnects, 0 errors**, identical transcript.

## 4. The transcript, verified by reading it

Returned, identically on 4 separate runs:

> هل يمكنكم تأجيل التصوير إلى الساعة تسعة صباحاً؟

Checked rather than assumed:

| Check | Result |
|---|---|
| Word count | 8 vs 8 in source |
| Presentation-form codepoints (the corruption signature) | **0** |
| Bidi control characters | **0** |
| U+FFFD replacement characters | **0** |
| First word | `هل` — the question particle, i.e. **logical order, not reversed** |
| Final character | `؟` ARABIC QUESTION MARK |
| Diacritics preserved | yes (tanween on `صباحاً`) |

All eight words are correct Arabic in correct order. This is genuinely good output.

## 5. Named constraints discovered live

These are the findings that change how anything downstream must be written. They are
constraints, not trivia.

### CONSTRAINT 1 — Twilio's 20 ms frame is rejected. Audio must be aggregated.

```
error_code 3007: Input Duration Error: Input Duration Violation: 20.0 ms.
                 Expected between 50 and 1000 ms
```
and the socket is **closed** with code 3007. This refuted the design's central premise. Frames
are now buffered to 100 ms before sending. The bytes are still never resampled or re-encoded —
mu-law in, mu-law out — but **up to 100 ms is added to stage 1, and that cost is real.**
Also: 3007 is deterministic, so it is treated as fatal and never retried; the first run burned
two reconnects failing identically.

### CONSTRAINT 2 — Numbers come back as Arabic WORDS, never digits.

The audio said "nine". The transcript says **`تسعة`** (tisʿa), not `9`.

**Anything downstream that parses times, dates, quantities, prices or phone numbers must not
expect digits.** A booking agent matching `\d{1,2}` against this transcript finds nothing. This
needs an Arabic number-word parser (`واحد` … `تسعة`, `عشرين`, `مئة`, and compound forms like
`الساعة تسعة والنصف`), and that parser does not exist in this repo or anywhere in the
workspace. It is unbuilt work, not a detail.

Note the asymmetry with TTS: `call_agent/tts_stream.normalise_numerals()` converts Arabic-Indic
digits **to** Western digits on the way out, because Fish mispronounces `٢٠٢٦`. So the pipeline
converts digits→words on output and receives words→never-digits on input. Both directions need
handling and they are not symmetric.

### CONSTRAINT 3 — Partials are REVISED, not merely extended. This breaks prefix logic.

Captured live:

```
partial 1   هل يمكنكم؟
partial 2   هل يمكنكم تأجيل التصوير إلى السنة؟            <- "to the YEAR"
partial 3   هل يمكنكم تأجيل التصوير إلى الساعة تسعة صباحاً؟   <- "to NINE O'CLOCK"
```

Between partials 2 and 3 the service **went back and changed a word it had already emitted**.
`السنة` (the year) became `الساعة تسعة` (nine o'clock) — different words, different meaning,
shown before being withdrawn. There is no retraction event.

**Consequence for barge-in, which is the sharp edge here.** `call_agent` fires barge-in and
intent routing on partial text. Any logic that treats a partial as a stable prefix — keyword
matching, "did they say yes", prefix-diff caption rendering — **can fire on a phrase the service
is about to disown, and there is no way to take the action back.** The agent will have
interrupted, or branched the script, on words the caller never said.

This is a behavioural change from the local Whisper path, where each `quick()` pass was
independent and never implied stability. The safe rule: **act on `end_of_turn`, or accept that
an early action may rest on retracted text.** `t_partials_are_revised_not_just_extended`
asserts this so it cannot silently stop being true.

A second, milder cause of non-prefixing: because every partial is formatted, the trailing `؟`
moves as words are appended. Harmless semantically, still breaks naive diffing.

### CONSTRAINT 4 — `end_of_turn_confidence` is binary, not graded.

Measured **exactly `0.0` on every partial and exactly `1.0` on the final**, across 4 runs. So
`end_of_turn_confidence_threshold` has nothing to tune on this model, and code waiting for
`confidence > 0.7` to act early **will never fire early**. Trust the `end_of_turn` flag.

## 6. Answering the punctuation question directly

**Does `universal-3-5-pro` return Arabic punctuated and formatted? YES — and without being
asked.**

Evidence from what I actually received:

- `turn_is_formatted: true` on **every** turn, including all three partials.
- The text carries a real Arabic question mark `؟` (U+061F).
- Diacritics are preserved (`صباحاً`).
- Numbers are rendered as formatted words (`تسعة`).
- **We never sent `format_turns`.** `build_url()` omits it by default.

So the documented "`format_turns` is not available on `universal-3-5-pro`" is consistent with
what happens, but the practical conclusion is the opposite of what it sounds like: there is no
toggle **because formatting is always on**, not because it is unavailable. My authored fixture
had guessed `false` and was wrong.

Caveat: verified on one sentence, one speaker, one clip. Whether formatting stays this good on
dialect, noise, or long turns is unmeasured.

## 7. Verified vs unverified

### Verified live

- The URL, parameters and model name are all accepted (`speech_model`, `language_codes`,
  `encoding=pcm_mulaw`, `sample_rate=8000`).
- **`Authorization: <key>` with NO `Bearer` prefix is correct.** This was the highest-risk
  assumption in the repo; it is now measured, not believed.
- Arabic transcription works on 8 kHz phone-band mu-law and is accurate on this clip.
- Formatting, binary confidence, partial revision, word-form numbers (§5, §6).
- The 50–1000 ms chunk window, and that violating it closes the socket.
- Session config returned at `Begin`: `model: universal-3-5-pro`, `mode: balanced`,
  `api_version: 2025-05-12`, `speaker_labels: false`, `redact_pii: false`,
  `filter_profanity: false`, `domain: null`, `voice_focus: null`.
- 0 reconnects and 0 errors on clean runs; reconnect fires correctly on an injected drop.
- Endpoint lag is positive and asserted non-negative.

### Still unverified

1. **Accuracy beyond one clip.** One sentence, one synthetic voice, clean audio. Real callers
   have dialect (Gulf vs MSA), background noise, and mobile codecs. Completely unmeasured.
2. **Multi-turn behaviour.** Everything here is a single turn. `turn_order` increments are
   untested.
3. **Reconnect mid-turn on the live service.** A reconnect opens a **new session with no memory
   of the turn**, so the first half of a sentence is lost. The fake papers over this by
   restarting its script. Unsolved and untested live.
4. **Billing rate.** `v2/account` returns `{}`; `v2/account/billing`, `v2/usage`,
   `v2/account/usage`, `v2/billing` all **404**. **No endpoint exposes pricing or usage**, so
   per-second cost is unconfirmed and I am not going to guess it.
5. **Barge-in against the new socket.** Not wired, not measured.
6. **Long-turn and idle-socket behaviour**, and whether idle sockets are dropped server-side.

## 8. What does not work yet

Blunt list.

- **Fish TTS is out of credit — stage 3 cannot be measured.** `POST /v1/tts` returns
  **402 Payment Required**; the wallet reads `credit: 0.000000`, `cumulative_top_up: 0`. It was
  free-trial credit and today's 10:11 selftest appears to have spent the last of it.
  **This supersedes ASSETS.md**, which recorded Fish as working. So every stage-3 number in this
  repo is a labelled stub and **must not be quoted as measured**. Needs a top-up (owner, gated).
- **Nothing is wired into `call_agent/server.py`.** The adapter exists and is tested, but
  `Call.partials()` is still a poll loop. Converting it to a subscriber is the real integration
  work and has not been started. **I did not modify anything outside this directory.**
- **No Arabic number-word parser** (Constraint 2). Required before any booking/time logic works.
- **Barge-in is unsafe as currently written** against revised partials (Constraint 3). The rule
  is documented and tested; the *fix* in `call_agent` is not done.
- **`TranscriberAdapter` is a shim, not an equivalence.** `careful()` has no second, more
  accurate model — it waits for end-of-turn instead. Unmeasured against the `base`/`small` pair.
- **One turn only.** No conversation, no interruption handling.
- **The LLM turn is untouched.** Per ASSETS.md it is 3,878–16,917 ms — still the entire
  remaining latency problem. This project *measures* the budget; it does not fix it.
- **No auth on the media-stream websocket, no Twilio signature validation.** Pre-existing in
  `call_agent`, not addressed here, and a real hole if the tunnel URL leaks.
- **Nothing is deployed.** No public host, no tunnel, no remote, nothing pushed.

## 9. Honest positioning

The strongest **true** claim, now measured:

> AssemblyAI v3 transcribes Arabic from a Twilio-shaped 8 kHz mu-law stream with no resampling
> in the hot path, returning correctly-ordered, punctuated Arabic with a ~490 ms endpoint lag —
> and the turn budget is instrumented in three stages so the real bottleneck is visible rather
> than averaged away.

Claims to avoid:

- **Do not quote 483 ms as response time.** It is acknowledgement latency. Real answers take
  4–17 s because of the LLM.
- **Do not claim zero added latency from "byte-for-byte forwarding".** Aggregation to 100 ms is
  mandatory and costs up to 100 ms.
- **Do not present stage 3 as measured.** Fish is out of credit.
- Be straight that the Arabic angle is **input**, not output: this hears Arabic via AssemblyAI
  and would speak it via Fish. AssemblyAI has no Arabic voice at all.

## 10. Next actions, in order

1. **Top up Fish Audio** (owner, gated) so stage 3 becomes a real measurement.
2. Build the Arabic number-word parser (Constraint 2).
3. Convert `Call.partials()` in `call_agent/server.py` to a subscriber, and **move barge-in
   and intent routing off partials** or gate them on `end_of_turn` (Constraint 3).
4. Test a real multi-turn call and a mid-turn reconnect.
5. Measure accuracy on a genuinely human Arabic recording, not TTS.


---

## 11. The Arabic number-word parser (CONSTRAINT 2, built 2026-09-18)

`arabic_numbers.py` + `test_arabic_numbers.py`. **stdlib only** -- `re`,
`unicodedata`, `typing`. No new dependency, so the repo still installs with
nothing but `numpy` and `websockets`. Runs on the 3.9.6 CommandLineTools
interpreter and on 3.14.7; no `match` statements, no `X | Y` runtime unions.

This closes section 10 item 2. It does not touch anything else in the repo.

### API

```python
parse_numbers(text)    -> list[NumberMatch]   # .value .surface .start .end
normalize_digits(text) -> str                 # number spans -> Western digits
parse_time(text)       -> ParsedTime | None   # .hour .minute .surface .explicit_period
parse_quantity(text)   -> float | None
```

### On the real captured sentence

```
هل يمكنكم تأجيل التصوير إلى الساعة تسعة صباحاً؟
  -> ParsedTime(hour=9, minute=0, surface='الساعة تسعة صباحاً', explicit_period=True)
  -> normalize_digits: هل يمكنكم تأجيل التصوير إلى الساعة 9 صباحاً؟
```

Read off `fixtures/v3_session_arabic.jsonl` by the test itself, not re-typed.
The two **revised** partials from CONSTRAINT 3 are also asserted: `إلى السنة`
("to the year") must yield **no time at all** rather than a wrong one.

### Test output

```
$ python3 test_arabic_numbers.py
  ... 17 sections, every case printed ...
  232/232 passed
$ echo $?
0
```

### Mutation results -- each guard reintroduced as a bug, suite must fail

| Mutation | Caught by | Symptom |
|---|---|---|
| Drop units-first waw handling | section 4 | `خمسة وأربعين` -> `[5.0]` not `[45.0]`, 14 failures |
| Treat ordinal `التاسعة` as unmatched | sections 6/7/8 | `الساعة الثالثة` -> `[]`, 33 failures |
| Allow a substring match inside a longer word | sections 4/11 | `واحد وعشرون` -> `[1.0, 10.0]`, 19 failures |
| Let the clock reader waw-compound the hour | section 7 | `الساعة ثلاثة وعشرين دقيقة` -> 23:00 not 3:20 |
| Skip the 24h conversion | section 8 | `الساعة تسعة مساءً` -> 9 not 21, 10 failures |
| Drop the fraction guard | section 13 | `ثلاثة أرباع` -> 3.0 instead of refusing |
| Let adjacent bare numerals merge | section 11 | `خمسة صفر اثنين` -> one match `7.0` |
| Empty the `_NEVER` blocklist | section 11 | blocklisted words parse as numbers |

All 8 caught with exit 1; reverted to 232/232 exit 0.

**Two findings from doing this rather than asserting it:**

1. **The `_NEVER` blocklist was decorative.** Nothing could reach it -- the
   definite article is never stripped, so `الاثنين` (Monday) could not have
   matched in the first place. It was untested code pretending to be a guard.
   Fixed by testing the *mechanism*: the test injects the word into the lexicon
   the way a future edit would, then asserts the blocklist still refuses it. Now
   it fails when emptied.
2. **A mutation that silently does not mutate looks exactly like an uncaught
   one.** The first `_NEVER` mutation was `for w in () or (...)`, which is
   truthy-fallback and changed nothing, so the harness reported "NOT CAUGHT" for
   a test that was fine. The harness now prints `len(_NEVER)` to prove the
   mutation took effect before trusting its verdict.

### Design calls worth knowing

- **Token matching, never substrings.** `ستارة` (curtain) contains `ست` (six)
  and must not parse as 6. Every lookup is on a whole token.
- **Under-matching beats over-matching.** A wrong number silently books the
  wrong time and nothing downstream can detect it; a missing number at least
  fails loudly.
- **The lexicon is written in ordinary logical-order Arabic and normalised at
  import by the same function used on input**, so lexicon and input cannot drift
  apart. Zero presentation forms (U+FB50-U+FEFF) and zero bidi controls in
  either source file -- asserted by section 17 against the files on disk.
- **`parse_numbers` and `parse_time` deliberately disagree about waw.**
  `ثلاثة وعشرين` is **23** as a quantity but `الساعة ثلاثة وعشرين دقيقة` is
  **3:20**, so the clock reader takes a single hour lexeme and treats the waw
  part as minutes.
- **Feminine ordinals only count under the `الساعة` anchor.** `الساعة الثالثة`
  is 3 o'clock; a bare `الثالثة` is not a number, and `الطابق الثاني` is the
  second floor.

### What it cannot do -- honest ceiling

- **999,999 max for number words.** `مليون` / `مليار` are absent, so
  `مليون درهم` yields nothing rather than something wrong. Digit runs bypass
  this and are read straight through.
- **Clitic prefixes ب / ل / ك / ف are not stripped**, so `بعشرة دراهم` is a
  known miss. Deliberate: `لست` ("I am not") would otherwise strip to `ست` = 6.
- **No fractions.** `ثلاثة أرباع` refuses rather than returning 3.
- **No dates, no ordinal day-of-month, no phone-number grouping.** A spoken run
  `خمسة صفر اثنين` returns three separate matches.
- **AM/PM is never guessed.** With no period word `explicit_period` is False and
  the hour is left exactly as spoken.
- `ليلاً` uses a documented convention (hours 1-4 stay, 12 becomes 0, else +12).
  That is a heuristic, not a measurement.
- **Dialect coverage is thin and largely untested.** `ثنين`, `ثنتين`, `ونص` and
  `الصبح` are in and tested; everything else Gulf/Levantine/Egyptian is not.
- **Untested against real ASR output beyond the one captured sentence.** Every
  other test string is authored. Whether `universal-3-5-pro` spells numbers the
  way this lexicon expects across dialect, noise and long turns is unmeasured --
  same caveat as section 7 item 1.
- **Not wired into anything.** `call_agent/server.py` is still untouched.
