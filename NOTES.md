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

---

## 12. The agent (`agent.py`), added 2026-09-18

Answers the "no conversation, no intent, no reply" gap in §8. Deterministic
slot-filling booking agent for a UAE photo/video studio. Stdlib only, no new
dependency. `test_agent.py`: **275/275, exit 0**.

### The measured case for rules-first

| Path | Measured |
|---|---|
| Rules turn (median of 400) | **0.018 ms** |
| Rules turn p95 / max | 0.030 ms / 0.068 ms |
| `arabic_numbers.parse_time` alone | 5.6 us |
| Ollama `qwen2.5:7b-instruct`, **warm** | **1,431-1,940 ms** |
| Ollama `qwen2.5:7b-instruct`, **cold** | **8,226 ms** |

So a normal turn is roughly **fifty thousand times cheaper** than one LLM call,
and the LLM is only reached when pattern matching extracts nothing. `llm=None` is
a fully working agent. Note the cold number: the default 2.5 s timeout means the
first LLM call after boot **will** time out and fall back to the scripted line.
That is the intended behaviour - a scripted clarification beats 8 s of silence -
but it means the model wants pre-warming if the LLM path is to be usable at all.

### CONSTRAINT 5 - the local LLM lies, and it lied on the first two calls

Not a hypothetical. The first two real replies from `qwen2.5:7b-instruct`, to the
agent's own prompt:

```
تبلغ تكلفة التصوير معنا 200 درهم إماراتي.        <- invented a price
ساعات عمل استوديو التصوير是从周一到周五上午9点...   <- switched to Chinese
```

A later live run leaked **Spanish** (`aparcar`) into an Arabic sentence. Three
different failures in a handful of calls.

The price one is the dangerous one: there is no price list anywhere in this repo,
so every figure it emits is fabricated, and a fabricated quote spoken to a caller
is a commercial commitment. The fix is not a blocklist of lies. **The LLM's role
is fenced by SHAPE**: its only legal output is a short clarifying *question*.
`llm_output_is_safe()` refuses anything that (1) is not a question, (2) contains
any digit or currency unit, (3) contains a non-Arabic script, or (4) is long or
multi-line. Discarding costs nothing because the scripted clarification is always
available, so the gate is deliberately strict.

Worth noting: **the Chinese reply passes `verify_arabic()` cleanly** - no
presentation forms, no bidi controls. Script purity is a genuinely separate gate,
not a second corruption check.

### CONSTRAINT 3 enforced, not just documented

`BookingAgent.handle_turn()` **raises** on a partial, and `ACTS_ON` is readable
by the integration. Partial-driven barge-in stays the caller's business; nothing
in the agent can act on revised text.

### The thesis, enforced in code

Every reply passes `verify_arabic()` before it is returned, and the agent
**raises** rather than speaking corrupted Arabic. Presentation forms are derived
from `unicodedata.name()`, not from the U+FB50-U+FEFF range, so the ornate
parentheses U+FD3E/U+FD3F that enclose a Quranic quotation are **not** flagged -
the range-based version of this check is what produced a 35.6% false-positive
corruption rate against Islamic heritage text in an earlier audit in this
workspace. There is a test asserting they pass.

### Mutation-tested

Seven mutations, every one caught with exit 1. **Three initially SURVIVED** and
the tests were strengthened until they did not - the pass count was flattering
the suite:

| Mutation | First run | Isolating test added |
|---|---|---|
| `verify_arabic` stops scanning | caught | - |
| LLM allowed to assert, not just ask | caught | - |
| LLM digit gate removed | **survived** | Arabic-Indic `٩`, which clears the script and currency gates |
| Script-purity gate removed | caught | - |
| `load_state` trusts any state string | **survived** | assert the state that comes OUT is always valid |
| `_apply` mutates the caller's dict | caught | - |
| Correction takes the REJECTED value | **survived** | `فيديو مو فوتوغرافي` - the time case passes either way |

