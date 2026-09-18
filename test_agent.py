"""Tests for agent.py. Exits 0 on pass, non-zero on failure.

    python3 test_agent.py            # all
    python3 test_agent.py -v         # show every assertion
    python3 test_agent.py --only happy

No pytest: it is not installed on this machine, and a suite that cannot run is
worse than no suite. Same convention as selftest.py.

The happy-path case PRINTS the whole Arabic dialogue. That is deliberate - the
only way to know whether a booking agent sounds like a person is to read what it
says, and an assertion that the string is non-empty proves nothing about that.

Tests that depend on `arabic_numbers` (times, spoken phone digits) report
SKIPPED, loudly, if that module is absent. They do not fake a pass.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
import traceback
import unicodedata

import agent
from agent import (ArabicCorruption, BookingAgent, Reply, make_ollama_llm,
                   verify_arabic)

HAVE_NUMBERS = agent.arabic_numbers is not None
TODAY = datetime.date(2026, 9, 18)          # a Friday. Pinned so dates are stable.

_results = []
_skipped = []
VERBOSE = False


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))
    if VERBOSE or not cond:
        print("    [%s] %s%s" % ("ok  " if cond else "FAIL", name,
                                 "  -- %s" % detail if detail else ""))
    return bool(cond)


def skip(name, why):
    _skipped.append((name, why))
    print("    [SKIPPED] %s  -- %s" % (name, why))


def eq(name, got, want):
    return check(name, got == want, "got %r want %r" % (got, want))


def new(llm=None):
    return BookingAgent(llm=llm, today=TODAY)


def say(ag, text, show=False):
    r = ag.handle(text)
    if show:
        print("    CALLER : %s" % text)
        print("    AGENT  : %s" % r.text)
    return r


# ---------------------------------------------------------------------------
# 1. Happy path, printed so a human can read the Arabic
# ---------------------------------------------------------------------------

def t_happy():
    if not HAVE_NUMBERS:
        skip("happy path", "arabic_numbers missing, time+phone slots unfillable")
        return
    ag = new()
    print()
    print("    ---- full booking, read it ----")
    g = ag.greet()
    print("    AGENT  : %s" % g.text)
    check("greet is a Reply", isinstance(g, Reply))
    check("greet not done", g.done is False)
    check("greet used no llm", g.used_llm is False)
    check("greet names the studio", "استوديو" in g.text)

    r = say(ag, "السلام عليكم، أبغى أحجز تصوير فيديو", show=True)
    # Not optional politeness in the Gulf. Failing to return the salaam was the
    # most machine-like thing this agent did.
    check("the salaam is RETURNED", r.text.startswith("وعليكم السلام"), r.text)
    check("and the greeting does not replace the turn's work",
          "نوع تصوير" in r.text or "يوم" in r.text, r.text)
    eq("service still captured on a greeting turn", r.slots["service"], "video")
    check("no acknowledgement word on top of the greeting",
          not any(a.rstrip(".") in r.text for a in ("طيب", "ممتاز", "زين")),
          r.text)
    check("asks for the date next", "يوم" in r.text)

    r = say(ag, "بكرة الساعة تسعة صباحاً", show=True)
    eq("date is tomorrow", r.slots["date"]["iso"], "2026-09-19")
    eq("hour is 9", r.slots["time"]["hour"], 9)
    check("period was explicit so no am/pm question",
          "صباحاً أو مساءً" not in r.text, r.text)
    # Two slots in one breath: echo them before asking the next question, so a
    # misheard time is catchable NOW rather than four questions later.
    check("a two-slot turn is echoed back", "بكرة" in r.text
          and "التاسعة" in r.text, r.text)
    check("the echo comes before the next question",
          r.text.index("التاسعة") < r.text.index("وين"), r.text)
    check("asks where", "وين" in r.text, r.text)

    r = say(ag, "في دبي، منطقة الخليج التجاري", show=True)
    check("location keeps the area the caller gave",
          "دبي" in r.slots["location"] and "الخليج" in r.slots["location"],
          r.slots["location"])
    check("area keeps ta-marbuta, not the folded matching form",
          "منطقة" in r.slots["location"], r.slots["location"])
    check("location separator is speakable, not a dash",
          "-" not in r.slots["location"], r.slots["location"])

    r = say(ag, "اسمي أحمد المنصوري", show=True)
    eq("name captured without the lead-in", r.slots["name"], "أحمد المنصوري")

    r = say(ag, "رقمي صفر خمسة صفر واحد اثنين ثلاثة أربعة خمسة ستة سبعة",
            show=True)
    eq("phone assembled from Arabic number WORDS", r.slots["phone"],
       "0501234567")
    check("a number read aloud did not corrupt the date",
          r.slots["date"]["iso"] == "2026-09-19", r.slots["date"])
    check("readback reads the whole booking", "خليني أأكد الحجز" in r.text)
    for must in ("تصوير فيديو", "بكرة", "التاسعة", "دبي", "أحمد", "0501234567"):
        check("readback contains %s" % must, must in r.text)
    check("readback asks a yes/no question", r.text.endswith("؟"))
    check("not done before confirmation", r.done is False)

    r = say(ag, "نعم", show=True)
    check("done after yes", r.done is True)
    check("closing line thanks the caller", "شكراً" in r.text)
    check("no llm was used anywhere on the happy path", r.used_llm is False)
    print("    ---- end ----")
    print()


# ---------------------------------------------------------------------------
# 2. Out-of-order: the caller volunteers several slots in one sentence
# ---------------------------------------------------------------------------

def t_out_of_order():
    ag = new()
    ag.greet()
    r = say(ag, "أبغى تصوير منتجات بكرة في الشارقة")
    eq("service from a multi-slot sentence", r.slots["service"], "products")
    eq("date from the same sentence", r.slots["date"]["iso"], "2026-09-19")
    eq("location from the same sentence", r.slots["location"], "الشارقة")
    check("three slots in one turn", r.debug["extracted"].get("service")
          and r.debug["extracted"].get("date")
          and r.debug["extracted"].get("location"))
    if HAVE_NUMBERS:
        check("next question skips what it already has, asks the time",
              "ساعة" in r.text, r.text)
    else:
        skip("out-of-order next question", "arabic_numbers missing")

    # and the reverse order: time before service
    ag2 = new()
    ag2.greet()
    if HAVE_NUMBERS:
        r = say(ag2, "الساعة أربعة العصر تصوير مقابلة")
        eq("service found after the time", r.slots["service"], "interview")
        eq("afternoon maps to 16:00", r.slots["time"]["hour"], 16)
    else:
        skip("time-before-service", "arabic_numbers missing")


# ---------------------------------------------------------------------------
# 3. Correction mid-flow: "لا، الساعة عشرة مو تسعة"
# ---------------------------------------------------------------------------

def t_correction_midflow():
    if not HAVE_NUMBERS:
        skip("mid-flow correction", "arabic_numbers missing, time unfillable")
        return
    ag = new()
    ag.greet()
    say(ag, "تصوير فوتوغرافي بكرة الساعة تسعة صباحاً")
    eq("starts at 9", ag.slots["time"]["hour"], 9)

    r = say(ag, "لا، الساعة عشرة مو تسعة", show=True)
    # The correction is on the LEFT of the contrast particle. Taking the right
    # side would re-apply the exact value the caller just rejected.
    eq("corrected to 10, not back to 9", ag.slots["time"]["hour"] % 12, 10)
    eq("other slots untouched by the correction", ag.slots["service"], "photo")
    eq("date untouched by the correction", ag.slots["date"]["iso"],
       "2026-09-19")
    check("a bare 'عشرة' is ambiguous so it asks am/pm",
          "صباحاً أو مساءً" in r.text, r.text)

    r = say(ag, "صباحاً", show=True)
    eq("period resolved to morning", ag.slots["time"]["hour"], 10)
    check("answering am/pm is not treated as off-script",
          r.debug["intent"] != "offscript", r.debug["intent"])

    # Isolates the contrast-particle split. The service table is scanned in a
    # fixed order with photo FIRST, so "فيديو مو فوتوغرافي" resolves to the
    # REJECTED service unless the text right of مو is discarded. The time case
    # above cannot prove this, because a time parser reading left to right
    # happens to land on the right answer either way.
    ag2 = new()
    ag2.greet()
    r = say(ag2, "أبغى فيديو مو فوتوغرافي", show=True)
    eq("correction keeps the value LEFT of مو, not the rejected one",
       r.slots["service"], "video")
    for particle in ("مب", "مش", "ليس"):
        a3 = new()
        a3.greet()
        eq("contrast particle %s behaves the same" % particle,
           a3.handle("أبغى فيديو %s فوتوغرافي" % particle).slots["service"],
           "video")


# ---------------------------------------------------------------------------
# 4. "No" at confirmation changes ONE field, does not wipe the booking
# ---------------------------------------------------------------------------

def t_no_at_confirmation():
    ag = new()
    ag.greet()
    say(ag, "تصوير فيديو")
    say(ag, "بكرة")
    if HAVE_NUMBERS:
        say(ag, "الساعة تسعة صباحاً")
    else:
        for _ in range(4):                  # let the time slot escalate
            say(ag, "مممم")
    say(ag, "دبي")
    say(ag, "اسمي خالد")
    r = say(ag, "0501112233")
    check("reached confirmation", "خليني أأكد الحجز" in r.text, r.text)
    before = json.loads(json.dumps(ag.slots, ensure_ascii=False))

    r = say(ag, "لا", show=True)
    eq("rejection routes to fix", r.debug["intent"], "rejected")
    check("asks WHICH field, does not restart",
          "أي معلومة" in r.text, r.text)
    check("nothing was wiped by the no",
          ag.slots["service"] == "video" and ag.slots["name"] == "خالد",
          ag.slots)

    r = say(ag, "المكان", show=True)
    eq("field named", r.debug.get("field"), "location")
    check("that field only is cleared", ag.slots["location"] is None)
    check("every other field survives",
          ag.slots["service"] == "video" and ag.slots["name"] == "خالد"
          and ag.slots["phone"] == "0501112233", ag.slots)
    check("asks for the cleared field", "وين" in r.text, r.text)

    r = say(ag, "أبوظبي", show=True)
    eq("new value applied", ag.slots["location"], "أبوظبي")
    check("back at confirmation", "خليني أأكد الحجز" in r.text, r.text)
    for k in ("service", "name", "phone"):
        eq("field %s identical after the fix" % k, ag.slots[k], before[k])

    r = say(ag, "تمام", show=True)
    check("'تمام' also counts as yes", r.done is True, r.text)


# ---------------------------------------------------------------------------
# 5. llm=None: off-script utterances still get a sensible Arabic reply
# ---------------------------------------------------------------------------

def t_no_llm_offscript():
    ag = new(llm=None)
    ag.greet()
    offscript = [
        "كم تكلفة التصوير عندكم؟",
        "هل عندكم موقف سيارات؟",
        "أنا أتصل من شركة كبيرة جداً وأريد التحدث مع المدير",
        "?????",
        "",
        "   ",
    ]
    for line in offscript:
        r = ag.handle(line)
        check("off-script %r never crashes and returns a Reply" % line[:20],
              isinstance(r, Reply))
        check("off-script %r replies in Arabic" % line[:20],
              any("ؠ" <= c <= "ي" for c in r.text), r.text)
        check("off-script %r used no llm" % line[:20], r.used_llm is False)
        check("off-script %r never claims done" % line[:20], r.done is False)
        check("off-script %r re-anchors on a question" % line[:20],
              "؟" in r.text, r.text)

    # It must not loop forever on a slot it cannot fill.
    ag2 = new(llm=None)
    ag2.greet()
    last = None
    for _ in range(8):
        last = ag2.handle("بلابلابلا")
    check("an unfillable slot escalates instead of looping",
          ag2.slots["service"] is None
          and "service" in last.debug.get("skipped", []),
          last.debug)
    check("escalation says a human will follow up",
          "بنرجع لك" in last.text or "بيتأكد لاحقاً" in last.text, last.text)

    # An LLM that fails must be indistinguishable from llm=None.
    def broken(_prompt):
        raise RuntimeError("model is down")

    ag3 = BookingAgent(llm=broken, today=TODAY)
    ag3.greet()
    r = ag3.handle("كم تكلفة التصوير عندكم؟")
    check("a raising llm degrades to the scripted clarification",
          isinstance(r, Reply) and r.used_llm is False, r.text)
    check("the llm error is recorded for the demo UI",
          "llm_error" in r.debug, r.debug)

    def times_out(_prompt):
        return None

    ag4 = BookingAgent(llm=times_out, today=TODAY)
    ag4.greet()
    r = ag4.handle("كم تكلفة التصوير عندكم؟")
    check("an llm returning None degrades cleanly", r.used_llm is False)

    # And an LLM that returns corrupted Arabic must be rejected, not spoken.
    shaped = "".join(chr(c) for c in (0xFEE3, 0xFEA4, 0xFEE4, 0xFEAF))

    def corrupt(_prompt):
        return shaped

    ag5 = BookingAgent(llm=corrupt, today=TODAY)
    ag5.greet()
    r = ag5.handle("كم تكلفة التصوير عندكم؟")
    check("corrupted llm output is discarded, not spoken",
          r.used_llm is False and shaped not in r.text, r.text)
    check("and the rejection is visible in debug",
          "rejected" in str(r.debug.get("llm", "")), r.debug.get("llm"))


def t_llm_role_is_fenced():
    """The LLM may ask, never assert. Both defects here are REAL captured output.

    Not hypotheticals: these are the first two replies qwen2.5:7b-instruct
    actually produced from the agent's own prompt on 2026-09-18.
    """
    from agent import llm_output_is_safe

    invented_price = "تبلغ تكلفة التصوير معنا 200 درهم إماراتي."
    went_chinese = ("ساعات عمل استوديو التصوير是从周一到周五上午9点至下午5点。"
                    "请注意，具体时间可能因studio而异。")

    ok, why = llm_output_is_safe(invented_price)
    check("REGRESSION: the invented price is refused", ok is False, why)
    check("...and the reason names the number", "digit" in why or "currency" in why,
          why)
    ok, why = llm_output_is_safe(went_chinese)
    check("REGRESSION: the Chinese reply is refused", ok is False, why)
    # That real capture happens to contain digits too ("9点至下午5点"), so it
    # trips the digit gate first. Prove the SCRIPT gate stands on its own with a
    # digit-free version, or this test would pass even with script purity removed.
    ok, why = llm_output_is_safe("هل تقصد التصوير؟请注意具体时间因工作室而异？")
    check("digit-free CJK is refused by the script gate alone",
          ok is False and "script" in why, why)

    # The Chinese one passes the corruption guard, which is exactly why script
    # purity has to be a SEPARATE gate rather than a second corruption check.
    clean = True
    try:
        verify_arabic(went_chinese)
    except ArabicCorruption:
        clean = False
    check("the Chinese reply passes verify_arabic - so the gates are not "
          "redundant", clean)

    # Shape rules, each stated as its own case.
    for bad, label in (
            ("استوديونا مفتوح طوال الأسبوع.", "a statement, not a question"),
            ("نعم، الموعد متاح.", "confirms availability it cannot know"),
            ("السعر 500 درهم؟", "a price even in question form"),
            ("الحجز الساعة 9؟", "quotes a time the rules should own"),
            ("Can you repeat that?", "English"),
            ("هل تقصد التصوير photography؟", "mixed Latin"),
            ("", "empty"),
            ("ممكن توضح؟\nوأيضاً؟", "multi-line"),
            ("ممكن توضح قصدك بالتفصيل " * 12 + "؟", "too long"),
            (None, "not a string"),
            (12345, "not a string"),
    ):
        ok, why = llm_output_is_safe(bad)
        check("refused (%s)" % label, ok is False, "%r -> %s" % (bad, why))

    # Isolates the DIGIT gate. An Arabic-Indic digit is inside the Arabic block,
    # so it sails past script purity and past the currency list - only the digit
    # rule catches it. Without this case, deleting that rule breaks nothing.
    ok, why = llm_output_is_safe("هل نحجز لك الساعة ٩؟")
    check("an Arabic-Indic digit is refused by the digit gate alone",
          ok is False and "digit" in why, why)

    for good in ("ممكن توضح قصدك؟",
                 "هل تقصد حجز جلسة تصوير؟",
                 "عفواً، ممكن تعيد السؤال؟",
                 "هل تحب أحولك لأحد الزملاء؟"):
        ok, why = llm_output_is_safe(good)
        check("allowed: %s" % good, ok is True, why)

    # End to end through the agent.
    def priced(_p):
        return invented_price

    def chinese(_p):
        return went_chinese

    for fn, label in ((priced, "price"), (chinese, "Chinese")):
        a2 = BookingAgent(llm=fn, today=TODAY)
        a2.greet()
        r = a2.handle("كم تكلفة التصوير عندكم؟")
        check("agent never speaks the %s reply" % label,
              r.used_llm is False, r.text)
        check("agent falls back to the scripted clarification (%s)" % label,
              "عفواً" in r.text and "؟" in r.text, r.text)
        check("and says why in debug (%s)" % label,
              str(r.debug.get("llm", "")).startswith("rejected"),
              r.debug.get("llm"))

    # A well-behaved model IS used.
    def polite(_p):
        return "ممكن توضح قصدك؟"

    a3 = BookingAgent(llm=polite, today=TODAY)
    a3.greet()
    r = a3.handle("كم تكلفة التصوير عندكم؟")
    check("a safe clarifying question is used", r.used_llm is True, r.text)
    check("and it is followed by the pending question",
          "ممكن توضح قصدك؟" in r.text and "نوع تصوير" in r.text, r.text)
    check("llm latency is reported for the demo", "llm_ms" in r.debug, r.debug)


# ---------------------------------------------------------------------------
# 6. The Arabic-correctness guard
# ---------------------------------------------------------------------------

def t_arabic_guard():
    # Build the corrupt string from codepoints and PROVE they are presentation
    # forms, rather than pasting glyphs and hoping.
    cps = (0xFEE3, 0xFEA4, 0xFEE4, 0xFEAF)
    names = [unicodedata.name(chr(c)) for c in cps]
    check("test fixture really is presentation forms",
          all(n.endswith(("ISOLATED FORM", "INITIAL FORM", "MEDIAL FORM",
                          "FINAL FORM")) for n in names), names[0])
    shaped = "".join(chr(c) for c in cps)

    raised = False
    try:
        verify_arabic(shaped)
    except ArabicCorruption as e:
        raised = "presentation form" in str(e)
    check("verify_arabic RAISES on a pre-shaped string", raised)

    for bad, why in ((u"‮مرحبا", "RLO override"),
                     (u"مرحبا‏", "RLM"),
                     (u"مرحبا�", "U+FFFD")):
        r = False
        try:
            verify_arabic(bad)
        except ArabicCorruption:
            r = True
        check("verify_arabic RAISES on %s" % why, r)

    for ok in ("مرحبا", "الساعة التاسعة صباحاً", "الحجز يوم 19 سبتمبر",
               "دبي - منطقة الخليج التجاري"):
        p = True
        try:
            verify_arabic(ok)
        except ArabicCorruption as e:
            p = False
            check("clean Arabic must pass: %s" % ok, False, str(e))
        if p:
            check("clean Arabic passes: %s" % ok[:24], True)

    # The false positive that matters. U+FD3E/U+FD3F sit INSIDE FB50-FEFF but
    # are ornate parentheses around a Quranic quotation, not presentation forms.
    # A range-based guard flags them and then accuses correct Islamic-heritage
    # text of being corrupt. Deriving the class from the Unicode NAME does not.
    quran = u"﴾إنا أعطيناك الكوثر﴿"
    ok = True
    try:
        verify_arabic(quran)
    except ArabicCorruption as e:
        ok = False
        check("ornate parens must NOT be flagged", False, str(e))
    if ok:
        check("ornate parens U+FD3E/U+FD3F are not flagged as corruption", True)

    # Now end to end: a corrupted slot must make the AGENT raise rather than
    # speak it. This is the thesis enforced in code.
    ag = new()
    ag.greet()
    say(ag, "تصوير فيديو")
    say(ag, "بكرة")
    ag._slots["time"] = {"hour": 9, "minute": 0, "explicit_period": True}
    ag._slots["location"] = "دبي"
    ag._slots["phone"] = "0501234567"
    ag._slots["name"] = shaped                 # poisoned, as if from a bad source
    raised = False
    try:
        ag.handle("اسمي")                      # forces a readback
    except ArabicCorruption:
        raised = True
    except Exception as e:                                       # noqa: BLE001
        check("agent raised the WRONG error", False, repr(e))
    check("the agent RAISES rather than speaking a corrupted readback", raised)


# ---------------------------------------------------------------------------
# 7. Timing on the rules path
# ---------------------------------------------------------------------------

def t_timing():
    lines = ["تصوير فيديو", "بكرة", "الساعة تسعة صباحاً", "في دبي",
             "اسمي أحمد المنصوري", "0501234567", "لا", "المكان", "الشارقة",
             "نعم"]
    samples = []
    for _ in range(40):
        ag = new()
        ag.greet()
        for line in lines:
            r = ag.handle(line)
            samples.append(r.ms)
    samples.sort()
    n = len(samples)
    med = samples[n // 2]
    p95 = samples[int(n * 0.95)]
    worst = samples[-1]
    print("    rules path over %d turns: median %.3f ms, p95 %.3f ms, "
          "max %.3f ms" % (n, med, p95, worst))
    check("median rules turn under 5 ms", med < 5.0, "%.3f ms" % med)
    check("p95 rules turn under 5 ms", p95 < 5.0, "%.3f ms" % p95)
    check("even the worst rules turn is under 20 ms", worst < 20.0,
          "%.3f ms" % worst)
    check("Reply.ms is populated and positive", all(s > 0 for s in samples))


# ---------------------------------------------------------------------------
# 8. Stateless round-trip (the serverless demo)
# ---------------------------------------------------------------------------

def t_state_roundtrip():
    convo = ["أبغى تصوير فيديو", "بكرة", "الساعة تسعة صباحاً", "في دبي",
             "اسمي أحمد المنصوري", "0501234567", "نعم"]

    # A: one agent runs the whole conversation.
    a = new()
    a.greet()
    a_replies = [a.handle(x) for x in convo]

    # B: a FRESH agent per turn, resumed only from exported JSON. This is exactly
    # what a stateless HTTP function does.
    b = new()
    b.greet()
    st = b.export_state()
    b_replies = []
    for x in convo:
        blob = json.dumps(st, ensure_ascii=False)       # must survive JSON
        fresh = BookingAgent()                          # no `today` injected
        fresh.load_state(json.loads(blob))
        r = fresh.handle(x)
        b_replies.append(r)
        st = fresh.export_state()

    for i, (ra, rb) in enumerate(zip(a_replies, b_replies)):
        eq("turn %d text identical across the state round-trip" % (i + 1),
           rb.text, ra.text)
        eq("turn %d slots identical across the state round-trip" % (i + 1),
           json.dumps(rb.slots, ensure_ascii=False, sort_keys=True),
           json.dumps(ra.slots, ensure_ascii=False, sort_keys=True))
        eq("turn %d done flag identical" % (i + 1), rb.done, ra.done)
    check("the stateless run also completed the booking",
          b_replies[-1].done is True)

    # `today` must travel, or a conversation started at 23:59 changes meaning.
    check("exported state carries the conversation's own 'today'",
          a.export_state()["today"] == TODAY.isoformat())
    check("export_state is JSON-serialisable",
          isinstance(json.dumps(a.export_state(), ensure_ascii=False), str))
    check("scratch tokens are not exported",
          not any(k in a.export_state() for k in ("_ntoks", "_dtoks")))


def t_state_hostile():
    """That dict arrives from a stranger's browser. It must never 500."""
    hostile = [
        {}, None, [], "nope", 42, 3.5, True,
        {"slots": "not a dict"},
        {"slots": {"service": "DROP TABLE"}},
        {"slots": {"service": ["video"]}},
        {"slots": {"date": {"iso": "not-a-date"}}},
        {"slots": {"date": {"iso": "2026-13-45"}}},
        {"slots": {"date": "tomorrow"}},
        {"slots": {"time": {"hour": 99}}},
        {"slots": {"time": {"hour": "9"}}},
        {"slots": {"time": {"hour": True}}},
        {"slots": {"time": {"hour": 9, "minute": -5}}},
        {"slots": {"name": "x" * 5000}},
        {"slots": {"phone": {"nested": "thing"}}},
        {"state": "admin"}, {"state": 7},
        {"asked": "__class__"}, {"asked": ["time"]},
        {"attempts": {"time": -9}}, {"attempts": {"time": 10 ** 9}},
        {"attempts": "no"},
        {"skipped": ["time", "__init__", 5]}, {"skipped": "time"},
        {"turn": -1}, {"turn": 10 ** 12}, {"turn": "many"},
        {"period_pending": "yes"},
        {"today": "2026-99-99"}, {"today": 20260918},
        {"unknown_key": {"deeply": ["nested", {"junk": 1}]}},
        {"v": 999, "slots": {"service": "video"}},
        # a "confirmed" state with nothing in it must not read an empty booking
        {"state": "confirm", "asked": "confirm", "slots": {}},
    ]
    for h in hostile:
        ag = BookingAgent()
        try:
            ag.load_state(h)
        except Exception as e:                                   # noqa: BLE001
            check("load_state survives %r" % (str(h)[:40],), False, repr(e))
            continue
        check("load_state survives %r" % (str(h)[:40],), True)
        try:
            r = ag.handle("تصوير فيديو")
            check("and the next turn still works after %r" % (str(h)[:30],),
                  isinstance(r, Reply) and bool(r.text))
        except Exception as e:                                   # noqa: BLE001
            check("next turn after %r" % (str(h)[:30],), False, repr(e))

    # Whatever comes in, what comes OUT must be a valid state name. Without this
    # a forged {"state": "admin"} is stored verbatim and merely happens to behave
    # like "collect" because nothing matches it.
    for forged in ("admin", "done ", 7, None, ["confirm"], "__init__"):
        ag = BookingAgent()
        ag.load_state({"state": forged})
        check("forged state %r is normalised to a real state" % (forged,),
              ag.export_state()["state"] in ("collect", "confirm", "fix", "done"),
              ag.export_state()["state"])

    ag = BookingAgent()
    ag.load_state({"slots": {"service": "DROP TABLE"}})
    check("an invalid enum value is dropped, not stored",
          ag.slots["service"] is None)
    ag = BookingAgent()
    ag.load_state({"state": "confirm", "asked": "confirm", "slots": {}})
    check("a forged 'confirm' with empty slots falls back to collecting",
          ag.export_state()["state"] == "collect",
          ag.export_state()["state"])
    ag = BookingAgent()
    ag.load_state({"slots": {"time": {"hour": 9, "minute": 30,
                                      "explicit_period": True}}})
    eq("a valid time survives the validator", ag.slots["time"]["hour"], 9)
    eq("and its minutes too", ag.slots["time"]["minute"], 30)


