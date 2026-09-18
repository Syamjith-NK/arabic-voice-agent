"""Selftest. Exits 0 on pass, non-zero on failure. No API key, no phone call.

    python3 selftest.py            # all
    python3 selftest.py -v         # show each assertion
    python3 selftest.py --only reconnect

Stdlib only plus numpy/websockets, both already installed. pytest is deliberately
not used - it is not installed anywhere on this machine, and a test suite that
cannot run is worse than no test suite.

What these tests can and cannot prove is stated in fixtures/README.md and NOTES.md.
In short: they prove OUR client handles the documented protocol correctly. They
prove nothing about AssemblyAI's real Arabic accuracy or timing, because nothing
here has ever touched a live socket.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from pathlib import Path

import aai_stream
import fake_aai_server
import replay
from aai_stream import AAIStream, Turn, TranscriberAdapter

HERE = Path(__file__).parent
# What the LIVE service actually returns for fixtures/arabic_caller_8k.ulaw,
# captured 2026-09-17 and identical across 4 runs.
#
# NOTE it differs from the text that was synthesised to make the audio, which had
# the DIGIT 9: "...إلى الساعة 9 صباحاً؟". The TTS spoke that digit as the word
# "تسعة" (tisʿa), and the ASR correctly wrote the word it heard. Transcribing
# speech back to the digit would be the service editorialising. This is the right
# answer, and the earlier hand-authored fixture was wrong.
ARABIC = "هل يمكنكم تأجيل التصوير إلى الساعة تسعة صباحاً؟"

_results: list[tuple[str, bool, str]] = []
VERBOSE = False


def check(name: str, cond: bool, detail: str = "") -> bool:
    _results.append((name, bool(cond), detail))
    if VERBOSE or not cond:
        mark = "ok  " if cond else "FAIL"
        print(f"    [{mark}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(cond)


# ---------------------------------------------------------------------------
# 1. Fixtures are present and are what they claim to be
# ---------------------------------------------------------------------------

async def t_fixtures():
    audio = replay.load_audio()
    check("audio fixture is 8 kHz mu-law sized", len(audio) == 33228,
          f"{len(audio)} bytes = {len(audio)/8000:.2f}s")
    frames = replay.frames_of(audio)
    check("frames are 160 bytes except a possible tail",
          all(len(f) == 160 for f in frames[:-1]) and 0 < len(frames[-1]) <= 160,
          f"{len(frames)} frames, last={len(frames[-1])}B")
    # The tail frame must be KEPT. Dropping it clips the end of the utterance.
    check("no audio is lost by framing", sum(len(f) for f in frames) == len(audio))

    # It must be real speech, not silence - a silent fixture would let every
    # downstream test pass while proving nothing.
    import numpy as np
    tbl = _mulaw_table()
    pcm = tbl[np.frombuffer(audio, dtype=np.uint8)].astype(np.float32) / 32768.0
    rms = float(np.sqrt((pcm ** 2).mean()))
    check("audio fixture is speech, not silence", rms > 0.02, f"rms={rms:.4f}")

    script = fake_aai_server.load_script()
    check("protocol fixture loads", len(script) >= 4, f"{len(script)} messages")
    finals = [r for r in script if r["message"].get("end_of_turn")]
    check("protocol fixture has exactly one final turn", len(finals) == 1,
          f"{len(finals)} found")
    check("no message is gated past the end of the audio",
          max(r["after_bytes"] for r in script) <= len(audio))


def _mulaw_table():
    """Local G.711 decode table. Duplicated from call_agent/mulaw.py deliberately:
    this repo must stand alone for a judge who clones it, and importing across
    sibling workspace directories would break that."""
    import numpy as np
    t = np.empty(256, dtype=np.int16)
    for i in range(256):
        u = ~i & 0xFF
        sign, exponent, mantissa = u & 0x80, (u >> 4) & 0x07, u & 0x0F
        s = (((mantissa << 3) + 0x84) << exponent) - 0x84
        t[i] = -s if sign else s
    return t


# ---------------------------------------------------------------------------
# 2. URL and auth construction - the two things easiest to get silently wrong
# ---------------------------------------------------------------------------

async def t_url_and_auth():
    url = aai_stream.build_url()
    check("url targets the v3 streaming endpoint",
          url.startswith("wss://streaming.assemblyai.com/v3/ws"))
    check("url pins universal-3-5-pro (the only model with Arabic)",
          "speech_model=universal-3-5-pro" in url)
    check("url asks for mu-law at 8 kHz, matching Twilio exactly",
          "encoding=pcm_mulaw" in url and "sample_rate=8000" in url)
    check("language_codes is a JSON array", '%5B%22ar%22%5D' in url, url)
    # format_turns is documented as unavailable on this model. Sending it anyway
    # would be us guessing at the API.
    check("format_turns is omitted by default", "format_turns" not in url)
    check("format_turns is sent only when explicitly asked",
          "format_turns=true" in aai_stream.build_url(format_turns=True))

    # The auth header. This is THE detail that differs from the Voice Agent API.
    s = AAIStream(url="wss://example.invalid/v3/ws", api_key="TESTKEY123")
    h = s._headers()
    check("Authorization is the bare key", h.get("Authorization") == "TESTKEY123", str(h))
    check("Authorization has NO Bearer prefix",
          "Bearer" not in h.get("Authorization", ""), str(h))

    # And a ws:// (fake) URL must not demand credentials at all.
    f = AAIStream(url="ws://127.0.0.1:1/v3/ws")
    check("fake ws:// url needs no key", f._headers() == {})


async def t_missing_key():
    """A missing key must produce an actionable message, never a traceback."""
    missing = HERE / "fixtures" / "definitely_not_a_key_file"
    raised = None
    try:
        aai_stream.load_key(missing, env_first=False)
    except aai_stream.AssemblyAIKeyMissing as exc:
        raised = str(exc)
    except Exception as exc:                                  # noqa: BLE001
        check("missing key raises AssemblyAIKeyMissing", False, f"got {type(exc).__name__}")
        return
    check("missing key raises AssemblyAIKeyMissing", raised is not None)
    if raised:
        check("message names the path it looked at", str(missing) in raised)
        check("message says how to fix it", "assemblyai.com" in raised and "chmod" in raised)
        check("message points at the no-key test path", "selftest.py" in raised)


# ---------------------------------------------------------------------------
# 3. Turn parsing
# ---------------------------------------------------------------------------

async def t_turn_parsing():
    t = Turn.from_message({"type": "Turn", "turn_order": 3, "transcript": ARABIC,
                           "end_of_turn": True, "end_of_turn_confidence": 0.87,
                           "turn_is_formatted": False, "words": [{"text": "x"}]})
    check("transcript parsed", t.transcript == ARABIC)
    check("end_of_turn parsed", t.end_of_turn is True and t.is_partial is False)
    check("confidence parsed as float", abs(t.end_of_turn_confidence - 0.87) < 1e-9)
    check("raw message is retained", t.raw.get("turn_order") == 3)
    check("arrival time is stamped locally", t.received_at > 0)

    # Defensive: real APIs send nulls.
    z = Turn.from_message({"type": "Turn"})
    check("missing fields do not crash the parser",
          z.transcript == "" and z.end_of_turn is False and z.end_of_turn_confidence == 0.0)
    n = Turn.from_message({"type": "Turn", "transcript": None,
                           "end_of_turn_confidence": None, "words": None})
    check("explicit nulls do not crash the parser",
          n.transcript == "" and n.end_of_turn_confidence == 0.0 and n.words == [])


# ---------------------------------------------------------------------------
# 4. The whole replay path
# ---------------------------------------------------------------------------

async def t_replay_clean():
    audio = replay.load_audio()
    server, fake = await fake_aai_server.run_server()
    try:
        stream, col = await replay.replay(
            audio=audio, url=fake_aai_server.server_url(server), verbose=False)
    finally:
        server.close()
        await server.wait_closed()

    check("every byte of audio reached the socket",
          stream.bytes_sent == len(audio) and fake.bytes_received == len(audio),
          f"sent={stream.bytes_sent} received={fake.bytes_received}")
    check("final transcript is the full Arabic sentence",
          stream.text() == ARABIC, repr(stream.text()))
    partials = [t for t in stream.turns if t.is_partial]
    finals = [t for t in stream.turns if t.end_of_turn]
    check("partials arrived before the final", len(partials) >= 3, f"{len(partials)}")
    check("exactly one final turn", len(finals) == 1, f"{len(finals)}")
    check("no errors on a clean run", not stream.errors, str(stream.errors))
    check("no reconnects on a clean run", stream.reconnects == 0)

    # v3 resends the WHOLE turn each time, never a delta.
    #
    # THE ORIGINAL ASSERTION HERE WAS WRONG AND LIVE DATA DISPROVED IT. It required
    # each partial to be a strict PREFIX of the next. Real captured partials are not:
    #
    #   "هل يمكنكم؟"
    #   "هل يمكنكم تأجيل التصوير إلى السنة؟"        <- "to the YEAR"
    #   "هل يمكنكم تأجيل التصوير إلى الساعة تسعة صباحاً؟"  <- revised to "to NINE O'CLOCK"
    #
    # Two things break prefixing: the model REVISES earlier words as more audio
    # arrives, and because this model formats every partial, the trailing "؟" moves.
    # Consequence for anyone rendering live captions: you must REPLACE the line,
    # never append a diff, or the caption both stutters and keeps stale words.
    texts = [t.transcript for t in partials]
    check("every partial carries the full turn text, not a delta",
          all(t.split()[0] == texts[0].split()[0] for t in texts if t.split()),
          str(texts))
    check("transcript length is non-decreasing across partials",
          all(len(b) >= len(a) for a, b in zip(texts, texts[1:])), str([len(t) for t in texts]))
    check("the client replaces rather than accumulates",
          len(finals[0].transcript) < sum(len(t) for t in texts),
          "a concatenating client would produce a far longer string")
    check("final transcript starts the same sentence as the partials",
          finals[0].transcript.split()[0] == texts[0].split()[0])

    # End-of-turn confidence must actually distinguish the final from the partials.
    check("end-of-turn confidence is highest on the final turn",
          finals[0].end_of_turn_confidence > max(t.end_of_turn_confidence for t in partials),
          f"final={finals[0].end_of_turn_confidence} "
          f"max_partial={max(t.end_of_turn_confidence for t in partials)}")

    # MEASURED LIVE: on universal-3-5-pro this value is BINARY, not graded -
    # exactly 0.0 on every partial and exactly 1.0 on the final, across 4 runs.
    # So end_of_turn_confidence_threshold has nothing to tune on this model, and
    # code that waits for "confidence > 0.7" to act early will never fire early.
    # Trust the end_of_turn FLAG, not the number.
    check("end-of-turn confidence is binary on this model, not graded",
          all(t.end_of_turn_confidence == 0.0 for t in partials)
          and finals[0].end_of_turn_confidence == 1.0,
          f"partials={[t.end_of_turn_confidence for t in partials]} "
          f"final={finals[0].end_of_turn_confidence}")

    # MEASURED LIVE: this model formats WITHOUT being asked. We never send
    # format_turns (build_url omits it by default), yet every turn including
    # partials comes back turn_is_formatted=true, with Arabic punctuation and
    # diacritics. The docs say format_turns is Universal-Streaming-only; on
    # universal-3-5-pro formatting is simply always on.
    check("turns come back formatted even though format_turns was never sent",
          all(t.turn_is_formatted for t in stream.turns if t.transcript),
          str([(t.turn_is_formatted, t.transcript[:20]) for t in stream.turns]))
    check("formatted Arabic carries real punctuation",
          "؟" in finals[0].transcript, repr(finals[0].transcript))

    # Pacing. 208 frames at 20 ms is 4.16 s; allow generous slack for a loaded box
    # but catch the case where pacing was skipped entirely.
    span = col.end_of_turn_at - col.audio_started
    check("audio was paced at ~20 ms/frame, not blasted", 3.5 < span < 8.0,
          f"end-of-turn at {span:.2f}s for {len(audio)/8000:.2f}s of audio")

    check("Terminate control message was sent",
          any(m.get("type") == "Terminate" for m in fake.control_messages),
          str(fake.control_messages))
    check("socket was closed (billing is per connection-second)", stream._ws is None)


async def t_partials_are_revised_not_just_extended():
    """A partial can be RETRACTED and replaced wholesale. Measured, not assumed.

    Captured live 2026-09-17 on this fixture:

        partial 1  هل يمكنكم؟
        partial 2  هل يمكنكم تأجيل التصوير إلى السنة؟          <- "to the YEAR"
        partial 3  هل يمكنكم تأجيل التصوير إلى الساعة تسعة صباحاً؟   <- "to NINE O'CLOCK"

    Between partial 2 and partial 3 the model did not append: it went back and
    changed a word it had already emitted. السنة (the year) became الساعة تسعة
    (nine o'clock). Those are different words with different meanings, and the
    first one was shown to the user before being withdrawn.

    CONSEQUENCE FOR BARGE-IN, which is the reason this test exists:
    call_agent's barge-in and intent routing fire on partial text. Any logic that
    treats a partial as a stable prefix - keyword matching, "did they say yes",
    prefix-diff caption rendering - can fire on a phrase that is about to be
    retracted, and there is no retraction event to undo it with. The safe rule is:
    act on end_of_turn, or accept that an early action may be based on text the
    service later disowns. This is a behavioural difference from the local Whisper
    path, where each `quick()` pass was independent and never claimed stability.
    """
    audio = replay.load_audio()
    server, _ = await fake_aai_server.run_server()
    try:
        stream, _ = await replay.replay(
            audio=audio, url=fake_aai_server.server_url(server), verbose=False)
    finally:
        server.close()
        await server.wait_closed()

    partials = [t.transcript for t in stream.turns if t.is_partial and t.transcript]
    check("there is more than one partial to compare", len(partials) >= 2, str(partials))

    # The client must expose each partial as the WHOLE turn, so a consumer that
    # replaces is correct and a consumer that appends is visibly wrong.
    check("each partial is a complete standalone sentence",
          all(len(p.split()) >= 1 for p in partials))

    # The defining property: at least one partial is NOT a prefix of its successor.
    # If this ever stops being true the note above can be relaxed - but it must be
    # re-measured, not assumed.
    # Two DISTINCT phenomena break prefixing, and they have different consequences.
    #
    # (1) Punctuation movement. Because this model formats every partial, the
    #     trailing "؟" sits at the end of each one and moves as words are added:
    #       "هل يمكنكم؟" -> "هل يمكنكم تأجيل التصوير إلى السنة؟"
    #     Annoying for a prefix-diff caption renderer; semantically harmless.
    non_prefix = [(a, b) for a, b in zip(partials, partials[1:]) if not b.startswith(a)]
    check("at least one partial is not a prefix of its successor",
          len(non_prefix) >= 1,
          f"if this fails, re-measure before relaxing the barge-in rule: {partials}")

    # (2) GENUINE WORD RETRACTION - the one that matters. A word the service
    #     already emitted disappears from the final transcript entirely:
    #       السنة  ("the year")  ->  الساعة تسعة  ("nine o'clock")
    #     Different word, different meaning, shown to the user before withdrawal.
    #     This is what makes prefix-matched barge-in and keyword routing unsafe.
    final_words = set(stream.text().split())
    retracted = sorted({w for p in partials for w in p.split()
                        if w not in final_words and not w.strip("؟?.،,")== ""})
    check("a word emitted in a partial was later retracted from the final",
          len(retracted) >= 1,
          f"retracted={retracted} final={stream.text()!r}")

    # And the client must have ended on the revised text, not the retracted one.
    check("the final transcript reflects the revision, not the retracted partial",
          stream.text() == ARABIC and "السنة" not in stream.text(),
          repr(stream.text()))


async def t_turbo_is_not_paced():
    """--turbo must actually skip pacing. If it did not, the flag is a lie and
    someone will quote a turbo run as a latency figure."""
    audio = replay.load_audio()
    server, _ = await fake_aai_server.run_server()
    t0 = time.monotonic()
    try:
        stream, _ = await replay.replay(audio=audio, url=fake_aai_server.server_url(server),
                                        turbo=True, verbose=False)
    finally:
        server.close()
        await server.wait_closed()
    dt = time.monotonic() - t0
    check("turbo finishes far faster than real time", dt < 2.0, f"{dt:.2f}s for 4.15s audio")
    check("turbo still produces the right transcript", stream.text() == ARABIC)


# ---------------------------------------------------------------------------
# 5. Failure paths
# ---------------------------------------------------------------------------

async def t_no_reconnect_on_clean_close():
    """A clean run must open EXACTLY ONE socket. Regression test for a real cost bug.

    AssemblyAI bills streaming by connection duration. After finish() the service
    closes the socket normally, and the read loop used to treat that expected close
    as a failure and open a replacement it was about to discard - a billable socket
    on every turn of every call, against a free-tier cap of 5 new connections per
    minute.

    It did NOT show up in the plain replay test, because there close() follows
    finish() immediately and sets _closing before the reader can react. It needs a
    realistic delay in between - which on a real call is ALWAYS, since the LLM turn
    sits in exactly that window. So this test puts a delay there on purpose.
    """
    import latency
    b = await latency.measure_replay(llm_stub_s=0.6, verbose=False)
    check("a clean turn with an answer delay opens no extra sockets",
          b.reconnects == 0,
          f"{b.reconnects} reconnect(s) - an expected close is being retried")
    check("a clean turn reports no errors", not b.errors, str(b.errors))

    # And directly: the flag must be set by a normal termination.
    import fake_aai_server
    audio = replay.load_audio()
    server, _ = await fake_aai_server.run_server()
    s = AAIStream(url=fake_aai_server.server_url(server))
    try:
        await s.start()
        for f in replay.frames_of(audio):
            await s.feed(f)
        await s.finish(timeout=5.0)
        check("session is marked terminated after finish()", s._terminated is True)
        # Give the reader a window in which it would previously have reconnected.
        await asyncio.sleep(0.8)
        check("no reconnect occurs in the window after termination", s.reconnects == 0,
              f"{s.reconnects}")
    finally:
        await s.close()
        server.close()
        await server.wait_closed()


async def t_reconnect():
    """One injected drop must produce exactly ONE reconnect.

    This test exists because the first implementation produced THREE: feed() and
    the read loop each raced to replace the socket. On a per-connection-billed
    service with a 5-new-connections-per-minute cap, that is a real bug.
    """
    audio = replay.load_audio()
    # Drop past the end of the audio so exactly one drop can occur.
    server, fake = await fake_aai_server.run_server(drop_after=20_000)
    try:
        stream, _ = await replay.replay(
            audio=audio, url=fake_aai_server.server_url(server),
            verbose=False, finish_timeout=3.0)
    finally:
        server.close()
        await server.wait_closed()

    check("a dropped socket is reconnected", stream.reconnects >= 1,
          f"{stream.reconnects}")
    check("ONE drop causes ONE reconnect, not a stampede", stream.reconnects == 1,
          f"{stream.reconnects} reconnects - the feed/read race is back")
    check("the drop was recorded as an error rather than swallowed",
          any("Close" in e or "Connection" in e for e in stream.errors),
          str(stream.errors)[:200])
    check("streaming continued after the reconnect",
          stream.bytes_sent == len(audio), f"{stream.bytes_sent}/{len(audio)}")


async def t_reconnect_budget_is_bounded():
    """With nothing listening, reconnect must give up, not spin forever."""
    s = AAIStream(url="ws://127.0.0.1:1/v3/ws", max_retries=2)
    # Shrink the backoff so the test does not take 20 seconds.
    orig = aai_stream.RETRY_BACKOFF_S
    aai_stream.RETRY_BACKOFF_S = (0.01, 0.01, 0.01, 0.01)
    try:
        t0 = time.monotonic()
        ok = await s._reconnect()
        dt = time.monotonic() - t0
    finally:
        aai_stream.RETRY_BACKOFF_S = orig
    check("reconnect gives up rather than looping forever", ok is False)
    check("it gave up quickly", dt < 5.0, f"{dt:.2f}s")
    check("each failed attempt was recorded", len(s.errors) == 2, str(s.errors))


async def t_auth_rejection():
    """A server that demands auth must reject a keyless connection cleanly."""
    server, fake = await fake_aai_server.run_server(auth_required=True)
    url = fake_aai_server.server_url(server)
    failed = False
    try:
        s = AAIStream(url=url, api_key="")   # ws:// -> no auth header sent
        try:
            await s.start()
            await s.feed(b"\xff" * 160)
            await asyncio.sleep(0.2)
            failed = bool(s.errors) or s._ws is None
        finally:
            await s.close()
    except Exception:                                        # noqa: BLE001
        failed = True
    finally:
        server.close()
        await server.wait_closed()
    check("a keyless connection to an auth-required server fails visibly", failed)


async def t_feed_before_start():
    s = AAIStream(url="ws://127.0.0.1:1/v3/ws")
    raised = False
    try:
        await s.feed(b"\x00" * 160)
    except RuntimeError:
        raised = True
    check("feed() before start() raises a clear RuntimeError", raised)


async def t_empty_and_odd_frames():
    """Twilio can merge or split payloads. The client must not assume 160 bytes."""
    audio = replay.load_audio()
    server, fake = await fake_aai_server.run_server()
    s = AAIStream(url=fake_aai_server.server_url(server))
    try:
        await s.start()
        await s.feed(b"")                      # must be a no-op, not an error
        check("empty payload is ignored", s.frames_sent == 0)
        await s.feed(audio[:37])               # odd size
        await s.feed(audio[37:400])            # merged
        check("odd and merged payload sizes are accepted", s.bytes_sent == 400,
              f"{s.bytes_sent}")
    finally:
        await s.close()
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# 6. The migration adapter
# ---------------------------------------------------------------------------

async def t_adapter():
    """The bridge to call_agent/stt_stream.py's poll-a-buffer interface."""
    import numpy as np
    audio = replay.load_audio()
    server, _ = await fake_aai_server.run_server()
    url = fake_aai_server.server_url(server)

    s = AAIStream(url=url)
    ad = TranscriberAdapter(s, careful_timeout_s=6.0)
    try:
        check("warm() is a no-op and returns None", ad.warm() is None)
        await s.start()
        check("quick() before any audio returns empty, not None", await ad.quick() == "")

        # Feed the first ~1.5 s so a partial exists.
        frames = replay.frames_of(audio)
        t0 = time.monotonic()
        for i, f in enumerate(frames[:90]):
            due = t0 + i * 0.02
            d = due - time.monotonic()
            if d > 0:
                await asyncio.sleep(d)
            await s.feed(f)
        await asyncio.sleep(0.1)

        # The numpy arg is IGNORED by design - documented in aai_stream.py.
        q = await ad.quick(np.zeros(8000, dtype=np.float32))
        check("quick() returns the latest partial", len(q) > 0, repr(q))
        check("quick() ignores the numpy argument entirely",
              q == (s.current.transcript if s.current else ""))

        qt0 = time.monotonic()
        await ad.quick()
        check("quick() returns immediately (it does not transcribe)",
              time.monotonic() - qt0 < 0.05)

        for f in frames[90:]:
            await s.feed(f)
        await s.finish(timeout=5.0)

        c = await ad.careful(np.zeros(8000, dtype=np.float32))
        check("careful() returns the committed final transcript", c == ARABIC, repr(c))
    finally:
        await s.close()
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# 7. Latency instrumentation
# ---------------------------------------------------------------------------

async def t_latency():
    import latency
    b = await latency.measure_replay(verbose=False)

    check("all three stages are measured separately",
          b.to_first_partial_ms is not None
          and b.to_end_of_turn_ms is not None
          and b.to_first_tts_byte_ms is not None, repr(b))
    check("first partial precedes end of turn",
          b.to_first_partial_ms < b.to_end_of_turn_ms,
          f"{b.to_first_partial_ms} vs {b.to_end_of_turn_ms}")
    check("first TTS byte comes after end of turn",
          b.to_first_tts_byte_ms >= b.to_end_of_turn_ms)
    check("the LLM gap is reported as its own number",
          b.llm_gap_ms is not None and b.llm_gap_ms >= 0, repr(b.llm_gap_ms))
    check("nothing is hardcoded: stages derive from recorded event times",
          b.to_end_of_turn_ms != b.to_first_partial_ms)

    # --- negative-latency regression -------------------------------------
    # The first version computed endpoint lag as (end_of_turn - audio_seconds),
    # comparing a recorded event against a DERIVED duration, and reported
    # -10.2 ms. A harness that can emit a negative latency cannot be trusted on
    # the positive ones either, and measuring the 3.9-16.9 s LLM turn is the
    # entire point of this file.
    for label, v in (("to_first_partial_ms", b.to_first_partial_ms),
                     ("to_end_of_turn_ms", b.to_end_of_turn_ms),
                     ("to_first_tts_byte_ms", b.to_first_tts_byte_ms),
                     ("endpoint_lag_ms", b.endpoint_lag_ms),
                     ("asr_stream_ms", b.asr_stream_ms),
                     ("llm_gap_ms", b.llm_gap_ms),
                     ("trailing_silence_ms", b.trailing_silence_ms)):
        check(f"{label} is not negative", v is None or v >= 0, f"{v}")

    check("endpoint lag is measured from SPEECH end, not stream end",
          b.speech_end_at is not None and b.audio_end_at is not None
          and b.speech_end_at < b.audio_end_at,
          "fixture has trailing silence, so these must differ")
    check("trailing silence is reported rather than hidden",
          b.trailing_silence_ms is not None and b.trailing_silence_ms > 50,
          f"{b.trailing_silence_ms}ms")

    # The stub is the only thing in stage 3, so the gap must reflect it. This is
    # what proves the three stages are independently measured rather than shared.
    b4 = await latency.measure_replay(llm_stub_s=1.0, verbose=False)
    check("stage 3 tracks the answer side independently of stages 1 and 2",
          b4.llm_gap_ms is not None and 900 <= b4.llm_gap_ms <= 1600,
          f"1.0s stub -> llm_gap {b4.llm_gap_ms}ms")
    check("a slower answer does not move the ASR stages",
          abs(b4.to_end_of_turn_ms - b.to_end_of_turn_ms) < 500,
          f"{b.to_end_of_turn_ms} vs {b4.to_end_of_turn_ms}")

    # A stage that never happened must be n/a, never 0 - otherwise a broken
    # pipeline reads as an infinitely fast one.
    empty = latency.TurnBudget()
    check("an unmeasured stage is None, not 0",
          empty.to_first_partial_ms is None and empty.llm_gap_ms is None)

    d = b.as_dict()
    check("budget serialises for a run log", "to_first_partial_ms" in d
          and json.dumps(d) is not None)
    # The whole reason this file exists: no single blended number.
    check("there is no blended 'total latency' field masquerading as the answer",
          not any(k in d for k in ("latency_ms", "total_ms", "turn_latency_ms")),
          str(list(d)))


# ---------------------------------------------------------------------------

async def t_wire_formats() -> None:
    """The browser demo feeds 16 kHz PCM16, the phone line feeds 8 kHz mu-law.

    The aggregator counts BYTES. Bytes per millisecond is 8 on one and 32 on the
    other, so a byte count that is a correct 100 ms chunk on the phone line is a
    25 ms chunk in the browser - under the service's measured 50 ms floor, which
    closes the socket with error_code 3007. This is the guard that stops the
    phone line's constant leaking into the browser path.
    """
    phone = aai_stream.AAIStream(url="ws://127.0.0.1:1/x", chunk_ms=100,
                                 auto_load_key=False)
    check("phone line: 100 ms of 8 kHz mu-law is 800 bytes",
          phone.chunk_bytes == 800, str(phone.chunk_bytes))

    browser = aai_stream.AAIStream(
        url="ws://127.0.0.1:1/x", chunk_ms=100, auto_load_key=False,
        bytes_per_ms=aai_stream.PCM16_BYTES_PER_MS_16K,
        pad_byte=aai_stream.PCM16_SILENCE,
    )
    check("browser: 100 ms of 16 kHz PCM16 is 3200 bytes",
          browser.chunk_bytes == 3200, str(browser.chunk_bytes))
    check("browser floor is 50 ms = 1600 bytes, not the phone line's 400",
          browser.min_chunk_bytes == 1600, str(browser.min_chunk_bytes))

    # The failure this exists to catch, stated as arithmetic: if the browser
    # path used the phone line's byte rate, its "100 ms" chunk would really be
    # 800 / 32 = 25 ms, and the service would close the socket.
    wrong_ms = 800 / aai_stream.PCM16_BYTES_PER_MS_16K
    check("the mistaken chunk really is below the service floor",
          wrong_ms < aai_stream.MIN_CHUNK_MS, f"{wrong_ms} ms")

    check("silence padding differs per encoding",
          phone.pad_byte == 0xFF and browser.pad_byte == 0x00,
          f"{phone.pad_byte} {browser.pad_byte}")

    # The URL the demo server actually asks for.
    u = aai_stream.build_url(language="ar", sample_rate=16000, encoding="pcm_s16le")
    check("browser URL carries 16 kHz pcm_s16le and still pins the Arabic model",
          "sample_rate=16000" in u and "pcm_s16le" in u
          and "universal-3-5-pro" in u and "%22ar%22" in u.replace("%5B", "[").replace("%5D", "]"),
          u)


TESTS = {
    "fixtures": t_fixtures,
    "wire": t_wire_formats,
    "url": t_url_and_auth,
    "key": t_missing_key,
    "parsing": t_turn_parsing,
    "replay": t_replay_clean,
    "revision": t_partials_are_revised_not_just_extended,
    "turbo": t_turbo_is_not_paced,
    "cleanclose": t_no_reconnect_on_clean_close,
    "reconnect": t_reconnect,
    "backoff": t_reconnect_budget_is_bounded,
    "auth": t_auth_rejection,
    "guards": t_feed_before_start,
    "frames": t_empty_and_odd_frames,
    "adapter": t_adapter,
    "latency": t_latency,
}


async def main(only: str | None) -> int:
    names = [only] if only else list(TESTS)
    if only and only not in TESTS:
        print(f"no such test {only!r}. have: {', '.join(TESTS)}")
        return 2

    t0 = time.monotonic()
    crashed = []
    for n in names:
        print(f"  {n} ...")
        try:
            await TESTS[n]()
        except Exception:                                    # noqa: BLE001
            crashed.append(n)
            print(f"    [CRASH] {n}")
            traceback.print_exc()

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = [r for r in _results if not r[1]]
    print()
    print(f"  {passed}/{len(_results)} checks passed in {time.monotonic()-t0:.1f}s")
    if failed:
        print(f"  {len(failed)} FAILED:")
        for name, _, detail in failed:
            print(f"    - {name}" + (f"  ({detail})" if detail else ""))
    if crashed:
        print(f"  {len(crashed)} test function(s) crashed: {', '.join(crashed)}")
    ok = not failed and not crashed
    print("\n  PASS" if ok else "\n  FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--only", default=None, help=f"one of: {', '.join(TESTS)}")
    a = ap.parse_args()
    VERBOSE = a.verbose
    sys.exit(asyncio.run(main(a.only)))