That last one is the instructive one. `الساعة عشرة مو تسعة` is the obvious
correction test and it **cannot** detect the bug, because a time parser reading
left to right lands on the right answer whether or not the text after `مو` is
discarded. The service table is scanned in a fixed order with photo first, so
`فيديو مو فوتوغرافي` resolves to the rejected service unless the split works.

### Dialogue pass, same day

Three fixes, all from reading the transcript rather than from a spec:

1.  **The salaam is returned.** `السلام عليكم` -> `وعليكم السلام`, plus مرحبا ->
    مرحبتين, صباح الخير -> صباح النور, مساء الخير -> مساء النور. It is PREPENDED
    in `_reply()`, the single place every reply passes through, so it cannot be
    forgotten on the confirm, fix or off-script branches, and a caller who says
    "السلام عليكم، أبغى تصوير فيديو" gets the greeting AND the next question.
    A greeting-only turn does not spend a slot attempt, because being polite is
    not a failed answer.

    **The trap this created, caught before it shipped: `مساء الخير` contains
    `مساء`, which is also the evening marker.** Left in place, saying "good
    evening" books the shoot for 6pm. The greeting is therefore STRIPPED from the
    text before slot extraction sees it, and there is a test asserting that
    `مساء الخير، الساعة تسعة` still asks which half of the day it is, while a
    bare `مساءً` still resolves to 21:00.

2.  **The acknowledgement is no longer a metronome.** It used to cycle
    طيب/ممتاز/زين/تمام on `self._turn`, so it emitted an approving word after
    every single utterance in the same order forever. Now it fires only when the
    caller actually supplied something, and the word is derived from the extracted
    content via `zlib.crc32` - `crc32` and not `hash()`, because `hash()` is
    salted per process and `export_state()` round-trip equality is asserted across
    separate agents.

3.  **A turn that fills two or more slots is echoed** before the next question:
    `تمام، بكرة الساعة التاسعة صباحاً. وين بيكون التصوير؟` This is functional as
    much as warm - it is the caller's first chance to catch a misheard time,
    instead of discovering it at the final readback four questions later. Skipped
    when the next step is the readback (the readback already is the echo), and an
    unresolved am/pm is never echoed as though it were settled.

### Never ask the same question twice in the same words

Found by watching the demo, not by reading a test. The greeting lists the four
service options; the caller answered with a TIME; the agent replied by reading
the identical four-option menu back word for word. Logically correct - no service
was given, so re-asking is right - and it reads as a crash.

Fixed as a general rule, not a special case for that pair. Every slot has three
phrasings indexed by how many times it has already been asked: **[0] full, with
the option list · [1] short, list dropped because it was already read out ·
[2] list offered again**, since by the third time the caller may genuinely not
have heard it. A re-ask also always names what WAS understood, because that is
the reassuring part and it is the one thing the agent got right:

```
before:  طيب. أي نوع تصوير تحتاج؟ فوتوغرافي، فيديو، مقابلة، أو تصوير منتجات؟
after:   ممتاز، سجلت الساعة التاسعة صباحاً. بس أي نوع تصوير بالضبط؟
```

**The subtlety that would have silently defeated it: `greet()` asks for the
service AND lists the options, so it is ask number one.** A counter that only
starts at `handle()` treats the first re-ask as a first ask and repeats the menu
verbatim - exactly the bug. `greet()` therefore sets `_ask_count["service"] = 1`,
and there is a mutation proving the tests catch its removal.

`_ask_count` is conversation state, so it is exported and validated like
everything else. Without that, a stateless serverless demo resets to phrasing [0]
on every turn and repeats itself forever - also mutation-tested. Answering a slot
resets its counter, so a later correction asks afresh rather than in the clipped
re-ask voice. Silence (an empty transcript) does NOT advance the counter; a
caller who said nothing has not been asked twice.

Escalation still terminates the loop: the phrasings vary, then after
`MAX_ATTEMPTS` the slot is handed to a human.

### Free-text slots now need to be plausible

`location` and `name` are the only two slots that cannot be pattern-matched, so
they used to accept whatever the caller said. A judge mumbling into a microphone
got `مممم` stored and read back as **`في مممم`** in the confirmation.