# ---------------------------------------------------------------------------
# 9. Barge-in safety: partials must be refused
# ---------------------------------------------------------------------------

def t_no_partials():
    class T:
        def __init__(self, transcript, end_of_turn):
            self.transcript = transcript
            self.end_of_turn = end_of_turn

    ag = new()
    ag.greet()
    raised = False
    try:
        ag.handle_turn(T("أبغى تصوير", False))
    except ValueError as e:
        raised = "end_of_turn" in str(e)
    check("handle_turn REFUSES a partial", raised)
    check("and the refused partial changed nothing",
          ag.slots["service"] is None)

    r = ag.handle_turn(T("أبغى تصوير فيديو", True))
    eq("handle_turn accepts a final", r.slots["service"], "video")
    eq("ACTS_ON is declared for the integration", BookingAgent.ACTS_ON,
       "end_of_turn")


# ---------------------------------------------------------------------------
# 10. Misc invariants
# ---------------------------------------------------------------------------

def t_invariants():
    ag = new()
    r = ag.greet()
    check("every reply is JSON round-trippable",
          json.loads(r.to_json())["text"] == r.text)
    ag.handle("تصوير فيديو")
    check("reset clears everything", True)
    ag.reset()
    check("slots empty after reset",
          all(ag.slots[k] is None for k in agent.SLOT_ORDER), ag.slots)
    check("state empty after reset",
          ag.export_state()["state"] == "collect")

    # Every scripted reply must pass the Arabic guard. Walk a lot of paths.
    ag = new()
    texts = [ag.greet().text]
    for line in ["تصوير منتجات", "يوم الأربعاء", "الساعة أربعة العصر",
                 "رأس الخيمة", "اسمي منى", "0509998877", "لا", "التاريخ",
                 "بعد بكرة", "نعم", "شكرا"]:
        texts.append(ag.handle(line).text)
    bad = []
    for t in texts:
        try:
            verify_arabic(t)
        except ArabicCorruption as e:
            bad.append((t, str(e)))
    check("every reply on a long path is clean logical-order Arabic",
          not bad, str(bad[:1]))
    check("no reply contains an em dash", not any("—" in t for t in texts))

    # Service vocabulary
    for phrase, want in (("تصوير فوتوغرافي", "photo"), ("فيديو", "video"),
                         ("مقابلة", "interview"), ("تصوير منتجات", "products"),
                         ("بودكاست", "interview"), ("ريلز", "video")):
        a2 = new()
        a2.greet()
        eq("service %r -> %s" % (phrase, want), a2.handle(phrase).slots["service"],
           want)

    # Relative dates
    for phrase, iso in (("اليوم", "2026-09-18"), ("بكرة", "2026-09-19"),
                        ("بعد بكرة", "2026-09-20"),
                        ("يوم الخميس", "2026-09-24")):
        a2 = new()
        a2.greet()
        a2.handle("تصوير فيديو")
        got = a2.handle(phrase).slots["date"]
        eq("date %r -> %s" % (phrase, iso), got and got["iso"], iso)

    # Emirates
    for phrase, want in (("دبي", "دبي"), ("أبو ظبي", "أبوظبي"),
                         ("العين", "العين"), ("الفجيرة", "الفجيرة")):
        a2 = new()
        a2.greet()
        a2.handle("تصوير فيديو")
        a2.handle("بكرة")
        a2._slots["time"] = {"hour": 9, "minute": 0, "explicit_period": True}
        eq("location %r -> %s" % (phrase, want),
           a2.handle(phrase).slots["location"], want)


