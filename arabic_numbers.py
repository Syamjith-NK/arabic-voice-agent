# -*- coding: utf-8 -*-
"""Arabic number-word parser.

Why this exists
---------------
AssemblyAI ``universal-3-5-pro`` returns Arabic numbers as **words**, never digits
(NOTES.md section 5, CONSTRAINT 2).  The live fixture in this repo says
``الساعة تسعة صباحاً`` -- there is no ``9`` anywhere in the transcript, so every
downstream booking / time / price / quantity intent that matches ``\\d{1,2}``
finds nothing.  This module turns those words into numbers.

Design stance: **under-matching beats over-matching.**  A parser that silently
books the wrong time is worse than one that says "I did not understand".  Where a
reading is genuinely ambiguous this module refuses rather than guesses, and every
such refusal is listed under "Known limits" below.

All matching is done on whole *tokens*, never substrings, so a number word buried
inside a longer word can never match.

Arabic text discipline
----------------------
Every Arabic string in this file is stored in **logical order** and contains no
presentation forms (U+FB50-U+FEFF) and no bidi control characters.  The lexicon is
written in ordinary spelling and normalised at import time by the same function
used on input, so there is exactly one normalisation rule and it cannot drift.

Public API
----------
``parse_numbers(text) -> list[NumberMatch]``   left-to-right by ``start``
``normalize_digits(text) -> str``              number spans replaced by Western digits
``parse_time(text) -> ParsedTime | None``
``parse_quantity(text) -> float | None``

Supported
---------
* ones 0-10, masculine and feminine (``ثلاثة`` / ``ثلاث``)
* teens 11-19, masculine and feminine (``أحد عشر`` / ``إحدى عشرة``)
* tens 20-90, both case endings (``عشرون`` / ``عشرين``)
* waw compounds, units-first (``خمسة وأربعين`` = 45, not 5 then 40)
* hundreds and thousands (``مئة``، ``مئتين``، ``ثلاثمئة``، ``ألف``، ``ألفين``،
  ``خمسة آلاف``، ``خمسة وعشرين ألف``)
* Arabic-Indic ٠١٢٣٤٥٦٧٨٩ and Eastern Arabic ۰۱۲۳۴۵۶۷۸۹ digits, and mixed text
* clock times, cardinal *and* ordinal (``الساعة تسعة`` and ``الساعة التاسعة``)
* time modifiers ``والنصف`` +30، ``والربع`` +15، ``وثلث`` +20،
  ``إلا ربعاً`` -15، ``إلا عشر دقائق`` -10، ``وعشرين دقيقة`` +20
* period words to 24h: ``صباحاً`` ``مساءً`` ``ظهراً`` ``عصراً`` ``ليلاً``
  ``بعد الظهر`` ``منتصف الليل``

Known limits -- read these, they are the honest ceiling
-------------------------------------------------------
1. **Ceiling is 999,999.**  ``مليون`` / ``مليار`` are NOT in the lexicon, so
   ``مليون درهم`` yields no match at all rather than a wrong one.
2. **Clitic prefixes ب / ل / ك / ف are not stripped.**  ``بعشرة دراهم`` does not
   match.  This is deliberate: ``لست`` ("I am not") would otherwise strip to
   ``ست`` = 6.  Only the conjunction ``و`` is stripped, and only when the
   remainder is itself a number word.
3. **The definite article ال is not stripped from cardinals.**  Otherwise
   ``يوم الاثنين`` (Monday) becomes 2.  Ordinals are listed *with* their article.
4. **Bare feminine ordinals are not numbers.**  ``الثالثة`` matches only directly
   after ``الساعة``.  Masculine ordinals (``الثاني``) never match -- ``الطابق
   الثاني`` is the second floor, not 2.
5. **Fractions are not parsed.**  ``ثلاثة أرباع`` returns nothing, not 3.
   ``نصف`` / ``ربع`` count only as clock modifiers.
6. **No dates, no ordinal day-of-month, no phone-number grouping.**  A spoken
   digit run ``خمسة صفر اثنين`` returns three separate matches [5, 0, 2]; joining
   them into a phone number is the caller's job.
7. **AM/PM is never guessed.**  With no period word, ``hour`` is left exactly as
   spoken (1-12) and ``explicit_period`` is False.
8. ``ليلاً`` uses a convention: hours 1-4 stay as-is (01:00-04:00), 12 becomes 0,
   everything else gains 12.  That is a heuristic, documented rather than hidden.
9. Dialect coverage is thin and mostly untested.  ``ثنين`` / ``ثنتين`` (two) and
   ``ونص`` (half) are in; wider Gulf/Levantine/Egyptian forms are not.
10. **The 999,999 ceiling applies to number WORDS only.**  A digit run is read
    straight through, so ``123456789012345`` parses to that float.  If you need a
    range check, do it on the value you get back.
11. ``normalize_digits`` replaces number *words* -- it does not assemble times.
    ``الساعة الثالثة والنصف`` becomes ``الساعة 3 والنصف``, not ``3:30``.  Use
    ``parse_time`` for that.
12. Bidi control characters already present in the input are **preserved, not
    stripped**.  This module never introduces one; cleaning an already-dirty
    transcript is a different job.

Stdlib only.  Python 3.9+.  Verified on 3.9.6 (the CommandLineTools interpreter
launchd uses) and on 3.14.7.
"""