The bar is **plausibility, not a whitelist**. A list of UAE place names would
reject a real address, which is the worse failure, because the two errors are not
symmetric: a false ACCEPT is one line in a readback the caller is immediately
asked to confirm, while a false REJECT takes the caller's real name and cannot be
recovered by them repeating it - it will just be rejected again. So anything that
cannot be separated cleanly is ACCEPTED, and the rules are structural properties
of noise rather than judgements about meaning:

| Rejected | Why |
|---|---|
| `اه` `مم` `آه` | fewer than 3 Arabic letters |
| `xyz` `123` | mostly not Arabic letters |
| `مممم` `ااااا` `هههه` `بلابلابلا` | one unit repeated 3+ times |
| `يعني` `اممم` | hesitation word |
| `فيديو` at the location prompt | an option from the PREVIOUS question |

The floor sits at **three letters** because `علي` is a real name and `دبي` a real
emirate at exactly three. `مدينة خليفة`, `الخالدية`, `جميرا`, `مصفح`,
`شارع المطار`, `عبدالله بن زايد` and `منى` all pass, and are asserted to.

Requiring **three or more** repeats (not two) is what keeps genuinely reduplicated
Arabic words like `زلزل` out of the net.

A rejected value is neither stored nor silently dropped: the agent says
`عفواً، ما التقطت المكان.` and the turn **consumes an ask**, so the graded re-ask
still varies the phrasing and the escalation path still terminates rather than
looping "sorry, say again" forever. The test asserts the symptom the caller would
actually see - that a rejected value can never appear in the readback - not just
the internal slot state.

**Two things this exposed while being built:**

- `_location` had a blanket digit veto that pre-empted the new check, so `123`
  was refused with the wrong message and no recorded reason, and a legitimate
  `شارع 5` was refused outright. Removed: a phone number spoken at the location
  prompt is already claimed by `_phone` before this code runs. `name` keeps a
  digit veto, since a name has no digits, but now reports it like every other
  rejection.
- The readback test initially passed for the wrong reason on the `name` slot: it
  rejected the name four times and then supplied a real one, so of course the
  confirmation was clean. Mutation testing is what surfaced it.

Six mutations, all caught - including **raising the floor from 3 to 5**, which
fails 19 checks. That one matters most: it proves the suite catches
OVER-rejection, not only under-rejection.

### Orthographic variation is handled by folding, and now proven

`number_e2e.py` measured the live API returning real spelling variation
(`إلا ربع` for `إلا ربعاً`, `واربعين` without the hamza). Every keyword list here
is matched on `norm()`ed text, which folds أ/إ/آ->ا, ة->ه, ى->ي and strips
tashkeel, and the lists themselves are normalised at import so both sides agree.

That was claimed throughout the file and is now a test: مقابلة/مقابله,
الشارقة/الشارقه, أبوظبي/ابوظبي/أبو ظبي, رأس الخيمة/راس الخيمه, الأحد/الاحد,
الجمعة/الجمعه, غداً/غدا, صباحاً/صباحا, مساءً/مساءا, خطأ/خطا all resolve
identically. Deleting `_NORM_MAP.update(_FOLD)` fails 15 checks.

**A mutation-testing note worth keeping:** the first attempt at that mutation was
`_FOLD = {} or {...}`, which is a NO-OP - `{}` is falsy, so the dict was
unchanged and the mutation "survived". A mutation that does not actually mutate
proves nothing, and it reads exactly like a gap in the tests. Sanity-check what
the mutated code does before believing its verdict.

### Known weak or unbuilt

- **The Arabic is now decent, still not warm.** After the dialogue pass it
  returns greetings and echoes what it heard, which moves it from "form-filling
  robot" to "brisk receptionist". It still does not small-talk, sympathise, or
  vary sentence SHAPE - every question is the same length and register, and the
  four ack words are the only variation in the whole script. A native speaker
  would still place it as a machine, just several turns later and less jarringly.