# ---------------------------------------------------------------------------
# 11. Optional: one real Ollama call (opt-in, never part of the pass count)
# ---------------------------------------------------------------------------

def measure_ollama():
    llm = make_ollama_llm(timeout_s=30.0)
    t = time.perf_counter()
    out = llm("أجب بجملة عربية قصيرة واحدة: ما هي ساعات عمل استوديو التصوير؟")
    ms = (time.perf_counter() - t) * 1000.0
    if out is None:
        print("  ollama: NOT REACHABLE (no number to report)")
        return
    print("  ollama qwen2.5:7b-instruct: %.0f ms" % ms)
    print("  reply: %s" % out.replace("\n", " ")[:200])


def t_greetings():
    """Return the greeting, and do not let a greeting act as data."""
    for said, want in (("السلام عليكم", "وعليكم السلام"),
                       ("سلام عليكم", "وعليكم السلام"),
                       ("السلام عليكم ورحمة الله", "وعليكم السلام"),
                       ("السلام عليكم ورحمة الله وبركاته", "وعليكم السلام"),
                       ("مرحبا", "مرحبتين"),
                       ("مرحباً", "مرحبتين"),        # hamza/tanween variant
                       ("هلا", "مرحبتين"),
                       ("أهلين", "مرحبتين"),
                       ("اهلين", "مرحبتين"),
                       ("صباح الخير", "صباح النور"),
                       ("مساء الخير", "مساء النور")):
        ag = new()
        ag.greet()
        r = ag.handle(said)
        check("%r is answered with %r" % (said, want),
              r.text.startswith(want), r.text[:50])
        check("%r still leaves a question on the table" % said,
              "؟" in r.text, r.text)
        check("%r is not treated as off-script" % said,
              r.debug["intent"] != "offscript", r.debug["intent"])

    # Prepended to the turn's real work, not instead of it.
    ag = new()
    ag.greet()
    r = ag.handle("السلام عليكم، أبغى تصوير فيديو بكرة")
    check("greeting AND slots in one utterance",
          r.text.startswith("وعليكم السلام")
          and r.slots["service"] == "video"
          and r.slots["date"]["iso"] == "2026-09-19", r.text)

    # THE TRAP: "مساء الخير" contains مساء, which is also the evening marker.
    # Without stripping the greeting before extraction the agent books 6pm
    # because the caller said good evening.
    if HAVE_NUMBERS:
        ag = new()
        ag.greet()
        ag.handle("تصوير فيديو")
        ag.handle("بكرة")
        r = ag.handle("مساء الخير، الساعة تسعة")
        check("'مساء الخير' is a greeting, NOT a pm marker",
              r.slots["time"]["hour"] == 9
              and r.slots["time"]["explicit_period"] is False, r.slots["time"])
        check("so it still asks am/pm", "صباحاً أو مساءً" in r.text, r.text)
        # and a real مساءً still works
        r = ag.handle("مساءً")
        eq("a genuine مساءً still resolves to pm", r.slots["time"]["hour"], 21)
    else:
        skip("good-evening trap", "arabic_numbers missing")

    # A greeting is not a failed answer, so it must not spend an attempt.
    ag = new()
    ag.greet()
    for _ in range(6):
        r = ag.handle("السلام عليكم")
    check("repeated greetings never escalate a slot",
          not r.debug.get("skipped") and not r.debug.get("attempts"),
          r.debug)
    check("and the agent keeps asking the same real question",
          "نوع تصوير" in r.text, r.text)

    # greet() is the first thing a judge hears. It must survive all of this.
    g = new().greet()
    check("greet() still opens with أهلاً وسهلاً",
          g.text.startswith("أهلاً وسهلاً، معك"), g.text)
    check("greet() still names the studio and asks the first question",
          "استوديو" in g.text and g.text.rstrip().endswith("؟"), g.text)
    check("greet() is clean Arabic", verify_arabic(g.text) == g.text)