import re
import unicodedata
from typing import Dict, List, Optional, Tuple

__all__ = [
    "NumberMatch",
    "ParsedTime",
    "parse_numbers",
    "normalize_digits",
    "parse_time",
    "parse_quantity",
]


# --------------------------------------------------------------------------
# character classes
# --------------------------------------------------------------------------

# Arabic letters (logical order, no presentation forms).
_LETTERS = "\u0620-\u063A\u0640-\u064A\u066E\u066F\u0671-\u06D3\u06D5\u06EE\u06EF\u06FA-\u06FF"
# Combining marks: harakat, tanween, sukun, superscript alef, Quranic marks.
_MARKS = "\u064B-\u065F\u0670\u06D6-\u06DC\u06DF-\u06E8\u06EA-\u06ED"
# Three digit families.
_DIGITS = "0-9\u0660-\u0669\u06F0-\u06F9"

_TOKEN_RE = re.compile(
    "[{d}]+(?:[.٫][{d}]+)?|[{l}{m}]+|[A-Za-z]+".format(d=_DIGITS, l=_LETTERS, m=_MARKS)
)
_MARK_RE = re.compile("[{m}ـ]".format(m=_MARKS))
_DIGIT_ONLY_RE = re.compile("^[{d}]+(?:[.٫][{d}]+)?$".format(d=_DIGITS))

# Presentation forms + bidi controls -- must never appear in output.
_FORBIDDEN_RE = re.compile("[\uFB50-\uFEFF\u200E\u200F\u202A-\u202E\u2066-\u2069]")

_DIGIT_MAP = {}
for _i in range(10):
    _DIGIT_MAP[chr(0x0660 + _i)] = str(_i)   # Arabic-Indic
    _DIGIT_MAP[chr(0x06F0 + _i)] = str(_i)   # Eastern Arabic-Indic
_DIGIT_MAP["٫"] = "."                    # Arabic decimal separator
_DIGIT_MAP["٬"] = ""                     # Arabic thousands separator

# Letter folding applied to both lexicon and input, so they cannot disagree.
_FOLD = {
    "أ": "ا",  # alef with hamza above -> alef
    "إ": "ا",  # alef with hamza below -> alef
    "آ": "ا",  # alef madda        -> alef
    "ٱ": "ا",  # alef wasla        -> alef
    "ى": "ي",  # alef maksura      -> yeh
    "ة": "ه",  # teh marbuta       -> heh
    "ئ": "ي",  # yeh with hamza    -> yeh
    "ؤ": "و",  # waw with hamza    -> waw
}


def _norm(word: str) -> str:
    """Fold one token for lexicon lookup.

    Removes diacritics and tatweel, then folds hamza seats, alef maksura and teh
    marbuta.  Never emits a presentation form: it only deletes characters or maps
    them to base Arabic letters.
    """
    word = unicodedata.normalize("NFC", word)
    word = _MARK_RE.sub("", word)
    return "".join(_FOLD.get(ch, ch) for ch in word)


def _digits_to_western(tok: str) -> str:
    return "".join(_DIGIT_MAP.get(ch, ch) for ch in tok)


# --------------------------------------------------------------------------
# tokenisation
# --------------------------------------------------------------------------

