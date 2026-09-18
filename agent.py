"""Deterministic Arabic booking agent for a UAE photo/video production studio.

    from agent import BookingAgent
    a = BookingAgent()
    print(a.greet().text)
    print(a.handle("أبغى تصوير فيديو بكرة الساعة تسعة صباحاً").text)

WHY THIS IS RULES-FIRST AND NOT AN LLM LOOP
-------------------------------------------------------------------------------
NOTES.md measured the turn budget on live calls. The ASR side is fine: endpoint
lag ~490 ms. The LLM side is not: 3,878-16,917 ms per turn. On a phone line that
is the difference between a conversation and a hold queue.

So the normal turn here does not call a model at all. Intent and slots come out
of pattern matching over the Arabic final transcript, which costs microseconds.
An LLM is called ONLY when the rules extracted nothing and the caller has gone
off-script, and even then it is hard-timeout-bounded and degrades to a scripted
clarification rather than hanging the call. `llm=None` is a fully working agent,
not a stub.

THREE MEASURED CONSTRAINTS FROM NOTES.md THAT SHAPE THIS FILE
-------------------------------------------------------------------------------
1.  PARTIALS ARE REVISED, NOT EXTENDED. `إلى السنة` ("to the year") became
    `إلى الساعة تسعة` ("to nine o'clock") between two partials, with no
    retraction event. Anything that acts on partial text can act on words the
    caller never said, and the action cannot be taken back. Therefore:

        THIS AGENT MUST ONLY EVER BE FED `end_of_turn` FINALS.

    `handle()` takes a string and cannot police that on its own, so
    `BookingAgent.handle_turn(turn)` is provided and REFUSES a partial, and
    `BookingAgent.ACTS_ON == "end_of_turn"` is readable by the integration.
    See the "barge-in safety" section at the bottom of this docstring.

2.  NUMBERS ARRIVE AS ARABIC WORDS, NEVER DIGITS. "nine" comes back as `تسعة`.
    A `\\d{1,2}` time regex finds nothing on a real transcript. All number work
    is delegated to the sibling module `arabic_numbers`; this file deliberately
    does NOT contain a second number parser. If that module is absent, the
    number-dependent slots (time, spoken phone digits) degrade to "ask again"
    and the agent still completes every other slot.

3.  ARABIC MUST LEAVE THIS FILE IN LOGICAL ORDER, UNSHAPED. Every reply is run
    through `verify_arabic()` before it is returned. If a reply carries Unicode
    presentation forms (the signature of a pre-shaped, reversed string) or bidi
    control characters, the agent RAISES instead of speaking it. That is the
    repo's whole thesis, enforced in code rather than asserted in a README.

BARGE-IN SAFETY - WHAT THE CALLER OF THIS MODULE MUST DO
-------------------------------------------------------------------------------
*   Route ONLY finals here: `if turn.end_of_turn: agent.handle(turn.transcript)`,
    or just call `agent.handle_turn(turn)` and let it refuse partials for you.
*   Partials are for the caption UI and for stopping playback (energy-based
    barge-in). They must never reach intent routing or slot extraction.
*   `end_of_turn_confidence` is binary on this model (exactly 0.0 or 1.0), so
    there is no "act early on a confident partial" path to take. Do not build one.
*   Every `handle()` call is a pure function of (state, final text). There is no
    background work and no timer, so an interrupted turn leaves no half-applied
    state to unwind.

Stdlib only. Python 3.9 compatible.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field

try:                                        # sibling module, another worker owns it
    import arabic_numbers                   # noqa: F401
except ImportError:                         # pragma: no cover - degradation path
    arabic_numbers = None

__all__ = [
    "Reply", "BookingAgent", "ArabicCorruption", "verify_arabic",
    "make_ollama_llm", "SLOT_ORDER",
]


# ===========================================================================
# 1. The Arabic correctness guard
# ===========================================================================

class ArabicCorruption(ValueError):
    """Raised when a reply we generated is not clean logical-order Arabic."""


def _presentation_forms():
    """Codepoints that are Arabic PRESENTATION FORMS, derived from the Unicode name.

    NOT a hardcoded range. U+FB50-U+FEFF is interleaved: U+FD3E/U+FD3F are the
    ORNATE PARENTHESES that enclose a Quranic quotation, and U+FD40-U+FD4F are
    honorifics. Those are legitimate characters in normal Islamic-heritage text.
    A range-based check flags them and then loudly accuses correct text of being
    corrupt - which is exactly the false positive that made an earlier corpus
    audit in this workspace report 35.6% corruption on clean books.

    Deriving the class from `unicodedata.name()` means new Unicode self-classifies
    and the ornate parentheses stay legal.
    """
    suffixes = ("ISOLATED FORM", "INITIAL FORM", "MEDIAL FORM", "FINAL FORM")
    out = set()
    for cp in range(0xFB50, 0xFF00):
        try:
            nm = unicodedata.name(chr(cp))
        except ValueError:
            continue
        if nm.endswith(suffixes):
            out.add(cp)
    return frozenset(out)


PRESENTATION_FORMS = _presentation_forms()

# Explicit bidi controls. If any of these are needed to make a string display
# correctly, the string is in the wrong order underneath.
BIDI_CONTROLS = frozenset(
    [0x061C, 0x200E, 0x200F, 0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF]
    + list(range(0x202A, 0x202F))
)
_ARABIC_LETTER = re.compile(r"[ؠ-يٱ-ۓ]")


def verify_arabic(text):
    """Return `text`, or raise ArabicCorruption. Cheap enough to run every turn.

    Three failure modes, each a real thing that has shipped in this workspace:
      - presentation forms  -> the string was shaped (and usually reversed) before
                               storage, so a reader sees `سرافلا دمحم`
      - bidi controls       -> visual order propped up by invisible characters
      - U+FFFD              -> an encoding round trip ate a byte
    """
    if not isinstance(text, str):
        raise ArabicCorruption("reply is not a str: %r" % (type(text),))
    if not text.strip():
        raise ArabicCorruption("reply is empty")
    for i, ch in enumerate(text):
        o = ord(ch)
        if o in PRESENTATION_FORMS:
            raise ArabicCorruption(
                "presentation form U+%04X (%s) at index %d - this text was shaped "
                "before storage and is not logical order: %r"
                % (o, unicodedata.name(ch, "?"), i, text[:60])
            )
        if o in BIDI_CONTROLS:
            raise ArabicCorruption(
                "bidi control U+%04X at index %d - order is being faked with "
                "invisible characters: %r" % (o, i, text[:60])
            )
        if o == 0xFFFD:
            raise ArabicCorruption("U+FFFD replacement char at index %d" % i)
    if not _ARABIC_LETTER.search(text):
        raise ArabicCorruption("reply contains no Arabic letters: %r" % text[:60])
    return text


# ===========================================================================
# 2. Normalisation (for MATCHING only - never for anything we speak)
# ===========================================================================

_STRIP = {}
for _cp in list(range(0x064B, 0x0653)) + [0x0640, 0x0670, 0x0653, 0x0654, 0x0655]:
    _STRIP[_cp] = None
_FOLD = {
    ord("أ"): "ا", ord("إ"): "ا", ord("آ"): "ا", ord("ٱ"): "ا",
    ord("ى"): "ي", ord("ة"): "ه", ord("ؤ"): "و", ord("ئ"): "ي",
}
_NORM_MAP = dict(_STRIP)
_NORM_MAP.update(_FOLD)
_PUNCT = re.compile(r"[،؛؟!.,:\-_/\\\"'()\[\]]+")
_WS = re.compile(r"\s+")
_DOUBLE_COMMA = re.compile(r"،(?:\s*[،,])+")

# Arabic-Indic and extended Arabic-Indic digits -> ASCII. Ten codepoints, used
# only when `arabic_numbers` is absent. This is a character map, not a parser;
# the actual number WORD parsing is never reimplemented here.
_DIGIT_MAP = {}
for _i in range(10):
    _DIGIT_MAP[0x0660 + _i] = str(_i)
    _DIGIT_MAP[0x06F0 + _i] = str(_i)


def norm(text):
    """Fold Arabic for MATCHING: drop tashkeel/tatweel, unify alef/ya/ta-marbuta."""
    if not text:
        return ""
    t = text.translate(_NORM_MAP)
    t = _PUNCT.sub(" ", t)
    return _WS.sub(" ", t).strip()


# Display form: same tokenisation as norm(), but WITHOUT the folding. Free-text
# slots (a name, an area) are captured from the caller's own words and then
# SPOKEN BACK, so they must not come out of the matching pipeline - a booking
# confirmed for "منطقه" instead of "منطقة" is the agent misspelling the caller.
_DISP_MAP = dict.fromkeys(list(range(0x064B, 0x0653)) + [0x0640])


def disp(text):
    if not text:
        return ""
    t = text.translate(_DISP_MAP)
    t = _PUNCT.sub(" ", t)
    return _WS.sub(" ", t).strip()


def _digits(text):
    if arabic_numbers is not None and hasattr(arabic_numbers, "normalize_digits"):
        try:
            return arabic_numbers.normalize_digits(text)
        except Exception:                                    # noqa: BLE001
            pass
    return text.translate(_DIGIT_MAP)


def _toks(ntext):
    return ntext.split()


def _kw(*words):
    """Normalise a keyword list once, at import, so matching is apples to apples."""
    return tuple(norm(w) for w in words)


# ===========================================================================
# 3. Domain vocabulary
# ===========================================================================

SLOT_ORDER = ("service", "date", "time", "location", "name", "phone")

SERVICES = (
    # (key, display Arabic, match phrases - longest first within a service)
    ("photo", "تصوير فوتوغرافي",
     _kw("تصوير فوتوغرافي", "فوتوغرافي", "فوتوغرافية", "فوتو", "صور شخصية",
         "تصوير صور", "فوتوسيشن", "بورتريه")),
    ("video", "تصوير فيديو",
     _kw("تصوير فيديو", "فيديو", "فيديوهات", "فلم", "فيلم", "اعلان", "ريلز",
         "مقطع فيديو")),
    ("interview", "تصوير مقابلة",
     _kw("مقابلة", "مقابلات", "انترفيو", "لقاء", "حوار", "بودكاست")),
    ("products", "تصوير منتجات",
     _kw("تصوير منتجات", "منتجات", "منتج", "المنتجات", "بضاعة", "باكشوت")),
)

_TODAY = _kw("اليوم", "هاليوم")
_TOMORROW = _kw("بكرة", "بكره", "غدا", "غدًا", "الغد", "باكر")
_DAY_AFTER = _kw("بعد بكرة", "بعد بكره", "بعد غد", "بعد باكر")
# Weekdays are matched ONLY in their definite form or after يوم. The bare forms
# are a trap: a phone number read aloud contains `اثنين` (two), which is a
# substring-identical match for `الاثنين` (Monday) once you strip the article -
# it silently overwrote an already-confirmed date during the first demo run.
# Likewise `احد` (one/someone) vs `الأحد` (Sunday).
_WEEKDAYS = (                       # python weekday(): Mon=0 .. Sun=6
    (5, "السبت", _kw("السبت", "يوم سبت")),
    (6, "الأحد", _kw("الاحد", "يوم احد")),
    (0, "الاثنين", _kw("الاثنين", "الإثنين", "يوم اثنين")),
    (1, "الثلاثاء", _kw("الثلاثاء", "يوم ثلاثاء")),
    (2, "الأربعاء", _kw("الاربعاء", "يوم اربعاء")),
    (3, "الخميس", _kw("الخميس", "يوم خميس")),
    (4, "الجمعة", _kw("الجمعه", "يوم جمعه")),
)
MONTHS_AR = ("يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
             "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر")
_MONTH_MATCH = tuple((i + 1, norm(m)) for i, m in enumerate(MONTHS_AR))
WEEKDAY_AR = ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس",
              "الجمعة", "السبت", "الأحد")

EMIRATES = (
    ("دبي", _kw("دبي")),
    ("أبوظبي", _kw("ابوظبي", "ابو ظبي")),
    ("الشارقة", _kw("الشارقه", "شارقه")),
    ("عجمان", _kw("عجمان")),
    ("رأس الخيمة", _kw("راس الخيمه", "راس الخيمة")),
    ("أم القيوين", _kw("ام القيوين")),
    ("الفجيرة", _kw("الفجيره")),
    ("العين", _kw("العين")),
)

_AM = _kw("صباحا", "الصبح", "صباح", "بالصباح", "صباحي", "الفجر")
_PM_EVE = _kw("مساء", "مساءا", "المسا", "بالليل", "ليلا", "الليل", "العشاء")
_PM_NOON = _kw("الظهر", "ظهرا", "بعد الظهر", "العصر", "بعد العصر", "عصرا")

YES = frozenset(_kw("نعم", "ايوه", "ايوا", "اي", "تمام", "صح", "صحيح", "اكيد",
                    "زين", "ماشي", "اوكي", "بالضبط", "مضبوط", "ايه"))
NO = frozenset(_kw("لا", "لأ", "غلط", "خطا", "مو", "مب", "مش", "ليس", "خطأ"))
# Contrast particles: "X مو Y" = "X, not Y". The correction is on the LEFT.
_CONTRAST = _kw("مو", "مب", "مش", "ليس", "وليس", "مهو")

FIELD_WORDS = (
    ("service", _kw("الخدمه", "نوع التصوير", "النوع", "الخدمة")),
    ("date", _kw("التاريخ", "تاريخ", "اليوم اللي", "الموعد")),
    ("time", _kw("الوقت", "الساعه", "التوقيت")),
    ("location", _kw("المكان", "الموقع", "العنوان", "وين")),
    ("name", _kw("الاسم", "اسمي")),
    ("phone", _kw("الرقم", "رقم التواصل", "الهاتف", "التلفون", "الجوال",
                  "الموبايل")),
)

# Returning a greeting is not optional politeness in the Gulf. Failing to answer
# السلام عليكم is the single most machine-like thing an agent can do, and it is
# orthogonal to slot filling - the return greeting is PREPENDED to whatever the
# turn was going to say, so "السلام عليكم، أبغى تصوير فيديو" gets both.
#
# Matched as PHRASES (substring on the folded text) because they are multi-word
# and unambiguous.
_GREET_PHRASE = (
    (_kw("صباح الخير"), "صباح النور"),
    (_kw("مساء الخير"), "مساء النور"),
    (_kw("السلام عليكم", "سلام عليكم"), "وعليكم السلام"),
)
# Matched as TOKENS. `هلا` is a substring of `اهلا` and `سهلا`, so a substring
# test here would fire on our own "أهلاً وسهلاً" echoed back by the caller.
_GREET_TOKEN = (
    (_kw("مرحبا", "مرحبتين", "هلا", "هلو", "اهلين", "اهلا"), "مرحبتين"),
)

FIELD_AR = {"service": "نوع التصوير", "date": "التاريخ", "time": "الوقت",
            "location": "المكان", "name": "الاسم", "phone": "الرقم"}

# Hesitation noises the ASR commits as a whole turn. Most repeated-letter forms
# (مممم, ااااا, هههه) are caught structurally below; these are the ones with real
# distinct letters that no structural rule would flag.
_FILLER = frozenset(_kw(
    "يعني", "امم", "اممم", "مم", "ممم", "اه", "ااه", "اهه", "اوه",
    "هاه", "اها", "هم", "همم", "طق", "ايش", "شو"))

# Plausibility thresholds for the two slots that CANNOT be pattern-matched.
# Deliberately permissive, because the two failure modes are not symmetric:
#
#   a false ACCEPT costs one wrong line in a readback the caller is about to be
#   asked to confirm, and they can correct it;
#   a false REJECT costs the caller their actual name, and they cannot recover
#   by repeating it, because it will be rejected again.
#
# So when a case cannot be separated cleanly, it is ACCEPTED. `علي` is a real
# name at three letters and `دبي` a real place at three, so the floor sits below
# both rather than at some tidier number.
MIN_FREE_TEXT_LETTERS = 3
MIN_ARABIC_RATIO = 0.6

_NAME_LEAD = _kw("اسمي هو", "اسمي", "انا اسمي", "انا", "الاسم هو", "الاسم")
_LOC_LEAD = _kw("في", "ب", "بمنطقه", "منطقه", "المكان في", "المكان", "عند")
_PHONE_HINT = _kw("رقمي", "الرقم", "رقم", "تلفوني", "جوالي", "موبايلي", "هاتفي")

# Hours 1-12 rendered as Arabic ordinal feminine words, for `الساعة ...`.
# A twelve-entry DISPLAY table, not a number parser - it only ever formats a
# clock hour we already hold as an int, and it never reads a transcript.
_HOUR_WORD = ("الثانية عشرة", "الواحدة", "الثانية", "الثالثة", "الرابعة",
              "الخامسة", "السادسة", "السابعة", "الثامنة", "التاسعة",
              "العاشرة", "الحادية عشرة", "الثانية عشرة")

ACKS = ("تمام.", "طيب.", "ممتاز.", "زين.")

# Asking the SAME question twice word for word reads as a crash, even when it is
# logically correct. A person who has just been asked something and answered
# something else does not get read the identical menu again.
#
# So every slot has three phrasings, indexed by how many times we have already
# asked for it:
#   [0] first ask  - full, with the option list
#   [1] re-ask     - short, no list, because the list was already given
#   [2] third+     - the list again, since by now they may genuinely not have
#                    heard it the first time
#
# `greet()` asks for the service AND lists the options, so it counts as ask
# number one. If the counter only started at handle(), the first re-ask would be
# treated as a first ask and would repeat verbatim - which is the exact bug.
_ASKS = {
    "service": (
        "أي نوع تصوير تحتاج؟ فوتوغرافي، فيديو، مقابلة، أو تصوير منتجات؟",
        "بس أي نوع تصوير بالضبط؟",
        "نعيدها: فوتوغرافي، فيديو، مقابلة، أو تصوير منتجات؟",
    ),
    "date": (
        "في أي يوم تحب نحجز التصوير؟",
        "بس ما حددت اليوم، أي يوم يناسبك؟",
        "أي يوم؟ اليوم، بكرة، أو يوم معين في الأسبوع؟",
    ),
    "time": (
        "وأي ساعة تناسبك؟",
        "بس أي ساعة بالضبط؟",
        "أي ساعة؟ مثلاً الساعة تسعة صباحاً؟",
    ),
    "location": (
        "وين بيكون التصوير؟",
        "بس وين بالضبط؟",
        "في أي إمارة أو منطقة بيكون التصوير؟",
    ),
    "name": (
        "ممكن اسمك الكريم؟",
        "بس ما أخذت اسمك، ممكن تعيده؟",
        "ممكن تقول اسمك مرة ثانية؟",
    ),
    "phone": (
        "وآخر شي، ممكن رقم تواصل؟",
        "بس ما وضح الرقم، ممكن تعيده؟",
        "ممكن رقم الموبايل؟ ببطء لو سمحت.",
    ),
}

# How many times we will re-ask one slot before handing it to a human. A voice
# agent that loops forever on a slot it cannot parse is worse than one that
# admits it and moves on.
MAX_ATTEMPTS = 3


# ===========================================================================
# 4. Reply
# ===========================================================================

@dataclass
class Reply:
    text: str
    slots: dict
    done: bool
    used_llm: bool
    ms: float
    debug: dict = field(default_factory=dict)

    def to_json(self):
        return json.dumps({
            "text": self.text, "slots": self.slots, "done": self.done,
            "used_llm": self.used_llm, "ms": round(self.ms, 3),
            "debug": self.debug,
        }, ensure_ascii=False)


# ===========================================================================
# 5. Optional Ollama fallback
# ===========================================================================

# ---------------------------------------------------------------------------
# What the LLM is ALLOWED to be
# ---------------------------------------------------------------------------
# The local model is not a spokesman for the studio. Its entire job is to notice
# that the caller said something off-script and hand back a short clarifying
# QUESTION. It is never permitted to state a fact about the business, because it
# has no access to one: there is no price list, no calendar and no availability
# table anywhere in this repo, so every such "fact" is invented.
#
# That role is enforced by SHAPE, not by trying to enumerate lies:
#
#   1. it must be a question          -> a statement of fact cannot get through
#   2. Arabic script only             -> no other language may be spoken
#   3. no digits, no currency units   -> it cannot quote a price, time or date
#   4. short, single line             -> it cannot deliver a speech
#
# Both of the first two rules exist because the FIRST two real calls to
# qwen2.5:7b-instruct broke them:
#
#   "تبلغ تكلفة التصوير معنا 200 درهم إماراتي"      <- invented a price
#   "ساعات عمل استوديو التصوير是从周一到周五..."      <- switched to Chinese
#
# Note the second one passes `verify_arabic()` perfectly well - it carries no
# presentation forms and no bidi controls - so script purity is a genuinely
# separate gate, not a duplicate of the corruption check.
#
# Discarding is free: the scripted clarification is always available and always
# safe, so there is no reason to be lenient with a reply we are unsure about.

_LLM_MONEY = re.compile(r"درهم|دراهم|ريال|ريالات|دولار|يورو|فلس|"
                        r"[Aa][Ee][Dd]|dirham|riyal|dollar")
_LLM_DIGIT = re.compile(r"[0-9٠-٩۰-۹]")
# Everything we are willing to let through: Arabic block, Arabic supplement and
# extended-A, Arabic punctuation, ASCII punctuation and whitespace. Any codepoint
# outside this is another script and the reply is discarded.
_LLM_ALLOWED = re.compile(
    r"^[؀-ۿݐ-ݿࢠ-ࣿ﴾﴿"
    r"\s.,:;!?'\"()\-،؛؟٪-٭۔]+$")
_LLM_QMARK = ("؟", "?")            # ARABIC QUESTION MARK, or ASCII
MAX_LLM_CHARS = 160


def llm_output_is_safe(line):
    """Return (ok, reason). The reason is surfaced in Reply.debug for the demo."""
    if not isinstance(line, str):
        return False, "not a string"
    line = line.strip()
    if not line:
        return False, "empty"
    if len(line) > MAX_LLM_CHARS:
        return False, "too long (%d chars)" % len(line)
    if "\n" in line:
        return False, "multi-line"
    # Digits and currency are checked BEFORE script purity. An ASCII digit is
    # also "non-Arabic script", so the wrong order refuses an invented price for
    # a true but useless reason ("non-Arabic script: '200'") and buries the thing
    # that actually matters, which is that the model quoted a figure.
    if _LLM_DIGIT.search(line):
        return False, "contains a digit - it may not quote a number"
    if _LLM_MONEY.search(line):
        return False, "contains a currency unit - it may not quote a price"
    if not _LLM_ALLOWED.match(line):
        bad = [c for c in line if not _LLM_ALLOWED.match(c)]
        return False, "non-Arabic script: %r" % ("".join(bad[:8]),)
    if not _ARABIC_LETTER.search(line):
        return False, "no Arabic letters"
    if not line.endswith(_LLM_QMARK):
        return False, "not a question - the model may only ask, never assert"
    return True, "ok"


def make_ollama_llm(model="qwen2.5:7b-instruct",
                    host="http://127.0.0.1:11434",
                    timeout_s=2.5,
                    num_predict=60):
    """Return a `callable(prompt) -> str | None` backed by a local Ollama model.

    $0, no new dependency, stdlib urllib. It is deliberately hard-bounded:

      - `stream: false`, so the server writes nothing until the whole answer is
        ready, which makes urllib's socket timeout an effective WALL-CLOCK bound
        rather than a per-chunk one.
      - `num_predict` capped, because the failure mode on a phone line is a model
        that decides to write an essay.
      - every failure - connection refused, timeout, bad JSON, model not pulled -
        returns None. The agent then speaks a scripted clarification. Nothing
        about the call hangs on this being up.
    """
    url = host.rstrip("/") + "/api/generate"

    def call(prompt):
        body = json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": num_predict, "temperature": 0.2},
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TypeError):
            return None
        out = payload.get("response")
        if not isinstance(out, str) or not out.strip():
            return None
        return out.strip()

    return call


# ===========================================================================
# 6. The agent
# ===========================================================================

class BookingAgent:
    """Slot-filling Arabic booking agent. Feed it `end_of_turn` finals only."""

    ACTS_ON = "end_of_turn"          # read by the telephony integration

    def __init__(self, llm=None, today=None, studio="استوديو بيكسلوجيك"):
        self._llm = llm
        self._studio = studio
        self._today = today                      # injectable for deterministic tests
        self.reset()

    # -- public API ---------------------------------------------------------

    def reset(self):
        self._slots = {k: None for k in SLOT_ORDER}
        self._state = "collect"
        self._asked = None
        self._attempts = {k: 0 for k in SLOT_ORDER}
        self._skipped = set()
        self._turn = 0
        self._period_pending = False
        self._ntoks = []
        self._dtoks = []
        self._pending_greeting = None
        self._ask_count = {}
        self._rejected = []

    @property
    def slots(self):
        return self._public_slots()

    # -- stateless operation (serverless demo) ------------------------------
    #
    # The hosted demo has no long-lived server: the browser talks to AssemblyAI
    # directly with a short-lived token, and each finished turn is one HTTP call
    # to a stateless function. So the whole conversation has to fit in a JSON
    # blob that travels with the request.
    #
    #     st = json.loads(request.body)["state"]
    #     ag = BookingAgent(); ag.load_state(st)
    #     r = ag.handle(text)
    #     return {"reply": r.text, "state": ag.export_state(), "done": r.done}
    #
    # `_ntoks`/`_dtoks` are per-turn scratch and deliberately NOT exported.

    STATE_VERSION = 1

    def export_state(self):
        """JSON-serialisable snapshot sufficient to resume the conversation."""
        return {
            "v": self.STATE_VERSION,
            "slots": {k: (dict(v) if isinstance(v, dict) else v)
                      for k, v in self._slots.items()},
            "state": self._state,
            "asked": self._asked,
            "attempts": dict(self._attempts),
            "skipped": sorted(self._skipped),
            "turn": self._turn,
            "period_pending": self._period_pending,
            "ask_count": dict(self._ask_count),
            "today": (self._today.isoformat() if self._today else None),
        }

    def load_state(self, state):
        """Restore from `export_state()`. Never raises on hostile input.

        This dict arrives in an HTTP body from a stranger's browser, so every
        field is validated by TYPE and by MEMBERSHIP, unknown keys are dropped,
        and anything that does not fit degrades to the fresh value rather than
        propagating a bad type into the turn logic. No eval, no pickle, no
        `__dict__.update`.
        """
        self.reset()
        if not isinstance(state, dict) or not state:
            return

        slots = state.get("slots")
        if isinstance(slots, dict):
            for k in SLOT_ORDER:
                v = slots.get(k)
                if v is None:
                    continue
                if k in ("service",):
                    if isinstance(v, str) and any(v == s[0] for s in SERVICES):
                        self._slots[k] = v
                elif k in ("location", "name", "phone"):
                    if isinstance(v, str) and 0 < len(v) <= 120:
                        self._slots[k] = v
                elif k == "date":
                    d = self._clean_date(v)
                    if d:
                        self._slots[k] = d
                elif k == "time":
                    t = self._clean_time(v)
                    if t:
                        self._slots[k] = t

        st = state.get("state")
        if st in ("collect", "confirm", "fix", "done"):
            self._state = st

        asked = state.get("asked")
        if asked in SLOT_ORDER or asked in ("confirm", "which_field"):
            self._asked = asked

        att = state.get("attempts")
        if isinstance(att, dict):
            for k in SLOT_ORDER:
                v = att.get(k)
                if isinstance(v, int) and not isinstance(v, bool) \
                        and 0 <= v <= MAX_ATTEMPTS:
                    self._attempts[k] = v

        ac = state.get("ask_count")
        if isinstance(ac, dict):
            for k in SLOT_ORDER:
                v = ac.get(k)
                if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 99:
                    self._ask_count[k] = v

        sk = state.get("skipped")
        if isinstance(sk, (list, tuple)):
            self._skipped = set(x for x in sk if x in SLOT_ORDER)

        turn = state.get("turn")
        if isinstance(turn, int) and not isinstance(turn, bool) \
                and 0 <= turn <= 10000:
            self._turn = turn

        self._period_pending = (bool(state.get("period_pending"))
                                and isinstance(self._slots["time"], dict))

        today = state.get("today")
        if isinstance(today, str):
            try:
                self._today = _dt.date(*[int(x) for x in today.split("-")[:3]])
            except (ValueError, TypeError):
                pass

        # A state that claims "confirm" with nothing filled would read an empty
        # booking back. Re-derive the stage from the slots instead of trusting it.
        if self._state in ("confirm", "fix") and self._next_missing() is not None:
            self._state = "collect"
            self._asked = self._next_missing()

    @staticmethod
    def _clean_date(v):
        if not isinstance(v, dict):
            return None
        iso = v.get("iso")
        if not isinstance(iso, str):
            return None
        try:
            d = _dt.date(*[int(x) for x in iso.split("-")[:3]])
        except (ValueError, TypeError):
            return None
        kind = v.get("kind")
        if kind not in ("today", "tomorrow", "day_after", "weekday", "explicit"):
            kind = "explicit"
        return {"kind": kind, "iso": d.isoformat(),
                "weekday": WEEKDAY_AR[d.weekday()], "day": d.day, "month": d.month}

    @staticmethod
    def _clean_time(v):
        if not isinstance(v, dict):
            return None
        h, m = v.get("hour"), v.get("minute", 0)
        if not isinstance(h, int) or isinstance(h, bool) or not 0 <= h <= 23:
            return None
        if not isinstance(m, int) or isinstance(m, bool) or not 0 <= m <= 59:
            m = 0
        return {"hour": h, "minute": m,
                "explicit_period": bool(v.get("explicit_period"))}

    def greet(self):
        t0 = time.perf_counter()
        self._state = "collect"
        self._asked = "service"
        # The greeting LISTS THE OPTIONS, so it is ask number one for `service`.
        # Without this the first re-ask reads as a first ask and repeats the
        # whole menu word for word, which is what made it look broken.
        self._ask_count["service"] = 1
        text = ("أهلاً وسهلاً، معك %s للتصوير. %s"
                % (self._studio, _ASKS["service"][0]))
        return self._reply(text, t0, {"intent": "greet", "extracted": {}})

    def handle_turn(self, turn):
        """Accept an AssemblyAI Turn. REFUSES partials - see CONSTRAINT 1.

        Partials are revised, not extended, so acting on one can act on words the
        caller never said and the action cannot be withdrawn.
        """
        if not getattr(turn, "end_of_turn", False):
            raise ValueError(
                "BookingAgent acts on end_of_turn finals only; got a partial "
                "(%r). Partials are revised, not extended." % (
                    getattr(turn, "transcript", "")[:40],))
        return self.handle(getattr(turn, "transcript", ""))

    def handle(self, final_text):
        t0 = time.perf_counter()
        self._turn += 1
        raw = (final_text or "").strip()
        n = norm(raw)
        self._ntoks = _toks(n)
        self._dtoks = _toks(disp(raw))
        if len(self._dtoks) != len(self._ntoks):     # alignment lost, stay safe
            self._dtoks = list(self._ntoks)
        # Greeting first, and REMOVED from the text the slot extractor sees.
        # Otherwise "مساء الخير" hands the period detector a bare `مساء` and the
        # agent books the shoot for 6pm because the caller said good evening.
        self._rejected = []
        self._pending_greeting, n = self._greeting_strip(n)
        dbg = {"heard": raw, "state_in": self._state, "asked": self._asked}
        if self._pending_greeting:
            dbg["greeting"] = self._pending_greeting

        if not n:
            # Includes "السلام عليكم" on its own: the greeting was stripped, so
            # there is nothing left to extract. It must NOT count as a failed
            # attempt at the pending slot - the caller was being polite.
            dbg["intent"] = "greeting" if self._pending_greeting else "empty"
            return self._reply(self._repeat_question(), t0, dbg)

        if self._state == "done":
            dbg["intent"] = "after_done"
            return self._reply(
                "الحجز مسجّل عندنا. إذا تحتاج أي تعديل قل لي.", t0, dbg,
                done=True)

        polarity = self._polarity(n)
        focus = self._focus(n)
        got = self._extract(focus, n)
        dbg["extracted"] = dict(got)
        dbg["polarity"] = polarity

        if got:
            self._apply(got)

        if self._state == "confirm":
            return self._turn_confirm(got, polarity, n, t0, dbg)
        if self._state == "fix":
            return self._turn_fix(got, n, t0, dbg)
        return self._turn_collect(got, polarity, raw, t0, dbg)

    # -- turn handlers ------------------------------------------------------

    def _turn_collect(self, got, polarity, raw, t0, dbg):
        if not got:
            if polarity is not None:
                # A bare "yes"/"no" while we are collecting is not off-script, it
                # is just an answer to a question we did not ask. Never worth an
                # LLM call.
                dbg["intent"] = "bare_polarity"
                return self._advance(t0, dbg)
            if self._pending_greeting:
                # They only said hello. Answering the greeting IS the reply; do
                # not apologise for not understanding, and do not spend a strike.
                dbg["intent"] = "greeting"
                return self._advance(t0, dbg, count=False)
            if self._rejected:
                # Heard something, but it was not a believable place or name.
                # Say so rather than storing it AND rather than silently
                # dropping it - and let it consume an ask, so the escalation
                # path still terminates instead of looping "sorry, say again".
                r0 = self._rejected[0]
                dbg["intent"] = "implausible"
                dbg["rejected"] = list(self._rejected)
                return self._advance(
                    t0, dbg,
                    prefix="عفواً، ما التقطت %s." % FIELD_AR[r0["slot"]])
            return self._offscript(raw, t0, dbg)

        dbg["intent"] = "slots"
        return self._advance(t0, dbg, got=got)

    def _turn_confirm(self, got, polarity, n, t0, dbg):
        if got:
            dbg["intent"] = "correction_at_confirm"
            return self._advance(t0, dbg, got=got)
        if polarity is True:
            dbg["intent"] = "confirmed"
            self._state = "done"
            self._asked = None
            return self._reply(
                "تم الحجز. بنتواصل معك لتأكيد التفاصيل. شكراً لك.",
                t0, dbg, done=True)
        if polarity is False:
            dbg["intent"] = "rejected"
            self._state = "fix"
            self._asked = "which_field"
            return self._reply(
                "ولا يهمك. أي معلومة تحتاج تعديل؟ "
                "الخدمة، التاريخ، الوقت، المكان، الاسم، أو الرقم؟", t0, dbg)
        # "الوقت" alone at the readback means "the time is the wrong bit",
        # without the caller having to say no first.
        fld = self._field_word(n)
        if fld:
            dbg["intent"] = "confirm_field_pointed"
            self._state = "fix"
            self._asked = "which_field"
            return self._turn_fix({}, n, t0, dbg)
        dbg["intent"] = "confirm_unclear"
        return self._reply(
            "ما وصلني ردك. هل التفاصيل صحيحة؟ قل نعم أو لا.", t0, dbg)

    def _turn_fix(self, got, n, t0, dbg):
        if got:
            dbg["intent"] = "fix_value"
            return self._advance(t0, dbg, got=got)
        fld = self._field_word(n)
        if fld:
            dbg["intent"] = "fix_field"
            dbg["field"] = fld
            self._slots[fld] = None
            self._attempts[fld] = 0
            self._ask_count[fld] = 0
            self._skipped.discard(fld)
            if fld == "time":
                self._period_pending = False
            self._state = "collect"
            self._asked = fld
            return self._reply(self._ask(fld), t0, dbg)
        dbg["intent"] = "fix_unclear"
        return self._reply(
            "أي معلومة بالضبط؟ الخدمة، التاريخ، الوقت، المكان، الاسم، أو الرقم؟",
            t0, dbg)

    # -- flow ---------------------------------------------------------------

    def _advance(self, t0, dbg, got=None, prefix="", count=True):
        """Ask the next missing thing, or read the whole booking back.

        This is also the ONE place that counts attempts. A slot we ask for and
        still do not have after MAX_ATTEMPTS is handed to a human instead of
        being asked a fourth time - a voice agent stuck in a slot loop is the
        single most infuriating failure mode on a phone line, and with
        `arabic_numbers` absent the time slot is genuinely unfillable.
        """
        progress = bool(got)
        lead = prefix.strip()
        self._state = "collect"
        while True:
            pending = "time" if self._period_pending else self._next_missing()
            if pending is None:
                self._state = "confirm"
                self._asked = "confirm"
                # No echo here: the readback IS the echo, and doing both reads
                # the same booking out twice in one breath.
                if not lead:
                    lead = self._ack_for(got)
                return self._reply(
                    (lead + " " + self._readback()).strip(), t0, dbg)

            # Bump only on a turn that made NO progress. A caller who answers
            # "where?" with their name has not failed the location slot, they
            # have just filled a different one - counting that as a strike
            # escalated a perfectly answerable date slot in four turns.
            if self._asked == pending and not progress and count:
                self._bump(pending)
                if self._attempts[pending] > MAX_ATTEMPTS:
                    self._skipped.add(pending)
                    self._attempts[pending] = 0
                    if pending == "time":
                        self._period_pending = False
                    dbg.setdefault("escalated", []).append(pending)
                    lead = (lead + " بنرجع لك بخصوص %s لاحقاً."
                            % FIELD_AR[pending]).strip()
                    continue

            self._asked = pending
            k = self._ask_count.get(pending, 0)
            if not lead:
                lead = self._echo(got, k)
            q = (self._period_question(k) if self._period_pending
                 else self._ask(pending, k))
            if count:
                self._ask_count[pending] = k + 1
            dbg["ask_index"] = k
            return self._reply((lead + " " + q).strip(), t0, dbg)

    def _next_missing(self):
        for k in SLOT_ORDER:
            if self._slots[k] is None and k not in self._skipped:
                return k
        return None

    def _repeat_question(self):
        """Re-anchor on whatever we last asked. Never leave a caller with nothing."""
        if self._state == "confirm":
            return "هل التفاصيل صحيحة؟ قل نعم أو لا."
        if self._state == "fix":
            return ("أي معلومة تحتاج تعديل؟ "
                    "الخدمة، التاريخ، الوقت، المكان، الاسم، أو الرقم؟")
        if self._period_pending and self._slots["time"]:
            return self._period_question(self._ask_count.get("time", 0))
        nxt = self._asked if self._asked in SLOT_ORDER else self._next_missing()
        if nxt is None:
            return "تفضل، كيف أقدر أساعدك؟"
        return self._ask(nxt, self._ask_count.get(nxt, 0))

    def _ack_for(self, got):
        """One acknowledgement word, chosen by CONTENT, not by a turn counter.

        The old version cycled ACKS on `self._turn`, so it emitted an approving
        word after literally every utterance, in the same order, forever. That
        metronome is the tell - people do not say "great" six times in a row.

        Two changes: it only fires when the caller actually supplied something,
        and the word is derived from what they supplied. `crc32` rather than
        `hash()` because hash() is salted per process, and `export_state()`
        round-trip equality is asserted across separate agents.
        """
        if not got or self._pending_greeting:
            return ""
        key = "|".join("%s=%s" % (k, got[k]) for k in sorted(got))
        return ACKS[zlib.crc32(key.encode("utf-8")) % len(ACKS)]

    def _echo(self, got, asked_before=0):
        """Read back a multi-slot turn compactly: 'تمام، بكرة الساعة التاسعة صباحاً.'

        Warm, but mainly FUNCTIONAL: it is the caller's first chance to catch a
        misheard time, instead of discovering it at the final readback after
        answering four more questions.
        """
        ack = self._ack_for(got)
        if not got:
            return ack
        filled = [k for k in SLOT_ORDER if k in got]
        # On a RE-ask, always say what we did understand, even if it was only one
        # thing. Re-asking is the moment the caller most needs reassuring that
        # they were heard at all - and it is the one thing the agent got right.
        if len(filled) < 2 and asked_before < 1:
            return ack
        parts = []
        for k in filled:
            if k == "service":
                parts.append(self._service_ar())
            elif k == "date":
                parts.append(self._date_ar(compact=True))
            elif k == "time":
                # An unresolved am/pm must not be echoed as if it were settled -
                # we are about to ask which one it is.
                if not self._period_pending:
                    parts.append(self._time_ar())
            elif k == "location":
                parts.append("في %s" % self._slots["location"])
            elif k == "name":
                parts.append("باسم %s" % self._slots["name"])
            elif k == "phone":
                parts.append("ورقم %s" % self._slots["phone"])
        parts = [p for p in parts if p]
        if not parts or (len(parts) < 2 and asked_before < 1):
            return ack
        return "%s، سجلت %s." % (ack.rstrip("."), " ".join(parts))

    def _ask(self, slot, asked_before=0):
        """Phrase the question for `slot`, given how many times we already asked.

        Never byte-identical two asks running: the second is short and drops the
        option list (it was already read out), the third brings the list back.
        """
        forms = _ASKS.get(slot)
        if not forms:
            return "تفضل."
        return forms[min(max(int(asked_before), 0), len(forms) - 1)]

    def _period_question(self, asked_before=0):
        h = self._slots["time"]["hour"] % 12 or 12
        if asked_before >= 1:
            return "صباحاً أو مساءً؟"
        return "الساعة %s صباحاً أو مساءً؟" % _HOUR_WORD[h]

    def _offscript(self, raw, t0, dbg):
        """Rules found nothing and the caller has gone off-script.

        This is the ONLY path that may call an LLM, and it is still optional:
        with `llm=None` the agent apologises and re-anchors on the pending
        question, which is a working agent, not a degraded one.
        """
        dbg["intent"] = "offscript"
        pending = self._asked if self._asked in SLOT_ORDER else self._next_missing()
        if self._llm is not None and pending is not None \
                and self._attempts[pending] < MAX_ATTEMPTS:
            reply = self._ask_llm(raw, self._repeat_question(), dbg)
            if reply:
                self._bump(pending)
                return self._reply(reply, t0, dbg, used_llm=True)
        return self._advance(t0, dbg, prefix="عفواً، ما وصلتني.")

    def _ask_llm(self, raw, question, dbg):
        """One bounded call. Returns clean Arabic, or None."""
        prompt = (
            "أنت موظف استقبال في استوديو تصوير في الإمارات، "
            "ووظيفتك الوحيدة أن تطلب توضيحاً من العميل.\n"
            "اكتب سؤال توضيح واحد فقط بالعربية الفصحى، سطر واحد قصير "
            "ينتهي بعلامة استفهام.\n"
            "ممنوع منعاً باتاً: ذكر أي سعر أو رقم أو تاريخ أو مدة، "
            "أو تأكيد أي موعد، أو استخدام أي لغة غير العربية.\n"
            "كلام العميل: %s\n"
            "سؤال التوضيح:" % raw
        )
        t = time.perf_counter()
        out = None
        try:
            out = self._llm(prompt)
        except Exception as e:                                   # noqa: BLE001
            dbg["llm_error"] = repr(e)[:120]
        dbg["llm_ms"] = round((time.perf_counter() - t) * 1000.0, 1)
        if not out:
            dbg["llm"] = "none"
            return None
        line = out.strip().splitlines()[0].strip()
        try:
            # Third-party text gets CHECKED and DISCARDED on failure. Our own
            # text gets checked and RAISES - a corrupt string we generated is a
            # bug in this file, a corrupt string a model returned is Tuesday.
            verify_arabic(line)
        except ArabicCorruption as e:
            dbg["llm"] = "rejected: corrupt Arabic: %s" % str(e)[:70]
            return None
        ok, why = llm_output_is_safe(line)
        if not ok:
            dbg["llm"] = "rejected: %s" % why
            return None
        dbg["llm"] = "used"
        return line + " " + question

    def _bump(self, slot):
        if slot in self._attempts:
            self._attempts[slot] += 1

    # -- extraction ---------------------------------------------------------

    def _greeting_strip(self, n):
        """Return (return_greeting_or_None, text with the greeting removed)."""
        for phrases, reply in _GREET_PHRASE:
            for p in phrases:
                if p and p in n:
                    return reply, _WS.sub(" ", n.replace(p, " ")).strip()
        toks = _toks(n)
        for words, reply in _GREET_TOKEN:
            ws = set(words)
            if set(toks) & ws:
                return reply, " ".join(t for t in toks if t not in ws)
        return None, n

    def _polarity(self, n):
        t = set(_toks(n))
        if t & YES and not (t & NO):
            return True
        if t & NO:
            return False
        return None

    def _focus(self, n):
        """"الساعة عشرة مو تسعة" -> the correction is LEFT of the contrast word.

        Without this, a number parser reading left to right is as likely to pick
        up the value the caller just rejected as the one they meant.
        """
        toks = _toks(n)
        while toks and toks[0] in NO:       # leading bare rejection: "لا، الساعة..."
            toks = toks[1:]
        for i in range(len(toks) - 1, 0, -1):
            if toks[i] in _CONTRAST:
                left = [t for t in toks[:i] if t not in NO]
                if left:
                    return " ".join(left)
        return " ".join(toks)

    def _extract(self, focus, full):
        got = {}
        # Phone FIRST and exclusively. A UAE mobile read aloud is ten number
        # words in a row, and number words collide with everything: `اثنين` with
        # Monday, `خمسة` with a clock hour. When we asked for the number and got
        # a number, that is the whole utterance - do not mine it for anything else.
        ph = self._phone(focus, full)
        if ph and self._asked == "phone":
            return {"phone": ph}

        s = self._service(focus)
        if s:
            got["service"] = s
        d = self._date(focus)
        if d:
            got["date"] = d
        tm = self._time(focus)
        if tm:
            got["time"] = tm
        elif self._period_pending:
            p = self._period(focus)
            if p:
                got["period_only"] = p
        if ph:
            got["phone"] = ph
        # Free-text slots (location, name) swallow a whole utterance when that
        # slot is the one we just asked for. So they only fire when NOTHING
        # structured matched - otherwise "بكرة" in answer to "where?" would set
        # the date AND become the location string.
        structured = bool(got)
        loc = self._location(focus, structured)
        if loc:
            got["location"] = loc
        nm = self._name(focus, structured or "location" in got)
        if nm:
            got["name"] = nm
        return got

    def _service(self, n):
        for key, _disp, phrases in SERVICES:
            for p in phrases:
                if p and p in n:
                    return key
        return None

    def _date(self, n):
        today = self._today or _dt.date.today()
        for p in _DAY_AFTER:                       # before _TOMORROW: superstring
            if p in n:
                return self._mkdate("day_after", today + _dt.timedelta(days=2))
        for p in _TODAY:
            if p in n:
                return self._mkdate("today", today)
        for p in _TOMORROW:
            if p in n:
                return self._mkdate("tomorrow", today + _dt.timedelta(days=1))
        toks = set(_toks(n))
        for wd, _label, phrases in _WEEKDAYS:
            if toks & set(phrases):
                ahead = (wd - today.weekday()) % 7
                # "الخميس" spoken ON Thursday means the NEXT one for a booking.
                ahead = ahead or 7
                return self._mkdate("weekday", today + _dt.timedelta(days=ahead))
        for mnum, mname in _MONTH_MATCH:
            if mname in n:
                day = self._day_number(n, mname)
                if day:
                    year = today.year + (1 if mnum < today.month else 0)
                    try:
                        return self._mkdate("explicit", _dt.date(year, mnum, day))
                    except ValueError:
                        return None
        return None

    def _day_number(self, n, mname):
        d = _digits(n)
        m = re.search(r"\b(\d{1,2})\b", d)
        if m:
            v = int(m.group(1))
            return v if 1 <= v <= 31 else None
        if arabic_numbers is None or not hasattr(arabic_numbers, "parse_numbers"):
            return None
        try:
            nums = arabic_numbers.parse_numbers(n)
        except Exception:                                        # noqa: BLE001
            return None
        for item in nums or []:
            v = getattr(item, "value", None)
            if v is not None and 1 <= v <= 31 and float(v).is_integer():
                return int(v)
        return None

    def _mkdate(self, kind, d):
        return {"kind": kind, "iso": d.isoformat(),
                "weekday": WEEKDAY_AR[d.weekday()],
                "day": d.day, "month": d.month}

    def _period(self, n):
        for p in _PM_NOON:
            if p in n:
                return "noon"
        for p in _AM:
            if p in n:
                return "am"
        for p in _PM_EVE:
            if p in n:
                return "pm"
        return None

    def _time(self, n):
        if arabic_numbers is None or not hasattr(arabic_numbers, "parse_time"):
            return None
        try:
            t = arabic_numbers.parse_time(n)
        except Exception:                                        # noqa: BLE001
            return None
        if t is None:
            return None
        hour = getattr(t, "hour", None)
        if hour is None or not (0 <= int(hour) <= 23):
            return None
        hour = int(hour)
        minute = int(getattr(t, "minute", 0) or 0)
        explicit = bool(getattr(t, "explicit_period", False))
        period = self._period(n)
        if period == "noon" and hour < 12:
            hour += 12
            explicit = True
        elif period == "pm" and hour < 12:
            hour += 12
            explicit = True
        elif period == "am":
            if hour == 12:
                hour = 0
            explicit = True
        return {"hour": hour, "minute": minute, "explicit_period": explicit}

    def _implausible(self, cand):
        """Why `cand` is not a believable place or person name, or None if it is.

        A PLAUSIBILITY test, not a whitelist. A list of UAE place names would
        reject a real address, and rejecting something real is the worse error
        here: a wrong location is one line in a readback the caller is about to
        be asked to confirm, but a rejected name cannot be recovered by the
        caller repeating it, because it will be rejected again.

        Everything below is therefore a structural property of noise, not a
        judgement about meaning. Anything that is merely unusual is ACCEPTED.
        """
        t = (cand or "").strip()
        if not t:
            return "empty"
        letters = _ARABIC_LETTER.findall(t)
        if len(letters) < MIN_FREE_TEXT_LETTERS:
            # `علي` and `دبي` are three letters, so the floor sits below them.
            return "too short (%d Arabic letters)" % len(letters)
        nonspace = [c for c in t if not c.isspace()]
        if nonspace and (len(letters) / float(len(nonspace))) < MIN_ARABIC_RATIO:
            return "mostly not Arabic letters"
        toks = _toks(t)
        if toks and set(toks) <= _FILLER:
            return "hesitation word"
        if len(toks) == 1:
            w = toks[0]
            # Filler noise transcribes as one unit repeated: مممم, ااااا, هههه,
            # بلابلابلا. Requiring THREE or more repeats keeps real reduplicated
            # Arabic words (زلزل, سلسل - unit repeated twice) out of the net.
            for u in (1, 2, 3):
                if len(w) >= u * 3 and len(w) % u == 0 and w == w[:u] * (len(w) // u):
                    return "repeated syllable %r" % w[:u]
        if self._service(t):
            # They are still answering the PREVIOUS question, not this one.
            return "an option from the previous question"
        return None

    def _reject(self, slot, cand, why):
        self._rejected.append({"slot": slot, "heard": cand, "why": why})
        return None

    def _disp_of(self, sub):
        """Map a normalised substring back to the caller's own spelling."""
        st = _toks(sub)
        if not st:
            return sub
        nt, dt = self._ntoks, self._dtoks
        for i in range(len(nt) - len(st) + 1):
            if nt[i:i + len(st)] == st:
                return " ".join(dt[i:i + len(st)])
        return sub

    def _location(self, n, structured=False):
        for label, phrases in EMIRATES:
            for p in phrases:
                if p in n:
                    # keep any area the caller added: "دبي، منطقة الخليج التجاري"
                    extra = _WS.sub(" ", n.replace(p, " ")).strip()
                    extra = self._strip_lead(extra, _LOC_LEAD).strip()
                    if extra and len(_toks(extra)) <= 4:
                        # Arabic comma, not a dash: this string is SPOKEN.
                        return label + "، " + self._disp_of(extra)
                    return label
        if (not structured and self._asked == "location"
                and self._polarity(n) is None):
            free = self._strip_lead(n, _LOC_LEAD)
            if free and len(_toks(free)) <= 6:
                why = self._implausible(free)
                if why:
                    return self._reject("location", free, why)
                return self._disp_of(free)
        return None

    def _name(self, n, structured=False):
        lead = self._has_lead(n, _NAME_LEAD)
        if lead:
            cand = self._strip_lead(n, _NAME_LEAD)
        elif (not structured and self._asked == "name"
                and self._polarity(n) is None):
            cand = n
        else:
            return None
        cand = cand.strip()
        if not cand:
            return None
        if re.search(r"\d", _digits(cand)):
            return self._reject("name", cand, "a name does not contain digits")
        toks = _toks(cand)
        if not (1 <= len(toks) <= 4):
            return None
        if set(toks) & (YES | NO):
            return None
        if self._service(cand) or self._date(cand) or self._period(cand):
            return None
        why = self._implausible(cand)
        if why:
            return self._reject("name", cand, why)
        return self._disp_of(cand)

    def _has_lead(self, n, leads):
        for p in sorted(leads, key=len, reverse=True):
            if p and (n == p or n.startswith(p + " ")):
                return p
        return None

    def _strip_lead(self, n, leads):
        p = self._has_lead(n, leads)
        return n[len(p):].strip() if p else n

    def _phone(self, focus, full):
        d = _digits(full)
        compact = re.sub(r"[\s\-()+]", "", d)
        m = re.search(r"\d{7,15}", compact)
        if m:
            return self._norm_phone(m.group(0))
        wants = (self._asked == "phone"
                 or any(p in focus for p in _PHONE_HINT))
        if not wants:
            return None
        if arabic_numbers is None or not hasattr(arabic_numbers, "parse_numbers"):
            return None
        try:
            nums = arabic_numbers.parse_numbers(focus)
        except Exception:                                        # noqa: BLE001
            return None
        buf = ""
        for item in nums or []:
            v = getattr(item, "value", None)
            if v is None or not float(v).is_integer() or v < 0:
                continue
            buf += str(int(v))
        if 7 <= len(buf) <= 15:
            return self._norm_phone(buf)
        return None

    @staticmethod
    def _norm_phone(s):
        if s.startswith("00971"):
            s = "0" + s[5:]
        elif s.startswith("971") and len(s) >= 12:
            s = "0" + s[3:]
        if len(s) == 9 and s.startswith("5"):
            s = "0" + s
        return s

    def _field_word(self, n):
        for fld, phrases in FIELD_WORDS:
            for p in phrases:
                if p in n:
                    return fld
        return None

    # -- applying -----------------------------------------------------------

    def _apply(self, got_in):
        # COPY. `got` is also the caller's "did anything happen this turn?"
        # signal, and popping from it in here made answering the AM/PM question
        # look like an empty extraction, which sent a perfectly good `مساءً`
        # down the off-script path.
        got = dict(got_in)
        if "period_only" in got and self._slots["time"]:
            p = got.pop("period_only")
            h = self._slots["time"]["hour"] % 12
            if p in ("pm", "noon"):
                h += 12
            self._slots["time"]["hour"] = h
            self._slots["time"]["explicit_period"] = True
            self._period_pending = False
        for k, v in got.items():
            if k not in self._slots:
                continue
            self._slots[k] = v
            self._attempts[k] = 0
            self._ask_count[k] = 0       # answered: the next ask starts fresh
            self._skipped.discard(k)
        if "time" in got:
            t = self._slots["time"]
            # An unqualified 1-11 is genuinely ambiguous for a shoot. Ask, the
            # way a receptionist would, instead of silently guessing morning.
            self._period_pending = (not t["explicit_period"]
                                    and 1 <= t["hour"] <= 11)

    # -- rendering ----------------------------------------------------------

    def _service_ar(self):
        k = self._slots["service"]
        for key, disp, _ in SERVICES:
            if key == k:
                return disp
        return None

    def _date_ar(self, compact=False):
        d = self._slots["date"]
        if not d:
            return None
        # The echo wants "بكرة"; the final readback wants "بكرة 19 سبتمبر",
        # because that is where an off-by-one day has to be catchable.
        tail = "" if compact else " %d %s" % (d["day"], MONTHS_AR[d["month"] - 1])
        kind = d["kind"]
        if kind == "today":
            return ("اليوم%s" % tail).strip()
        if kind == "tomorrow":
            return ("بكرة%s" % tail).strip()
        if kind == "day_after":
            return ("بعد بكرة%s" % tail).strip()
        if kind == "weekday":
            return ("يوم %s%s" % (d["weekday"], tail)).strip()
        return ("يوم %s%s" % (d["weekday"], tail)).strip()

    def _time_ar(self):
        t = self._slots["time"]
        if not t:
            return None
        h, mnt = t["hour"], t["minute"]
        if mnt == 45:
            h2, word = (h + 1) % 24, "الساعة %s إلا ربع"
        else:
            h2, word = h, "الساعة %s"
        base = word % _HOUR_WORD[h2 % 12 or 12]
        if mnt == 30:
            base += " والنصف"
        elif mnt == 15:
            base += " والربع"
        elif mnt not in (0, 45):
            base += " و%d دقيقة" % mnt
        if h < 12:
            return base + " صباحاً"
        if h < 17:
            return base + " بعد الظهر"
        return base + " مساءً"

    def _readback(self):
        bits = []
        bits.append(self._service_ar() or "التصوير")
        bits.append(self._date_ar() or "التاريخ بيتأكد لاحقاً")
        bits.append(self._time_ar() or "الوقت بيتأكد لاحقاً")
        loc = self._slots["location"]
        bits.append("في %s" % loc if loc else "المكان بيتأكد لاحقاً")
        nm = self._slots["name"]
        bits.append("باسم %s" % nm if nm else "الاسم بيتأكد لاحقاً")
        ph = self._slots["phone"]
        bits.append("ورقم التواصل %s" % ph if ph else "ورقم التواصل ناقص")
        return ("خليني أأكد الحجز: " + "، ".join(bits)
                + ". هل التفاصيل صحيحة؟")

    def _public_slots(self):
        out = {}
        for k in SLOT_ORDER:
            v = self._slots[k]
            out[k] = dict(v) if isinstance(v, dict) else v
        out["_display"] = {
            "service": self._service_ar(),
            "date": self._date_ar(),
            "time": self._time_ar(),
        }
        return out

    def _reply(self, text, t0, dbg, done=False, used_llm=False):
        # The return greeting is PREPENDED here, in the one place every reply
        # passes through, so it cannot be forgotten on the confirm, fix or
        # off-script paths. Cleared immediately: one greeting per turn.
        if self._pending_greeting:
            # The body may already OPEN with punctuation, because the
            # acknowledgement path builds its own lead-in. Joining blindly
            # produced `وعليكم السلام، ، سجلت` on the live demo: two commas and
            # a gap, in the one language this whole project is about. Strip any
            # leading comma or full stop off the body before joining, and do not
            # add a separator if the body supplies its own.
            body = text.lstrip().lstrip("،,.").lstrip()
            text = self._pending_greeting + "، " + body
            self._pending_greeting = None
        text = _WS.sub(" ", text).strip()
        # Belt and braces: collapse any repeated Arabic comma that some future
        # path reintroduces. Cheap, and the failure is embarrassing rather than
        # subtle.
        text = _DOUBLE_COMMA.sub("،", text)
        verify_arabic(text)                      # CONSTRAINT 3, enforced here
        dbg = dict(dbg)
        dbg["state_out"] = self._state
        dbg["asking"] = self._asked
        dbg["attempts"] = {k: v for k, v in self._attempts.items() if v}
        if self._skipped:
            dbg["skipped"] = sorted(self._skipped)
        return Reply(text=text, slots=self._public_slots(), done=done,
                     used_llm=used_llm,
                     ms=(time.perf_counter() - t0) * 1000.0, debug=dbg)


# ===========================================================================
# 7. Demo
# ===========================================================================

if __name__ == "__main__":
    import sys

    use_llm = "--llm" in sys.argv
    ag = BookingAgent(llm=make_ollama_llm() if use_llm else None,
                      today=_dt.date(2026, 9, 18))
    script = [
        "السلام عليكم، أبغى أحجز تصوير فيديو",
        "بكرة الساعة تسعة صباحاً",
        "في دبي، منطقة الخليج التجاري",
        "اسمي أحمد المنصوري",
        "رقمي صفر خمسة صفر واحد اثنين ثلاثة أربعة خمسة ستة سبعة",
        "نعم",
    ]
    r = ag.greet()
    print("AGENT :", r.text)
    for line in script:
        print("CALLER:", line)
        r = ag.handle(line)
        print("AGENT :", r.text, "   [%.2f ms%s]" % (
            r.ms, ", llm" if r.used_llm else ""))
        if r.done:
            break
    print()
    print(json.dumps(ag.slots, ensure_ascii=False, indent=2))