def t_acknowledgement_is_not_a_metronome():
    ACKS = ("تمام", "طيب", "ممتاز", "زين")

    def ack_of(text):
        for a in ACKS:
            if text.startswith(a):
                return a
        return None

    # 1. No new information -> no acknowledgement.
    ag = new()
    ag.greet()
    r = ag.handle("بلابلابلا")
    check("no ack when the caller supplied nothing", ack_of(r.text) is None,
          r.text[:40])

    # 2. New information -> an acknowledgement.
    ag = new()
    ag.greet()
    r = ag.handle("تصوير فيديو")
    check("an ack when the caller supplied something",
          ack_of(r.text) is not None, r.text[:40])

    # 3. It must not be a fixed rotation keyed on the turn counter. Identical
    #    content at DIFFERENT turn numbers must give the SAME word; that is what
    #    proves it is derived from content and not from a counter.
    a1 = new()
    a1.greet()
    first = ack_of(a1.handle("تصوير فيديو").text)
    a2 = new()
    a2.greet()
    a2.handle("بلابلابلا")
    a2.handle("بلابلابلا")
    later = ack_of(a2.handle("تصوير فيديو").text)
    eq("same content gives the same ack regardless of turn number", later,
       first)

    # 4. Different content gives different words across a real conversation, so
    #    it is varied and not just constant.
    ag = new()
    ag.greet()
    seen = []
    for line in ("تصوير فيديو", "بكرة", "في دبي", "اسمي خالد"):
        a = ack_of(ag.handle(line).text)
        if a:
            seen.append(a)
    check("the acks vary across a conversation", len(set(seen)) > 1, seen)

    # 5. Deterministic across agents: crc32, not the salted built-in hash().
    #    (Cross-PROCESS stability is what the state round-trip depends on; it is
    #    asserted by construction because crc32 has no per-process seed.)
    a3 = new()
    a3.greet()
    eq("ack is stable across separate agents",
       ack_of(a3.handle("تصوير فيديو").text), first)