class _Tok(object):
    __slots__ = ("raw", "norm", "start", "end", "num")

    def __init__(self, raw, start, end):
        self.raw = raw
        self.start = start
        self.end = end
        self.norm = _norm(raw)
        self.num = None
        if _DIGIT_ONLY_RE.match(raw):
            try:
                self.num = float(_digits_to_western(raw))
            except ValueError:
                self.num = None

    def __repr__(self):  # pragma: no cover - debugging aid
        return "_Tok(%r,%d,%d)" % (self.raw, self.start, self.end)


def _tokenize(text: str) -> List[_Tok]:
    return [_Tok(m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]


# --------------------------------------------------------------------------
# lexicon.  Written in ordinary logical-order Arabic, normalised at import.
# kinds: unit (0-10) | teen | ten | hundred | thousand | scale100 | scale1000
# --------------------------------------------------------------------------

def _build(pairs) -> Dict[str, Tuple[float, str]]:
    out = {}
    for spelling, value, kind in pairs:
        key = _norm(spelling)
        if key in out and out[key][0] != float(value):
            raise AssertionError("lexicon collision on %r: %r vs %r" % (key, out[key], (value, kind)))
        out[key] = (float(value), kind)
    return out


_CARDINAL = _build([
    ("صفر", 0, "unit"),
    ("واحد", 1, "unit"), ("واحدة", 1, "unit"),
    ("اثنان", 2, "unit"), ("اثنين", 2, "unit"), ("اثنتان", 2, "unit"),
    ("اثنتين", 2, "unit"), ("ثنين", 2, "unit"), ("ثنتين", 2, "unit"),
    ("ثلاثة", 3, "unit"), ("ثلاث", 3, "unit"),
    ("أربعة", 4, "unit"), ("أربع", 4, "unit"),
    ("خمسة", 5, "unit"), ("خمس", 5, "unit"),
    ("ستة", 6, "unit"), ("ست", 6, "unit"),
    ("سبعة", 7, "unit"), ("سبع", 7, "unit"),
    ("ثمانية", 8, "unit"), ("ثمان", 8, "unit"), ("ثماني", 8, "unit"),
    ("تسعة", 9, "unit"), ("تسع", 9, "unit"),
    ("عشرة", 10, "unit"), ("عشر", 10, "unit"),

    ("عشرون", 20, "ten"), ("عشرين", 20, "ten"),
    ("ثلاثون", 30, "ten"), ("ثلاثين", 30, "ten"),
    ("أربعون", 40, "ten"), ("أربعين", 40, "ten"),
    ("خمسون", 50, "ten"), ("خمسين", 50, "ten"),
    ("ستون", 60, "ten"), ("ستين", 60, "ten"),
    ("سبعون", 70, "ten"), ("سبعين", 70, "ten"),
    ("ثمانون", 80, "ten"), ("ثمانين", 80, "ten"),
    ("تسعون", 90, "ten"), ("تسعين", 90, "ten"),

    # bare hundred: multiplies a preceding unit when there is no waw
    ("مئة", 100, "scale100"), ("مائة", 100, "scale100"),
    ("مئتان", 200, "hundred"), ("مئتين", 200, "hundred"), ("مئتا", 200, "hundred"),
    ("مائتان", 200, "hundred"), ("مائتين", 200, "hundred"), ("مائتا", 200, "hundred"),
    ("ثلاثمئة", 300, "hundred"), ("ثلاثمائة", 300, "hundred"),
    ("أربعمئة", 400, "hundred"), ("أربعمائة", 400, "hundred"),
    ("خمسمئة", 500, "hundred"), ("خمسمائة", 500, "hundred"),
    ("ستمئة", 600, "hundred"), ("ستمائة", 600, "hundred"),
    ("سبعمئة", 700, "hundred"), ("سبعمائة", 700, "hundred"),
    ("ثمانمئة", 800, "hundred"), ("ثمانمائة", 800, "hundred"), ("ثمانيمئة", 800, "hundred"),
    ("تسعمئة", 900, "hundred"), ("تسعمائة", 900, "hundred"),

    # bare thousand: multiplies everything accumulated so far in the group
    ("ألف", 1000, "scale1000"), ("آلاف", 1000, "scale1000"), ("ألاف", 1000, "scale1000"),
    ("ألفان", 2000, "thousand"), ("ألفين", 2000, "thousand"), ("ألفا", 2000, "thousand"),
])

# First half of a teen bigram -> its units digit.
_TEEN_FIRST = _build([
    ("أحد", 1, "teenpart"), ("إحدى", 1, "teenpart"), ("حادي", 1, "teenpart"),
    ("اثنا", 2, "teenpart"), ("اثني", 2, "teenpart"),
    ("اثنتا", 2, "teenpart"), ("اثنتي", 2, "teenpart"),
    ("اثنان", 2, "teenpart"), ("اثنين", 2, "teenpart"), ("ثنين", 2, "teenpart"),
    ("ثلاثة", 3, "teenpart"), ("ثلاث", 3, "teenpart"),
    ("أربعة", 4, "teenpart"), ("أربع", 4, "teenpart"),
    ("خمسة", 5, "teenpart"), ("خمس", 5, "teenpart"),
    ("ستة", 6, "teenpart"), ("ست", 6, "teenpart"),
    ("سبعة", 7, "teenpart"), ("سبع", 7, "teenpart"),
    ("ثمانية", 8, "teenpart"), ("ثمان", 8, "teenpart"), ("ثماني", 8, "teenpart"),
    ("تسعة", 9, "teenpart"), ("تسع", 9, "teenpart"),
])

# Second half of a teen bigram.
_TEEN_SECOND = set(_norm(w) for w in ("عشر", "عشرة"))

# Feminine ordinals, clock use only.  Article included on purpose (see limit 3).
_ORDINAL = _build([
    ("الأولى", 1, "ord"), ("الواحدة", 1, "ord"), ("الحادية", 1, "ord"),
    ("الثانية", 2, "ord"),
    ("الثالثة", 3, "ord"),
    ("الرابعة", 4, "ord"),
    ("الخامسة", 5, "ord"),
    ("السادسة", 6, "ord"),
    ("السابعة", 7, "ord"),
    ("الثامنة", 8, "ord"),
    ("التاسعة", 9, "ord"),
    ("العاشرة", 10, "ord"),
])

_HOUR_ANCHOR = set(_norm(w) for w in ("الساعة", "ساعة", "الساعه", "ساعه"))

_WAW = "و"

# Fraction plurals: a number in front of one of these is a fraction, not a count.
_FRACTION_PLURAL = set(_norm(w) for w in ("أرباع", "أثلاث", "أخماس", "أسداس", "أعشار", "أنصاف"))

# Never a number, whatever the folding says.
_NEVER = set(_norm(w) for w in (
    "الاثنين", "الإثنين", "الأحد",      # weekdays
    "الحين", "الآن",                    # "now"
    "ثانية", "ثوان", "ثواني",           # "second" the time unit
    "الثاني", "الثانى", "ثاني",         # masculine ordinal
    "عشرات",                            # "dozens"
))


# --------------------------------------------------------------------------
# lexeme lookup
# --------------------------------------------------------------------------

def _lookup(tok: _Tok):
    """Return (value, kind, had_waw) for one token, or None."""
    key = tok.norm
    if key in _NEVER:
        return None
    if tok.num is not None:
        v = tok.num
        kind = "unit" if (float(v).is_integer() and 1 <= v <= 10) else "ten"
        return (v, kind, False)
    ent = _CARDINAL.get(key)
    if ent is not None:
        return (ent[0], ent[1], False)
    # conjunction waw, stripped only when the remainder is itself a number word
    if len(key) > 1 and key[0] == _WAW:
        rest = key[1:]
        if rest not in _NEVER:
            ent = _CARDINAL.get(rest)
            if ent is not None:
                return (ent[0], ent[1], True)
    return None


def _teen_at(toks: List[_Tok], i: int):
    """Teen bigram at token i -> (value, next_index, had_waw) or None."""
    if i + 1 >= len(toks):
        return None
    key = toks[i].norm
    had_waw = False
    if key in _NEVER:
        return None
    if key not in _TEEN_FIRST and len(key) > 1 and key[0] == _WAW and key[1:] in _TEEN_FIRST:
        key = key[1:]
        had_waw = True
    if key not in _TEEN_FIRST:
        return None
    if toks[i + 1].norm not in _TEEN_SECOND:
        return None
    return (10.0 + _TEEN_FIRST[key][0], i + 2, had_waw)


def _ordinal_at(toks: List[_Tok], i: int):
    """Feminine ordinal (optionally a teen ordinal) at i -> (value, next_index)."""
    ent = _ORDINAL.get(toks[i].norm)
    if ent is None:
        return None
    base = ent[0]
    if i + 1 < len(toks) and toks[i + 1].norm in _TEEN_SECOND and base <= 9:
        return (10.0 + base, i + 2)
    return (base, i + 1)


# --------------------------------------------------------------------------
# folding a sequence of parts into one value
# --------------------------------------------------------------------------

def _fold_items(items) -> float:
    total = 0.0
    group = 0.0
    pending = None  # a unit 1-10 that may still turn out to be a multiplier
    for value, kind, had_waw in items:
        if kind == "scale1000":
            if pending is not None:
                group += pending
                pending = None
            total += (group if group else 1.0) * 1000.0
            group = 0.0
        elif kind == "scale100":
            if pending is not None and not had_waw:
                group += pending * 100.0
                pending = None
            else:
                if pending is not None:
                    group += pending
                    pending = None
                group += 100.0
        else:
            if pending is not None:
                group += pending
                pending = None
            if kind == "unit" and 1 <= value <= 10:
                pending = value
            else:
                group += value
    if pending is not None:
        group += pending
    return total + group


# --------------------------------------------------------------------------
# public types
# --------------------------------------------------------------------------

class NumberMatch(object):
    """One number found in the text.  ``start``/``end`` index the ORIGINAL string."""

    __slots__ = ("value", "surface", "start", "end")

    def __init__(self, value, surface, start, end):
        self.value = float(value)
        self.surface = surface
        self.start = int(start)
        self.end = int(end)

    def __repr__(self):
        return "NumberMatch(value=%r, surface=%r, start=%d, end=%d)" % (
            self.value, self.surface, self.start, self.end)

    def __eq__(self, other):
        return (isinstance(other, NumberMatch)
                and self.value == other.value
                and self.surface == other.surface
                and self.start == other.start
                and self.end == other.end)

    def __hash__(self):
        return hash((self.value, self.surface, self.start, self.end))


class ParsedTime(object):
    """A clock time.  ``hour`` is 0-23 only when ``explicit_period`` is True;
    otherwise it is exactly what the caller said (1-12) and the half of the day
    is genuinely unknown."""

    __slots__ = ("hour", "minute", "surface", "explicit_period")

    def __init__(self, hour, minute, surface, explicit_period):
        self.hour = int(hour)
        self.minute = int(minute)
        self.surface = surface
        self.explicit_period = bool(explicit_period)

    def __repr__(self):
        return "ParsedTime(hour=%d, minute=%d, surface=%r, explicit_period=%r)" % (
            self.hour, self.minute, self.surface, self.explicit_period)

    def __eq__(self, other):
        return (isinstance(other, ParsedTime)
                and self.hour == other.hour
                and self.minute == other.minute
                and self.surface == other.surface
                and self.explicit_period == other.explicit_period)

    def __hash__(self):
        return hash((self.hour, self.minute, self.surface, self.explicit_period))


# --------------------------------------------------------------------------
# parse_numbers
# --------------------------------------------------------------------------

_STARTER_KINDS = ("unit", "ten", "hundred", "thousand", "scale100", "scale1000")


def _phrase_at(toks: List[_Tok], i: int):
    """Longest number phrase starting at token i.

    Returns (value, start_char, end_char, next_token_index) or None.

    Continuation rule: after the first part, the phrase only continues on an
    explicit waw (``وأربعين``) or on a bare scale word (``مئة`` / ``ألف``).  Two
    adjacent bare numerals never merge -- that is what keeps a spoken digit run
    like ``خمسة صفر اثنين`` three separate numbers instead of one wrong one.
    """
    n = len(toks)
    items = []
    j = i
    start_char = None

    while j < n:
        first = not items
        teen = _teen_at(toks, j)
        if teen is not None:
            value, nxt, had_waw = teen
            kind = "ten"  # a teen is self-contained, never a multiplier
        else:
            got = _lookup(toks[j])
            if got is None:
                break
            value, kind, had_waw = got
            nxt = j + 1

        if first:
            if kind not in _STARTER_KINDS:
                break
        else:
            if not (had_waw or kind in ("scale100", "scale1000")):
                break

        if first:
            start_char = toks[j].start + (1 if had_waw else 0)
        items.append((value, kind, had_waw))
        j = nxt

    if not items:
        return None

    # a number directly in front of a fraction plural is a fraction, not a count
    if j < n and toks[j].norm in _FRACTION_PLURAL:
        return None

    end_char = toks[j - 1].end
    return (_fold_items(items), start_char, end_char, j)


def parse_numbers(text: str) -> List[NumberMatch]:
    """Every number in ``text``, left to right by start index.

    Cardinals, teens, waw compounds, hundreds/thousands and all three digit
    families.  Bare ordinals are excluded (see limit 4) except immediately after
    ``الساعة``, where ``التاسعة`` unambiguously means nine o'clock.
    """
    if not text:
        return []
    toks = _tokenize(text)
    out = []
    i = 0
    n = len(toks)
    while i < n:
        # ordinal, but only under a clock anchor
        if i > 0 and toks[i - 1].norm in _HOUR_ANCHOR:
            ordn = _ordinal_at(toks, i)
            if ordn is not None:
                value, nxt = ordn
                out.append(NumberMatch(value, text[toks[i].start:toks[nxt - 1].end],
                                       toks[i].start, toks[nxt - 1].end))
                i = nxt
                continue
        got = _phrase_at(toks, i)
        if got is None:
            i += 1
            continue
        value, s, e, nxt = got
        out.append(NumberMatch(value, text[s:e], s, e))
        i = nxt
    return out


# --------------------------------------------------------------------------
# normalize_digits
# --------------------------------------------------------------------------

def _fmt(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return ("%f" % value).rstrip("0").rstrip(".")


def normalize_digits(text: str) -> str:
    """Same text with every number-word span replaced by Western digits.

    Splices only -- the output contains no character this function invented
    beyond ASCII digits, so it can never introduce a presentation form or a bidi
    control into Arabic that did not already have one.  Replacements are applied
    last-first so an earlier edit cannot shift a later offset.
    """
    if not text:
        return text
    matches = parse_numbers(text)
    out = text
    for m in reversed(matches):
        out = out[:m.start] + _fmt(m.value) + out[m.end:]
    return out


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------

_MOD_FIXED = {}
for _w, _d in (
    ("ونصف", 30), ("والنصف", 30), ("النصف", 30), ("نصف", 30),
    ("ونص", 30), ("والنص", 30), ("النص", 30), ("نص", 30),
    ("وربع", 15), ("والربع", 15), ("الربع", 15), ("ربع", 15),
    ("وثلث", 20), ("والثلث", 20), ("الثلث", 20), ("ثلث", 20),
):
    _MOD_FIXED[_norm(_w)] = _d

_ILLA = set(_norm(w) for w in ("إلا", "الا", "إلاّ"))
_ILLA_FIXED = {}
for _w, _d in (("ربع", 15), ("الربع", 15), ("ربعا", 15),
               ("ثلث", 20), ("الثلث", 20), ("ثلثا", 20),
               ("نصف", 30), ("النصف", 30), ("نصفا", 30)):
    _ILLA_FIXED[_norm(_w)] = _d

_MINUTE_UNIT = set(_norm(w) for w in ("دقيقة", "دقائق", "دقيقه", "دقيقتان", "دقيقتين"))

# period word -> half-of-day class
_PERIOD = {}
for _w, _k in (
    ("صباحاً", "am"), ("صباحا", "am"), ("صباح", "am"), ("الصباح", "am"), ("الصبح", "am"),
    ("فجراً", "am"), ("فجرا", "am"), ("الفجر", "am"),
    ("ظهراً", "noon"), ("ظهرا", "noon"), ("الظهر", "noon"), ("الظهيرة", "noon"),
    ("عصراً", "pm"), ("عصرا", "pm"), ("العصر", "pm"),
    ("مساءً", "pm"), ("مساء", "pm"), ("المساء", "pm"), ("مساءا", "pm"),
    ("ليلاً", "night"), ("ليلا", "night"), ("الليل", "night"), ("بالليل", "night"),
):
    _PERIOD[_norm(_w)] = _k

# multi-token period phrases, checked before single tokens
_PERIOD_PHRASES = [
    ([_norm("بعد"), _norm("الظهر")], "pm"),
    ([_norm("بعد"), _norm("الظهيرة")], "pm"),
    ([_norm("منتصف"), _norm("الليل")], "midnight"),
    ([_norm("في"), _norm("المساء")], "pm"),
    ([_norm("في"), _norm("الصباح")], "am"),
]


def _simple_hour_at(toks: List[_Tok], j: int, allow_ordinal: bool):
    """One hour lexeme at token j -> (value, next_index) or None.

    Deliberately does NOT take waw compounds: in a clock phrase
    ``الساعة ثلاثة وعشرين دقيقة`` the ``وعشرين`` is twenty *minutes*, not part of
    twenty-three.  parse_numbers keeps the compound behaviour; only the clock
    reader drops it.
    """
    if j >= len(toks):
        return None
    if allow_ordinal:
        ordn = _ordinal_at(toks, j)
        if ordn is not None:
            return ordn
    teen = _teen_at(toks, j)
    if teen is not None and not teen[2]:
        return (teen[0], teen[1])
    got = _lookup(toks[j])
    if got is None or got[2]:
        return None
    value, kind, _ = got
    if kind not in ("unit", "ten"):
        return None
    if not float(value).is_integer() or not (0 <= value <= 23):
        return None
    return (value, j + 1)


def _read_minutes(toks: List[_Tok], k: int) -> Tuple[int, int]:
    """Clock modifiers from token k.  Returns (delta_minutes, next_index)."""
    n = len(toks)
    delta = 0
    while k < n:
        key = toks[k].norm
        if key in _MOD_FIXED:
            delta += _MOD_FIXED[key]
            k += 1
            continue
        if key in _ILLA and k + 1 < n:
            nxt = toks[k + 1].norm
            if nxt in _ILLA_FIXED:
                delta -= _ILLA_FIXED[nxt]
                k += 2
                continue
            got = _simple_hour_at(toks, k + 1, allow_ordinal=False)
            if got is not None and 1 <= got[0] <= 59:
                j2 = got[1]
                if j2 < n and toks[j2].norm in _MINUTE_UNIT:
                    j2 += 1
                delta -= int(got[0])
                k = j2
                continue
            break
        # waw + number, optionally followed by an explicit minute unit
        if len(key) > 1 and key[0] == _WAW:
            stripped = _Tok(toks[k].raw[1:], toks[k].start + 1, toks[k].end)
            probe = [stripped] + toks[k + 1:]
            got = _simple_hour_at(probe, 0, allow_ordinal=False)
            if got is not None and 1 <= got[0] <= 59:
                consumed = got[1]
                j2 = k + consumed
                if j2 < n and toks[j2].norm in _MINUTE_UNIT:
                    j2 += 1
                delta += int(got[0])
                k = j2
                continue
        # bare number followed by an explicit minute unit
        got = _simple_hour_at(toks, k, allow_ordinal=False)
        if got is not None and got[1] < n and toks[got[1]].norm in _MINUTE_UNIT and 1 <= got[0] <= 59:
            delta += int(got[0])
            k = got[1] + 1
            continue
        break
    return delta, k


def _read_period(toks: List[_Tok], k: int):
    """Period word at or just after token k -> (kind, next_index) or None.

    Allows one filler token (``في``، ``من``) between the time and the period word.
    """
    n = len(toks)
    fillers = set(_norm(w) for w in ("في", "من", "عند", "بعد"))
    for skip in (0, 1):
        j = k + skip
        if j >= n:
            break
        if skip == 1 and toks[k].norm not in fillers:
            break
        for phrase, kind in _PERIOD_PHRASES:
            if j + len(phrase) <= n and [t.norm for t in toks[j:j + len(phrase)]] == phrase:
                return (kind, j + len(phrase))
        if toks[j].norm in _PERIOD:
            return (_PERIOD[toks[j].norm], j + 1)
    return None


def _to_24h(hour: int, kind: str) -> int:
    if kind == "am":
        return 0 if hour == 12 else hour
    if kind == "pm":
        return hour + 12 if hour < 12 else hour
    if kind == "noon":
        return 12 if hour == 12 else (hour + 12 if hour < 12 else hour)
    if kind == "night":
        if hour == 12:
            return 0
        if 1 <= hour <= 4:
            return hour
        return hour + 12 if hour < 12 else hour
    if kind == "midnight":
        return 0
    return hour


def _build_time(text, toks, hour_start_tok, hour_value, after_hour, anchor_tok):
    n = len(toks)
    minutes, k = _read_minutes(toks, after_hour)
    hour = int(hour_value)
    if minutes < 0:
        minutes += 60
        hour -= 1
        if hour < 0:
            hour += 24
    if not (0 <= minutes <= 59):
        # a modifier that cannot be a minute is dropped rather than guessed at
        minutes, k = 0, after_hour
    explicit = False
    per = _read_period(toks, k)
    if per is not None:
        kind, k = per
        hour = _to_24h(hour, kind)
        explicit = True
    if not (0 <= hour <= 23):
        return None
    start_tok = anchor_tok if anchor_tok is not None else hour_start_tok
    surface = text[toks[start_tok].start:toks[k - 1].end]
    return ParsedTime(hour, minutes, surface, explicit)


def parse_time(text: str) -> Optional[ParsedTime]:
    """First clock time in ``text``, or None.

    Recognises ``الساعة`` + cardinal or ordinal, and a bare number carrying a
    period word (``تسعة صباحاً``).  With no period word the hour is left exactly
    as spoken and ``explicit_period`` is False -- AM/PM is never guessed.
    """
    if not text:
        return None
    toks = _tokenize(text)
    n = len(toks)

    # 1. explicit الساعة anchor
    for i in range(n):
        if toks[i].norm in _HOUR_ANCHOR:
            got = _simple_hour_at(toks, i + 1, allow_ordinal=True)
            if got is None:
                continue
            res = _build_time(text, toks, i + 1, got[0], got[1], i)
            if res is not None:
                return res

    # 2. bare number immediately carrying a period word
    for i in range(n):
        got = _simple_hour_at(toks, i, allow_ordinal=False)
        if got is None or not (0 <= got[0] <= 23):
            continue
        minutes, k = _read_minutes(toks, got[1])
        if _read_period(toks, k) is None:
            continue
        res = _build_time(text, toks, i, got[0], got[1], None)
        if res is not None:
            return res
    return None


# --------------------------------------------------------------------------
# quantity
# --------------------------------------------------------------------------

def parse_quantity(text: str) -> Optional[float]:
    """First standalone quantity in ``text``, or None.

    "Standalone" means: not inside a clock expression.  ``الساعة تسعة صباحاً``
    has no quantity in it (returns None); ``نحتاج ثلاث ساعات`` returns 3.0.
    """
    if not text:
        return None
    matches = parse_numbers(text)
    if not matches:
        return None
    t = parse_time(text)
    if t is not None and t.surface:
        lo = text.find(t.surface)
        if lo >= 0:
            hi = lo + len(t.surface)
            matches = [m for m in matches if m.end <= lo or m.start >= hi]
    if not matches:
        return None
    return matches[0].value


# --------------------------------------------------------------------------
# import-time sanity: the lexicon itself must be clean Arabic
# --------------------------------------------------------------------------

def _audit_lexicon():
    bad = []
    for table in (_CARDINAL, _TEEN_FIRST, _ORDINAL, _MOD_FIXED, _ILLA_FIXED, _PERIOD):
        for key in table:
            if _FORBIDDEN_RE.search(key):
                bad.append(key)
    for s in (_TEEN_SECOND, _HOUR_ANCHOR, _NEVER, _MINUTE_UNIT, _ILLA, _FRACTION_PLURAL):
        for key in s:
            if _FORBIDDEN_RE.search(key):
                bad.append(key)
    if bad:
        raise AssertionError("lexicon holds presentation forms or bidi controls: %r" % bad)


_audit_lexicon()


# --------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import json
    import os
    import sys

    if len(sys.argv) > 1:
        samples = sys.argv[1:]
    else:
        # Read the real captured transcript rather than re-typing it.
        samples = []
        fx = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "fixtures", "v3_session_arabic.jsonl")
        if os.path.exists(fx):
            with open(fx, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    msg = json.loads(line).get("message", {})
                    if msg.get("transcript"):
                        samples.append(msg["transcript"])
        if not samples:
            print("no fixture found; pass text on the command line")
            raise SystemExit(1)

    for s in samples:
        print(s)
        print("  codepoints of surface words that parsed:")
        for m in parse_numbers(s):
            print("    %-10r -> %s   [%s]" % (
                m.surface, _fmt(m.value),
                " ".join("U+%04X" % ord(c) for c in m.surface)))
        print("  digits  :", normalize_digits(s))
        print("  time    :", parse_time(s))
        print("  quantity:", parse_quantity(s))
        print()