- **The LLM clarification stacks two questions** ("ما هي الخدمات التي ترغب...؟
  أي نوع تصوير تحتاج؟"). Safe, redundant, slightly clumsy on a phone line.
- **Free-text plausibility is structural, so a plausible-looking wrong answer
  still gets through.** `الشارع` or a misheard real word passes every rule here,
  because nothing checks that a string denotes an actual place or person. The
  readback is the only thing standing between that and a wrong booking.
- **No availability check, no calendar, no persistence.** The agent will happily
  confirm a slot that is already booked; `done: true` writes nothing anywhere.
- **Dialect coverage is a keyword list**, tested against phrasing I wrote myself.
  Unmeasured against real Gulf callers, which is the same caveat §7 makes about
  the ASR accuracy.
- **No multi-booking, no cancel, no reschedule** - one booking per conversation.
- The escalation path (a slot given up after 3 tries) marks the field "to be
  confirmed later" and **relies on a human who does not exist yet**.

---

## 13. Answered since §7, all measured 2026-09-18

§7 listed six things as unverified. Four of them now have answers, and two of
those changed the architecture rather than merely confirming it.

### 13.1 Multi-turn: one socket carries a conversation

§7 item 2 said multi-turn behaviour was untested, and the answer mattered more
than it looked. Billing is per socket-second and the free tier caps **new**
connections at 5 per minute, so a socket-per-turn design would rate-limit
itself after five exchanges and would start each turn with no memory of the
audio before it.

`multiturn_probe.py`, live: two utterances separated by 1.5 s of real-time
silence, on one socket.

```
  [end_of_turn] order=0  هل يمكنكم تأجيل التصوير إلى الساعة تسعة صباحاً؟
  [end_of_turn] order=1  هل يمكنكم تأجيل التصوير إلى الساعة تسعة صباحاً؟
  [termination] {'audio_duration_seconds': 12, 'session_duration_seconds': 13}

  committed turns 2   turn_order [0, 1]   reconnects 0   errors none
```

**A conversation is one connection.** Note also what the `Termination` frame
carries: `session_duration_seconds`. §7 item 4 says no endpoint exposes usage,
and that is still true of the REST API, but the socket does report its own
billable duration on the way out. That is the only usage signal found so far.

### 13.2 A browser can hold the socket, and that decides the hosting

A browser cannot set an `Authorization` header on a WebSocket. That one
limitation is what makes the whole architecture question interesting, and it
has a clean answer:

```
GET https://streaming.assemblyai.com/v3/token?expires_in_seconds=60
    Authorization: <raw key>              # no Bearer, same as the socket
-> 200 {"token": "...", "expires_in_seconds": 60}

wss://streaming.assemblyai.com/v3/ws?...&token=<token>     # NO Authorization header
-> Begin, configuration.model = universal-3-5-pro
```

The parameter is `expires_in_seconds` exactly. `expires_in` returns **422**.

**Consequence: the demo needs no long-lived server.** Static files plus two
stateless functions, so the URL is up whether or not any machine here is awake,
and no machine of ours is exposed. The conversation state round-trips through
the client, which is why `load_state` has to treat its input as hostile.

The mint is public and cannot be made otherwise, because a secret shipped to a
browser is not a secret. TTL and rate limiting are speed bumps and are labelled
as such; the account spend cap is the only real bound.

### 13.3 `SpeechStarted` exists and is not in the docs

The live socket emits a `SpeechStarted` event when the caller begins talking.
It is not in the documentation and was found by handling unknown message types
gracefully rather than ignoring them.

It is a better origin for stage 1 than anything computable locally, because it
is the service's own opinion of when speech began rather than our energy gate's
guess, and the two can disagree on a quiet talker or a noisy line. `server.py`
prefers it and keeps the gate as a fallback, precisely because an undocumented
event may simply stop arriving.

### 13.4 The wire format is not one thing, and the byte arithmetic bites

The phone line is 8 kHz mu-law at **8 bytes per millisecond**. A browser
microphone is 16 kHz PCM16 at **32**. The chunk aggregator counts bytes, so
reusing the phone line's constant buffers 25 ms while believing it buffered
100, which is under the 50 ms floor from CONSTRAINT 1 and closes the socket
with 3007. Both the byte rate and the silence-pad byte are now parameters;
`t_wire_formats` guards it and the guard is mutation-checked.

---

## 14. Stage 3 is no longer a stub

§8 said stage 3 could not be measured because Fish Audio returns 402 and the
wallet reads zero. That is still true of Fish. It is no longer true of stage 3.

macOS ships an Arabic voice, `Majed` (`ar_001`). Measured, 3 runs per line:

| speech produced | render, median |
|---|---|
| 546 ms | 421 ms |
| 2,659 ms | 423 ms |
| 7,609 ms | 450 ms |
| 14,928 ms | 483 ms |

**The shape of that table is the finding, and it is not what I expected.** `say`
does not stream, so the obvious reading is that a long reply pays a long render
before a word is heard. It does not: 27x the speech length costs 62 ms, because
the cost is process startup, not synthesis. `tts_say.py` recorded the wrong
prediction in its own docstring rather than deleting it.

So the comparison against a streaming vendor reverses: Fish's measured 380 ms
to FIRST byte versus 420-480 ms for the COMPLETE utterance. The free offline
voice wins for any reply longer than about half a second of speech, and only
loses on the shortest possible acknowledgement.

Wired into `latency.py --real-answer`, the whole answer side becomes real:

```
  endpoint lag (caller stops -> ASR commits)    575 - 775 ms
  LLM + TTS    (commit -> answer exists)        474 - 586 ms
```

**That second number is the one this repo has carried as 3,878-16,917 ms and
called the entire remaining problem.** It is now about half a second, so end of
speech to a spoken Arabic answer is roughly 1.05 to 1.36 s. Not a faster model:
no model. The rules path answers in 0.018 ms median and the voice is flat.

One label correction, stated rather than buried: the field is
`first_tts_byte_at`, and what is stamped is COMPLETE audio because `say` does
not stream. Harder bar, so pessimistic rather than flattering, but a different
quantity and not comparable to a vendor's first-byte figure without saying so.

What this does NOT fix: voice quality. `Majed` is a system voice, intelligible
and not warm, and no measurement here defends it.

---

## 15. Parser and API together, which had never been tested

CONSTRAINT 2 was measured on **one** sentence. `arabic_numbers.py`'s 232 checks
are **authored** strings. Verified separately is not verified together, so
`number_e2e.py` synthesises twelve utterances with known ground truth, streams
each to the live socket, and asks whether the parser recovers what was spoken.

**12 of 12 values recovered. 7 of 12 transcripts verbatim.**

The gap is the useful part. What the service actually returned:

| asked | heard |
|---|---|
| `إلا ربعاً` | `إلا ربع` |
| `والربع` | `وربع` |
| `خمسمئة` | `خمسمائة` |
| `وأربعين` | `واربعين` (no hamza) |
| `الاستوديو` | `الستوديو` |

Real orthographic variation, and the value came out right through all of it. A
parser written against the single captured spelling would have looked perfect
on the fixture and failed here. The agent's dialect keyword lists were checked
against the same classes of variation and now have tests for it.

Caveat that must travel with the number: this is a **synthetic speaker on a
clean line**, plausibly easier for an ASR model than a human. It is a floor on
failure modes, not evidence of robustness. §7 item 1 is still open.

Method note: each case opens a NEW socket and the free tier caps new
connections at 5/minute, so the run paces itself. Without that, rate limiting
would surface as transcription failures, which is the worst kind of wrong
result because it looks like a finding.

---

## 16. Where §10 stands

1. ~~Top up Fish Audio so stage 3 becomes real~~ — **not needed.** §14 solved it for free.
2. ~~Build the Arabic number-word parser~~ — **done**, §11, and proven end to end in §15.
3. Move barge-in off partials in `call_agent` — **still not done.** The rule is documented and tested here; `call_agent/server.py` is untouched.
4. Multi-turn — **done**, §13.1. Mid-turn reconnect on the live service is still untested.
5. Accuracy on genuinely human Arabic — **still open, and now the single biggest gap.** Everything measured here has been synthetic or captured-synthetic audio.

---

## 17. Word error rate on genuinely human Arabic (`human_wer.py`, 2026-09-18)

§7 item 1 and §16 item 5 said the same thing in two places: **every accuracy
figure in this repo came from synthetic or captured-synthetic audio.**
`number_e2e.py` is the macOS `Majed` voice reading clean text, and
`fixtures/v3_session_arabic.jsonl` is one captured sentence from the same voice.
A TTS speaker on a clean line is plausibly EASIER for an ASR model than a human,
so 12 of 12 is a floor on failure modes and not evidence of robustness.

That gap is now measured rather than admitted. `human_wer.py` pulls real
recorded human Arabic with ground-truth transcripts from two public corpora via
the Hugging Face datasets-server rows API, transcodes to 16 kHz mono PCM16,
streams each clip to the same live `universal-3-5-pro` socket the agent uses, and
computes standard word error rate: Levenshtein over word sequences, (S+D+I)/N,
stdlib only, no `jiwer`.

### 17.1 Two corpora, because one number would have flattered us

| | `fleurs` | `casablanca` |
|---|---|---|
| dataset | `google/fleurs`, config `ar_eg`, split `test` | `UBC-NLP/Casablanca`, config `UAE`, split `test` |
| licence | CC BY 4.0 | CC BY-NC-ND 4.0 |
| speech | READ Modern Standard Arabic, Egyptian speakers, clean single-speaker recordings | SPONTANEOUS Emirati dialect off television, real conversation, music and effects under it |
| sample | 30 clips, 315.4 s, row offsets 0-30, 6.0-18.5 s each | 30 clips, 177.2 s, row offsets 0-36, 2.3-13.8 s each |

Mozilla Common Voice Arabic was the first candidate and is **not usable through
this path**, which is worth recording so nobody burns an hour on it again:
`mozilla-foundation/common_voice_17_0` and `_13_0` return `EmptyDatasetError`
from the rows API, `_11_0` returns "does not exist, or is not accessible" even
with a token because the terms have not been accepted on this account, and the
`fsicoli/*` mirrors return 501, "the dataset viewer doesn't support this dataset
because it runs arbitrary Python code". `halabi2016/arabic_speech_corpus` fails
the same way. Those are answers, not obstacles, and each was recorded rather
than fought.

### 17.2 The numbers, with the sample size attached to every one

**30 clips per corpus. That is indicative and it is not a settled question.**

| | `fleurs` (read MSA) | `casablanca` (Emirati dialect) |
|---|---|---|
| **WER raw** | **0.225** | **0.700** |
| | S 116, D 6, I 2, over 551 reference words | S 255, D 51, I 12, over 454 reference words |
| **WER normalised** | **0.092** | **0.588** |
| | S 44, D 6, I 1 | S 199, D 51, I 15 |
| against the corpus's own normalised reference | 0.092 | corpus ships one reference |
| per clip: best / median / worst | 0.000 / 0.069 / 0.346 | 0.281 / 0.619 / 1.167 |
| clips over 0.5 WER | 0 of 30 | 20 of 30 |
| clips exactly 0.000 after normalisation | 9 of 30 | 0 of 30 |
| empty hypotheses | 0 | 0 |
| stream errors, socket errors | 0 | 0 |

**Read Modern Standard Arabic transcribes well. Spontaneous Emirati dialect does
not.** 0.588 means roughly three words in five are wrong, and the split matters:
**51 deletions out of 451 reference words**, so more than a tenth of what was
said never appears in the transcript at all, and one clip scored 1.167, worse
than emitting nothing.

The two figures are never blended into one. A single averaged number would be
arithmetic with no referent.

### 17.3 The normalisation, spelled out, because a WER without it is meaningless

Applied to BOTH sides, in this order: NFC compose; strip tashkeel and Quranic
marks (U+0610-061A, U+064B-065F, U+0670, U+06D6-06ED); strip tatweel U+0640;
alef forms أ إ آ ٱ to ا; alef maqsura ى to ي; ta marbuta ة to ه; Arabic-Indic
digits to ASCII; drop every Unicode punctuation codepoint; collapse whitespace.

Deliberately NOT applied, and each omission is a decision: ؤ to و and ئ to ي,
because that heavier fold merges more genuinely different words; dropping the
definite article, because that hides a real agreement error; any stemming,
because a stem match is not a word match. The two folds that CAN merge real word
pairs are ة/ه and ى/ي, and they are named here rather than hidden.

The gap between raw and normalised is itself the finding, and an ablation says
where it comes from:

| cumulative normalisation | `fleurs` | `casablanca` |
|---|---|---|
| raw | 0.225 | 0.700 |
| + punctuation | 0.145 | 0.616 |
| + diacritics and tatweel | 0.094 | 0.616 |
| + alef forms and digits | 0.094 | 0.588 |
| + ya and ta-marbuta (full) | 0.092 | 0.588 |

On read MSA, **59 per cent of the apparent error was orthography**: punctuation
alone accounts for 8 points and the reference's diacritics for 5 more, because
FLEURS `raw_transcription` is partly vocalised and the service never returns
tashkeel. On dialect only 16 per cent of the error was orthographic, so the
remaining 0.588 is the model hearing different words, not spelling them
differently. **Normalisation cannot rescue the dialect number and it is not
being asked to.**

### 17.4 The Arabic that comes back is CLEAN, now checked on 60 transcripts

Across all 60 hypotheses, 962 transcript words:

```
  presentation-form codepoints   0
  bidi control characters        0
  U+FFFD replacement char        0
```

The claim that AssemblyAI returns correctly ordered logical Arabic had been
verified on exactly one sentence. It now holds on 60, from two corpora and two
dialects. Speaker gender is in both corpora and was NOT recorded in this run, so
nothing is claimed about it. The detector derives a positional form from the
codepoint NAME rather than a block range, because the presentation-forms blocks
are interleaved: the ornate parentheses U+FD3E and U+FD3F sit inside the range
and are not positional forms at all, so a range test reports corruption on
Islamic heritage text that is perfectly fine.

### 17.5 A correction to Finding 1, found by reading the output

Finding 1 says numbers come back as Arabic words, never digits. **That is
narrower than stated.** On human read speech the service emitted ASCII digits on
4 of 30 FLEURS clips: `مائة نقطة` came back as `100 نقطة`, `مائة في المئة` as
`100%`, and `6.5 درجة` stayed `6.5`.

So the true behaviour is **inconsistent**, which is worse than either pure case
and is exactly the shape of bug that reaches production: clock-style small
numbers arrive as words, large quantities and decimals arrive as digits, and
nothing announces which. `arabic_numbers.py` already parses both, checked
directly, so the agent survives this; a parser written against CONSTRAINT 2 as
worded would not have.

### 17.6 Read the output, because the metric cannot tell you these things

FLEURS, worst clip of the thirty, WER 0.346 normalised:

```
  ref: جرت التقاليد على أن وظيفة القوّات البحريّة هي ضمان قدرة بلدك على نقل شعبك وبضائعه
  hyp: قالت التقارير على أن وظيفة القوة البحرية هي ضمان كودريت بالك على نقل شعبه وبضائحه
```

`جرت التقاليد` ("tradition has it") became `قالت التقارير` ("the reports said"),
and `قدرة بلدك` became `كودريت بالك`, which is not Arabic at all: the model fell
through to a phonetic spelling. Both are real errors and no normalisation should
ever repair them.

FLEURS, median clip, WER 0.069:

```
  ref: لكن بسبب موقعها "القريب من المناطق الاستوائية" ببضع درجات فقط شمال الخط الاستوائي
  hyp: إيه. لكن بسبب موقعها القريب من المناطق الاستوائية ببضع درجات فقط شمال الخط الاستوائي
```

**The service prepended `إيه.`, a filler the speaker did not say.** That is an
insertion out of nothing, on the cleanest audio in the set, and for a booking
agent an invented leading token is not cosmetic: it is the token a barge-in or a
keyword match sees first.

Casablanca, median clip, WER 0.619, quoted as a short excerpt because the corpus
is no-derivatives:

```
  ref: عساكم من العايدين، و الفايزين، و كل سنة و كل حول، إن شاء الله، سامحونا تعبانين من البارحة، برقد، عن اذنكم
  hyp: أساكم العايدين والفائزين وكل سنة وكل حول إن شاء الله سامحونا تعباني من البرحة برضو أنا فيكم
```

The Eid greeting survives almost intact, then the colloquial tail collapses:
`برقد` ("I am going to sleep") became `برضو`, and `عن اذنكم` ("excuse me", the
speaker leaving) became `أنا فيكم`. **The polite formulas transcribe and the
dialectal verbs do not**, which is the pattern across the set and is precisely
the half a booking agent needs.

### 17.7 What this establishes and what it does not

Establishes:

- Read MSA from real humans transcribes at **0.092 WER normalised on 30 clips**, so the pipeline is not broken on human speech and the synthetic result was not carrying it.
- Spontaneous Emirati dialect transcribes at **0.588 WER normalised on 30 clips**, and an agent built on that transcript is working from three wrong words in five.
- The returned Arabic is clean and logically ordered on 60 transcripts, not one.
- The service inserts words that were never spoken, and formats numbers inconsistently.

Does not establish:

- **Neither corpus is what this agent is for.** FLEURS is read speech into a good microphone. Casablanca is broadcast television. **What the agent actually hears is spontaneous Gulf dialect on an 8 kHz phone line, and that has still never been measured.** The phone codec is a further loss on top of the dialect number, in an unknown direction and amount.
- 30 clips per corpus is indicative, not settled. A 30-clip result must be quoted as a 30-clip result.
- These figures are **not comparable to any published WER tier**, ours or a vendor's, because they were measured on different audio. A WER is a property of a test set and a normalisation, not of a model.
- Nothing here measures latency, dialect coverage beyond Emirati and Egyptian, overlapping speakers, or a hostile line.

### 17.8 Method, and the two traps in it

**Multi-turn joining.** A real clip is long enough to be committed as several
turns: 8 of 60 came back as 2 or 3. Taking the LAST committed turn, which is
enough for a one-sentence probe and is what `number_e2e.py` does, silently
discards most of the words and reports a beautiful deletion-heavy WER. The
hypothesis is every committed turn joined in `turn_order`.

**Connection pacing.** Each clip is a new socket and the free tier caps NEW
connections at 5 per minute. Tripping it surfaces as empty transcripts, which is
a WER of 1.0 that looks like a finding about Arabic and is really a finding about
our own pacing. `--gap` defaults to 13 s, audio is streamed at real time so the
endpointer sees pauses that actually happened, and the run reports 0 empty
hypotheses and 0 socket errors, so no figure above is rate limiting in disguise.

**Licence handling.** No audio is committed; `audio_cache/` is gitignored.
FLEURS is CC BY 4.0, so `fixtures/human_wer.json` carries its reference and
hypothesis text in full. Casablanca is CC BY-NC-ND 4.0, so the committed JSON
carries metrics plus excerpts capped at 110 characters, and the full text stays
in the local cache. Dataset id, config, split and every row offset are recorded,
so the sample is reproducible by anyone without trusting this file.

`python3 human_wer.py --cached` re-scores the finished run with no streaming and
no API key, which is what made the ablation table above affordable: changing a
normalisation rule must not cost 60 live sockets.

### 17.9 §16 item 5, updated

5. ~~Accuracy on genuinely human Arabic~~ - **measured**, this section. Read MSA 0.092, Emirati dialect 0.588, 30 clips each. The remaining gap is narrower and sharper: **spontaneous Gulf dialect over a real 8 kHz phone line**, which is the only audio this agent will ever actually hear.