def t_orthographic_variants():
    """The ASR really does vary hamza and taa-marbuta - `number_e2e.py` measured
    it returning `إلا ربع` for `إلا ربعاً` and `واربعين` without the hamza.

    Matching is done on `norm()`ed text, which folds أ/إ/آ->ا, ة->ه, ى->ي and
    strips tashkeel. That is claimed all over this file; this test PROVES it for
    every keyword list the agent owns, rather than trusting the claim.
    """
    groups = [
        ("service", ("مقابلة", "مقابله"), "interview"),
        ("service", ("تصوير فوتوغرافي", "تصوير فوتوغرافى"), "photo"),
        ("service", ("منتجات", "المنتجات"), "products"),
    ]
    for _kind, spellings, want in groups:
        got = []
        for sp in spellings:
            ag = new()
            ag.greet()
            got.append(ag.handle(sp).slots["service"])
        check("service spellings %s all give %s" % (list(spellings), want),
              all(g == want for g in got), got)

    for spellings, want in ((("الشارقة", "الشارقه"), "الشارقة"),
                            (("أبوظبي", "ابوظبي", "أبو ظبي", "ابو ظبي"),
                             "أبوظبي"),
                            (("رأس الخيمة", "راس الخيمه"), "رأس الخيمة"),
                            (("الفجيرة", "الفجيره"), "الفجيرة"),
                            (("أم القيوين", "ام القيوين"), "أم القيوين")):
        got = []
        for sp in spellings:
            ag = new()
            ag.greet()
            ag.handle("تصوير فيديو")
            ag.handle("بكرة")
            ag._slots["time"] = {"hour": 9, "minute": 0,
                                 "explicit_period": True}
            ag._period_pending = False
            got.append(ag.handle(sp).slots["location"])
        check("emirate spellings %s all give %s" % (list(spellings), want),
              all(g == want for g in got), got)

    for spellings, iso in ((("الأحد", "الاحد"), "2026-09-20"),
                           (("الإثنين", "الاثنين"), "2026-09-21"),
                           (("الجمعة", "الجمعه"), "2026-09-25"),
                           (("غداً", "غدا", "بكرة", "بكره"), "2026-09-19")):
        got = []
        for sp in spellings:
            ag = new()
            ag.greet()
            ag.handle("تصوير فيديو")
            got.append(ag.handle(sp).slots["date"])
        check("date spellings %s all give %s" % (list(spellings), iso),
              all(g and g["iso"] == iso for g in got),
              [g and g["iso"] for g in got])

    for spellings, want in ((("نعم", "أيوه", "ايوه", "تمام", "صح", "زين"), True),
                            (("لا", "لأ", "غلط", "خطأ", "خطا"), False)):
        for sp in spellings:
            ag = new()
            check("polarity %r reads as %s" % (sp, want),
                  ag._polarity(agent.norm(sp)) is want)

    # Period markers, with and without tanween.
    if HAVE_NUMBERS:
        for spellings, hour in ((("صباحاً", "صباحا", "الصبح"), 9),
                                (("مساءً", "مساءا", "بالليل"), 21)):
            got = []
            for sp in spellings:
                ag = new()
                ag.greet()
                ag.handle("تصوير فيديو")
                ag.handle("بكرة")
                ag.handle("الساعة تسعة")
                got.append(ag.handle(sp).slots["time"]["hour"])
            check("period spellings %s all give %d" % (list(spellings), hour),
                  all(g == hour for g in got), got)
    else:
        skip("period spellings", "arabic_numbers missing")


TESTS = {
    "happy": t_happy,
    "greetings": t_greetings,
    "acks": t_acknowledgement_is_not_a_metronome,
    "spelling": t_orthographic_variants,
    "outoforder": t_out_of_order,
    "correction": t_correction_midflow,
    "noatconfirm": t_no_at_confirmation,
    "nollm": t_no_llm_offscript,
    "llmfence": t_llm_role_is_fenced,
    "guard": t_arabic_guard,
    "timing": t_timing,
    "state": t_state_roundtrip,
    "hostile": t_state_hostile,
    "partials": t_no_partials,
    "invariants": t_invariants,
}


def main(only):
    if only and only not in TESTS:
        print("no such test %r. have: %s" % (only, ", ".join(TESTS)))
        return 2
    names = [only] if only else list(TESTS)
    if not HAVE_NUMBERS:
        print("  !! arabic_numbers NOT IMPORTABLE - number-dependent cases will "
              "report SKIPPED, not pass")
    t0 = time.monotonic()
    crashed = []
    for n in names:
        print("  %s ..." % n)
        try:
            TESTS[n]()
        except Exception:                                        # noqa: BLE001
            crashed.append(n)
            print("    [CRASH] %s" % n)
            traceback.print_exc()

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = [r for r in _results if not r[1]]
    print()
    if _skipped:
        print("  %d SKIPPED:" % len(_skipped))
        for name, why in _skipped:
            print("    - %s (%s)" % (name, why))
    if failed:
        print("  %d FAILED:" % len(failed))
        for name, _, detail in failed:
            print("    - %s%s" % (name, "  (%s)" % detail if detail else ""))
    if crashed:
        print("  %d test function(s) crashed: %s" % (len(crashed),
                                                     ", ".join(crashed)))
    ok = not failed and not crashed
    print("  %d/%d passed in %.1fs" % (passed, len(_results),
                                       time.monotonic() - t0))
    print("\n  PASS" if ok else "\n  FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--only", default=None, help="one of: %s" % ", ".join(TESTS))
    ap.add_argument("--ollama", action="store_true",
                    help="make ONE real Ollama call and report its latency")
    a = ap.parse_args()
    VERBOSE = a.verbose
    if a.ollama:
        measure_ollama()
    sys.exit(main(a.only))
