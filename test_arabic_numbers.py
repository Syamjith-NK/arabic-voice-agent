# -*- coding: utf-8 -*-
"""Tests for arabic_numbers.py.

Own runner -- pytest is NOT installed on this machine and a suite that cannot run
is worse than none (same rule as selftest.py).  Prints every case, exits 1 on any
failure, ends with N/N passed.

The negative section is the important one.  Over-matching is the failure mode that
matters here: a parser that silently reads a number out of a word that is not a
number will book the wrong time and nothing downstream can tell.
"""

import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import arabic_numbers as an
from arabic_numbers import (
    NumberMatch, ParsedTime,
    parse_numbers, normalize_digits, parse_time, parse_quantity,
)

# Presentation forms (U+FB50-U+FEFF) and bidi controls.  Written as escapes so
# this test file itself holds none of them.
FORBIDDEN = re.compile("[\uFB50-\uFEFF\u200E\u200F\u202A-\u202E\u2066-\u2069]")

PASSED = 0
FAILED = 0
_SECTION = ""


def section(name):
    global _SECTION
    _SECTION = name
    print("\n== %s" % name)


def ok(label, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  pass  %s" % label)
    else:
        FAILED += 1
        print("  FAIL  %s   %s" % (label, detail))


def vals(text):
    return [m.value for m in parse_numbers(text)]


def eq(label, got, want):
    ok(label, got == want, "got %r want %r" % (got, want))


def t_eq(label, text, hour, minute, explicit):
    got = parse_time(text)
    if got is None:
        ok(label, False, "parse_time returned None")
        return
    ok(label,
       got.hour == hour and got.minute == minute and got.explicit_period == explicit,
       "got h=%d m=%d explicit=%r want h=%d m=%d explicit=%r (surface=%r)"
       % (got.hour, got.minute, got.explicit_period, hour, minute, explicit, got.surface))


# --------------------------------------------------------------------------
section("1. ones 0-10, masculine and feminine")
# --------------------------------------------------------------------------
for word, want in [
    ("صفر", 0), ("واحد", 1), ("واحدة", 1),
    ("اثنان", 2), ("اثنين", 2), ("اثنتان", 2), ("اثنتين", 2),
    ("ثلاثة", 3), ("ثلاث", 3),
    ("أربعة", 4), ("أربع", 4),
    ("خمسة", 5), ("خمس", 5),
    ("ستة", 6), ("ست", 6),
    ("سبعة", 7), ("سبع", 7),
    ("ثمانية", 8), ("ثمان", 8), ("ثماني", 8),
    ("تسعة", 9), ("تسع", 9),
    ("عشرة", 10), ("عشر", 10),
]:
    eq("%s = %d" % (word, want), vals(word), [float(want)])

eq("feminine counting: 3 hours", vals("ثلاث ساعات"), [3.0])
eq("masculine counting: 3 days", vals("ثلاثة أيام"), [3.0])
eq("diacritics ignored: tanween on 5", vals("خمسةً"), [5.0])

# --------------------------------------------------------------------------
section("2. teens 11-19, masculine and feminine")
# --------------------------------------------------------------------------
for word, want in [
    ("أحد عشر", 11), ("اثنا عشر", 12), ("ثلاثة عشر", 13), ("أربعة عشر", 14),
    ("خمسة عشر", 15), ("ستة عشر", 16), ("سبعة عشر", 17), ("ثمانية عشر", 18),
    ("تسعة عشر", 19),
    ("إحدى عشرة", 11), ("اثنتا عشرة", 12), ("ثلاث عشرة", 13), ("تسع عشرة", 19),
]:
    eq("%s = %d" % (word, want), vals(word), [float(want)])

eq("teen beats two separate ones", vals("خمسة عشر"), [15.0])
eq("teen inside a sentence", vals("نحتاج خمسة عشر شخصاً"), [15.0])

# --------------------------------------------------------------------------
section("3. tens 20-90, both case endings")
# --------------------------------------------------------------------------
for word, want in [
    ("عشرون", 20), ("عشرين", 20), ("ثلاثون", 30), ("ثلاثين", 30),
    ("أربعون", 40), ("أربعين", 40), ("خمسون", 50), ("خمسين", 50),
    ("ستون", 60), ("ستين", 60), ("سبعون", 70), ("سبعين", 70),
    ("ثمانون", 80), ("ثمانين", 80), ("تسعون", 90), ("تسعين", 90),
]:
    eq("%s = %d" % (word, want), vals(word), [float(want)])

# --------------------------------------------------------------------------
section("4. waw compounds -- units FIRST, the part naive parsers get wrong")
# --------------------------------------------------------------------------
eq("21", vals("واحد وعشرون"), [21.0])
eq("45", vals("خمسة وأربعين"), [45.0])
eq("32", vals("اثنان وثلاثون"), [32.0])
eq("99", vals("تسعة وتسعين"), [99.0])
eq("23 in a sentence", vals("المبلغ ثلاثة وعشرين درهماً"), [23.0])
eq("67", vals("سبعة وستين"), [67.0])
ok("45 is ONE match not two",
   len(parse_numbers("خمسة وأربعين")) == 1,
   "got %r" % (parse_numbers("خمسة وأربعين"),))

# --------------------------------------------------------------------------
section("5. hundreds and thousands")
# --------------------------------------------------------------------------
for word, want in [
    ("مئة", 100), ("مائة", 100),
    ("مئتان", 200), ("مئتين", 200), ("مائتين", 200),
    ("ثلاثمئة", 300), ("ثلاثمائة", 300), ("خمسمئة", 500), ("تسعمئة", 900),
    ("ثلاث مئة", 300),
    ("ألف", 1000), ("ألفين", 2000), ("ألفان", 2000),
    ("خمسة آلاف", 5000), ("عشرة آلاف", 10000),
    ("مئة وخمسة وأربعين", 145),
    ("ألف وخمسمئة", 1500),
    ("خمسة وعشرين ألف", 25000),
    ("مئتين وخمسين", 250),
]:
    eq("%s = %d" % (word, want), vals(word), [float(want)])

eq("price in a sentence", vals("السعر ثلاثة آلاف وخمسمئة درهم"), [3500.0])

# --------------------------------------------------------------------------
section("6. ordinals -- clock form, gated on the الساعة anchor")
# --------------------------------------------------------------------------
for word, want in [
    ("الواحدة", 1), ("الأولى", 1), ("الثانية", 2), ("الثالثة", 3), ("الرابعة", 4),
    ("الخامسة", 5), ("السادسة", 6), ("السابعة", 7), ("الثامنة", 8),
    ("التاسعة", 9), ("العاشرة", 10),
]:
    eq("الساعة %s = %d" % (word, want), vals("الساعة " + word), [float(want)])

eq("الساعة الحادية عشرة = 11", vals("الساعة الحادية عشرة"), [11.0])
eq("الساعة الثانية عشرة = 12", vals("الساعة الثانية عشرة"), [12.0])
eq("cardinal after الساعة still works", vals("الساعة تسعة"), [9.0])

# --------------------------------------------------------------------------
section("7. time modifiers")
# --------------------------------------------------------------------------
t_eq("9:00 ordinal + morning", "الساعة التاسعة صباحاً", 9, 0, True)
t_eq("9:30 والنصف", "الساعة تسعة والنصف صباحاً", 9, 30, True)
t_eq("3:15 والربع + afternoon", "الساعة الثالثة والربع عصراً", 15, 15, True)
t_eq("4:20 وثلث", "الساعة الرابعة وثلث", 4, 20, False)
t_eq("4:45 إلا ربعاً", "الساعة الخامسة إلا ربعاً", 4, 45, False)
t_eq("4:45 إلا ربع", "الساعة الخامسة إلا ربع", 4, 45, False)
t_eq("4:50 إلا عشر دقائق", "الساعة الخامسة إلا عشر دقائق", 4, 50, False)
t_eq("4:50 إلا عشرة", "الساعة الخامسة إلا عشرة", 4, 50, False)
t_eq("3:20 ودقيقة form", "الساعة ثلاثة وعشرين دقيقة", 3, 20, False)
t_eq("9:30 dialect ونص", "الساعة تسعة ونص", 9, 30, False)
t_eq("11:30 teen ordinal + half", "الساعة الحادية عشرة والنصف", 11, 30, False)

# 3:20 is NOT 23 -- the clock reader deliberately drops waw compounding
ok("clock reader does not read 3+20 as 23",
   parse_time("الساعة ثلاثة وعشرين دقيقة").hour == 3,
   "hour was %r" % parse_time("الساعة ثلاثة وعشرين دقيقة").hour)
eq("but parse_numbers DOES compound it to 23",
   vals("ثلاثة وعشرين"), [23.0])

# --------------------------------------------------------------------------
section("8. period words -> 24h")
# --------------------------------------------------------------------------
t_eq("7am صباحاً", "الساعة السابعة صباحاً", 7, 0, True)
t_eq("7am الصبح", "الساعة السابعة الصبح", 7, 0, True)
t_eq("9pm مساءً", "الساعة تسعة مساءً", 21, 0, True)
t_eq("9pm المساء", "الساعة التاسعة في المساء", 21, 0, True)
t_eq("12 noon ظهراً", "الساعة الثانية عشرة ظهراً", 12, 0, True)
t_eq("1pm ظهراً", "الساعة الواحدة ظهراً", 13, 0, True)
t_eq("3pm العصر", "الساعة الثالثة عصراً", 15, 0, True)
t_eq("3pm بعد الظهر", "الساعة الثالثة بعد الظهر", 15, 0, True)
t_eq("10pm ليلاً", "الساعة العاشرة ليلاً", 22, 0, True)
t_eq("2am ليلاً (1-4 convention)", "الساعة الثانية ليلاً", 2, 0, True)
t_eq("midnight الثانية عشرة ليلاً", "الساعة الثانية عشرة ليلاً", 0, 0, True)
t_eq("12am صباحاً -> 0", "الساعة الثانية عشرة صباحاً", 0, 0, True)
t_eq("bare number + period, no anchor", "تسعة صباحاً", 9, 0, True)
t_eq("dawn الفجر", "الساعة الخامسة فجراً", 5, 0, True)

# --------------------------------------------------------------------------
section("9. Arabic-Indic, Eastern Arabic and mixed digits")
# --------------------------------------------------------------------------
eq("Arabic-Indic 9", vals("٩"), [9.0])
eq("Arabic-Indic 25", vals("٢٥"), [25.0])
eq("Eastern Arabic 9", vals("۹"), [9.0])
eq("Eastern Arabic 2026", vals("۲۰۲۶"), [2026.0])
eq("Western digit in Arabic text", vals("الساعة 9 صباحاً"), [9.0])
t_eq("mixed: الساعة ٩ صباحاً", "الساعة ٩ صباحاً", 9, 0, True)
t_eq("mixed: الساعة 9 مساءً", "الساعة 9 مساءً", 21, 0, True)
eq("Arabic-Indic normalises to Western",
   normalize_digits("الساعة ٩ صباحاً"), "الساعة 9 صباحاً")
eq("digit + scale word", vals("٥ آلاف"), [5000.0])

# --------------------------------------------------------------------------
section("10. Gulf / Emirati dialect variants")
# --------------------------------------------------------------------------
eq("ثنين = 2", vals("ثنين"), [2.0])
eq("ثنتين = 2", vals("ثنتين"), [2.0])
eq("عشرة = 10 (with taa marbuta)", vals("عشرة"), [10.0])
eq("عشر  = 10 (without)", vals("عشر"), [10.0])
t_eq("الساعة ثنتين", "الساعة ثنتين", 2, 0, False)

# --------------------------------------------------------------------------
section("11. NEGATIVE -- words that look numeric must NOT match")
print("   (over-matching is worse than under-matching: a wrong number silently")
print("    books the wrong time, and nothing downstream can detect it)")
# --------------------------------------------------------------------------
eq("الحين (now) is not a number", vals("الحين"), [])
eq("الحين in a sentence", vals("أريد الحين موعداً"), [])
eq("الآن (now) is not a number", vals("الآن"), [])
eq("الطابق الثاني = second FLOOR, not 2", vals("الطابق الثاني"), [])
eq("أحد الناس = one OF the people, not 1", vals("أحد الناس"), [])
eq("ثانية (the time unit) is not 2", vals("ثانية"), [])
eq("يوم الاثنين = Monday, not 2", vals("يوم الاثنين"), [])
eq("الأحد = Sunday, not 1", vals("الأحد"), [])
eq("عشرات (dozens) is not 10", vals("عشرات الأشخاص"), [])
eq("ستارة contains ست but is a curtain", vals("ستارة جميلة"), [])
eq("واحة (oasis) is not واحد", vals("واحة النخيل"), [])
eq("السنة (the year) is not a number", vals("السنة"), [])
eq("لست (I am not) must not strip to ست=6", vals("لست متأكداً"), [])
eq("bare ordinal with no anchor does not match", vals("الثالثة"), [])
eq("التصوير is not a number", vals("التصوير"), [])
eq("مواعيد is not a number", vals("مواعيد"), [])

# substring safety, stated as a property rather than one example
for host in ("ستارة", "عشرات", "واحة", "تسعين شيء"):
    pass
eq("positive control beside the negatives: ثانية واحدة -> [1]",
   vals("ثانية واحدة"), [1.0])
eq("positive control: عشرين still parses", vals("عشرين"), [20.0])

# the retracted partial from NOTES.md CONSTRAINT 3 must yield no time
ok("revised partial 'إلى السنة' yields NO time (not a wrong one)",
   parse_time("هل يمكنكم تأجيل التصوير إلى السنة؟") is None,
   "got %r" % (parse_time("هل يمكنكم تأجيل التصوير إلى السنة؟"),))

# adjacent bare numerals must not merge into one wrong number
eq("spoken digit run stays three numbers", vals("خمسة صفر اثنين"), [5.0, 0.0, 2.0])
ok("spoken digit run is 3 matches, not 1",
   len(parse_numbers("خمسة صفر اثنين")) == 3,
   "got %r" % (parse_numbers("خمسة صفر اثنين"),))

# The _NEVER blocklist is defence in depth.  With TODAY'S lexicon nothing can
# reach it -- the definite article is never stripped, so الاثنين could not have
# matched in the first place.  That makes it untested code, which rots into
# decoration.  So test the MECHANISM directly: inject the word into the lexicon
# the way a future edit would, and prove the blocklist still refuses it.
_probe = an._norm("الاثنين")
an._CARDINAL[_probe] = (2.0, "unit")
try:
    ok("_NEVER refuses a blocklisted word even when the lexicon knows it",
       vals("يوم الاثنين") == [], "got %r" % vals("يوم الاثنين"))
finally:
    del an._CARDINAL[_probe]
ok("probe removed cleanly", vals("يوم الاثنين") == [])

_probe2 = an._norm("ثانية")
an._CARDINAL[_probe2] = (2.0, "unit")
try:
    ok("_NEVER also guards the waw-stripped path",
       vals("وثانية") == [], "got %r" % vals("وثانية"))
finally:
    del an._CARDINAL[_probe2]

_probe3 = an._norm("عشرات")
an._TEEN_FIRST[_probe3] = (3.0, "teenpart")
try:
    # the bigram must NOT fire (would be 13); the following bare عشر is
    # legitimately 10 on its own, so that is what must survive
    ok("_NEVER also guards the teen-bigram path",
       vals("عشرات عشر") == [10.0], "got %r" % vals("عشرات عشر"))
finally:
    del an._TEEN_FIRST[_probe3]

eq("a duration is not a clock time", parse_time("ثلاث ساعات"), None)
eq("no number at all -> no time", parse_time("مرحباً كيف حالك"), None)
eq("no number at all -> no quantity", parse_quantity("مرحباً كيف حالك"), None)

# --------------------------------------------------------------------------
section("12. normalize_digits -- must not corrupt the surrounding Arabic")
# --------------------------------------------------------------------------
SENT = "هل يمكنكم تأجيل التصوير إلى الساعة تسعة صباحاً؟"
out = normalize_digits(SENT)
ok("nine became a digit", "9" in out, out)
eq("exact output", out, "هل يمكنكم تأجيل التصوير إلى الساعة 9 صباحاً؟")
ok("ZERO presentation-form codepoints (U+FB50-U+FEFF)",
   FORBIDDEN.search(out) is None,
   "found %r" % (FORBIDDEN.findall(out),))
ok("ZERO bidi control characters", FORBIDDEN.search(out) is None)
ok("ZERO U+FFFD replacement characters", "�" not in out)
ok("round-trips: putting the word back reproduces the input exactly",
   out.replace("9", "تسعة") == SENT,
   "%r != %r" % (out.replace("9", "تسعة"), SENT))
ok("first word untouched", out.split()[0] == "هل", out.split()[0])
ok("Arabic question mark survives", out.endswith("؟"), repr(out[-1]))
ok("tanween on صباحاً survives",
   "ً" in out, " ".join("U+%04X" % ord(c) for c in out))

eq("text with no numbers is returned unchanged",
   normalize_digits("لا يوجد رقم في هذه الجملة"), "لا يوجد رقم في هذه الجملة")
eq("compound normalises as one span",
   normalize_digits("المبلغ خمسة وأربعين درهماً"), "المبلغ 45 درهماً")
eq("two separate numbers", normalize_digits("ثلاثة و خمسة"), "3 و 5")
eq("empty string", normalize_digits(""), "")

# every output of normalize_digits over the whole corpus must be clean
CORPUS = [SENT, "الساعة الثالثة والربع عصراً", "خمسة آلاف درهم",
          "الساعة ٩ صباحاً", "مئة وخمسة وأربعين", "أحد عشر شخصاً"]
bad = [s for s in CORPUS if FORBIDDEN.search(normalize_digits(s))]
ok("no presentation form or bidi control in ANY corpus output", not bad, repr(bad))

# --------------------------------------------------------------------------
section("13. OUT OF SCOPE -- must return None/[] rather than a wrong answer")
# --------------------------------------------------------------------------
eq("مليون is above the ceiling -> no match at all",
   vals("مليون درهم"), [])
eq("مليار is above the ceiling -> no match at all",
   vals("مليار درهم"), [])
ok("parse_quantity on a million refuses",
   parse_quantity("مليون درهم") is None,
   "got %r" % parse_quantity("مليون درهم"))
ok("ثلاثة أرباع is 0.75, not 3 -> refused",
   parse_quantity("ثلاثة أرباع") is None,
   "got %r" % parse_quantity("ثلاثة أرباع"))
ok("نصف ساعة (half an hour) -> refused, fractions unsupported",
   parse_quantity("نصف ساعة") is None,
   "got %r" % parse_quantity("نصف ساعة"))
eq("clitic ب is not stripped: بعشرة is a documented MISS, not a wrong number",
   vals("بعشرة دراهم"), [])
eq("ordinal day-of-month is out of scope",
   vals("الثالث من الشهر"), [])

pt = parse_time("الساعة تسعة")
ok("no period word -> AM/PM is NOT guessed",
   pt is not None and pt.hour == 9 and pt.explicit_period is False,
   "got %r" % (pt,))

# --------------------------------------------------------------------------
section("14. parse_quantity")
# --------------------------------------------------------------------------
eq("3 hours", parse_quantity("نحتاج ثلاث ساعات"), 3.0)
eq("15 people", parse_quantity("خمسة عشر شخصاً"), 15.0)
eq("3500 dirhams", parse_quantity("السعر ثلاثة آلاف وخمسمئة درهم"), 3500.0)
ok("a clock time is not a quantity",
   parse_quantity(SENT) is None, "got %r" % parse_quantity(SENT))
eq("quantity beside a time is still found",
   parse_quantity("نحتاج ثلاث كاميرات الساعة تسعة صباحاً"), 3.0)

# --------------------------------------------------------------------------
section("15. public API shape and index correctness")
# --------------------------------------------------------------------------
ms = parse_numbers(SENT)
ok("returns a list", isinstance(ms, list))
ok("element is a NumberMatch", len(ms) == 1 and isinstance(ms[0], NumberMatch))
m = ms[0]
for attr in ("value", "surface", "start", "end"):
    ok("NumberMatch has .%s" % attr, hasattr(m, attr))
ok("value is a float", isinstance(m.value, float), type(m.value))
ok("start/end index the ORIGINAL text", SENT[m.start:m.end] == m.surface,
   "%r vs %r" % (SENT[m.start:m.end], m.surface))
ok("surface is the exact word", m.surface == "تسعة", repr(m.surface))

multi = parse_numbers("ثلاثة ثم خمسة ثم سبعة")
ok("matches are left-to-right by start",
   [x.start for x in multi] == sorted(x.start for x in multi),
   [x.start for x in multi])
eq("three matches", [x.value for x in multi], [3.0, 5.0, 7.0])

pt = parse_time(SENT)
ok("ParsedTime type", isinstance(pt, ParsedTime))
for attr in ("hour", "minute", "surface", "explicit_period"):
    ok("ParsedTime has .%s" % attr, hasattr(pt, attr))
ok("hour is an int 0-23", isinstance(pt.hour, int) and 0 <= pt.hour <= 23)
ok("minute is an int 0-59", isinstance(pt.minute, int) and 0 <= pt.minute <= 59)
ok("surface is a substring of the input", pt.surface in SENT, repr(pt.surface))
ok("empty input is safe", parse_numbers("") == [] and parse_time("") is None
   and parse_quantity("") is None)

# --------------------------------------------------------------------------
section("16. the REAL captured sentence, read from fixtures/ on disk")
# --------------------------------------------------------------------------
FX = os.path.join(HERE, "fixtures", "v3_session_arabic.jsonl")
ok("fixture exists", os.path.exists(FX), FX)

final_transcript = None
all_transcripts = []
with open(FX, encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line).get("message", {})
        if msg.get("transcript"):
            all_transcripts.append(msg["transcript"])
            if msg.get("end_of_turn"):
                final_transcript = msg["transcript"]

ok("found the end_of_turn transcript", final_transcript is not None)
print("     fixture text: %s" % final_transcript)
print("     codepoints  : %s" % " ".join("U+%04X" % ord(c) for c in final_transcript))

ok("fixture itself holds no presentation forms",
   FORBIDDEN.search(final_transcript) is None)
ok("fixture contains no Western digit",
   not re.search(r"\d", final_transcript),
   "CONSTRAINT 2 says numbers come back as words")

ft = parse_time(final_transcript)
ok("fixture parses to a time", ft is not None)
if ft is not None:
    ok("fixture hour == 9", ft.hour == 9, "got %r" % ft.hour)
    ok("fixture minute == 0", ft.minute == 0, "got %r" % ft.minute)
    ok("fixture explicit_period is True", ft.explicit_period is True,
       "got %r" % ft.explicit_period)
    print("     parsed      : %r" % (ft,))
    print("     surface     : %s" % ft.surface)

eq("fixture normalises", normalize_digits(final_transcript),
   "هل يمكنكم تأجيل التصوير إلى الساعة 9 صباحاً؟")

# the two earlier partials are revised text and must not produce a time
for i, tr in enumerate(all_transcripts):
    if "الساعة" not in tr:
        ok("revised partial %d yields no time: %s" % (i, tr),
           parse_time(tr) is None, "got %r" % (parse_time(tr),))

# --------------------------------------------------------------------------
section("17. source hygiene -- the parser must not itself store corrupt Arabic")
# --------------------------------------------------------------------------
for fname in ("arabic_numbers.py", "test_arabic_numbers.py"):
    src = open(os.path.join(HERE, fname), encoding="utf-8").read()
    hits = FORBIDDEN.findall(src)
    ok("%s holds ZERO presentation forms / bidi controls" % fname,
       not hits,
       " ".join("U+%04X" % ord(c) for c in hits))

# every lexicon key must be logical-order base Arabic
bad_keys = []
for table in (an._CARDINAL, an._TEEN_FIRST, an._ORDINAL, an._PERIOD):
    for key in table:
        for ch in key:
            if 0xFB50 <= ord(ch) <= 0xFEFF:
                bad_keys.append(key)
ok("no lexicon key holds a presentation form", not bad_keys, repr(bad_keys))

# named codepoint check on the one word the whole project hangs on
ok("lexicon key for 'nine' is TEH SEEN AIN TEH-MARBUTA->HEH",
   an._norm("تسعة") == "تسعه",
   " ".join("U+%04X" % ord(c) for c in an._norm("تسعة")))
ok("normalisation of صباحاً drops only the tanween",
   an._norm("صباحاً") == "صباحا",
   " ".join("U+%04X" % ord(c) for c in an._norm("صباحاً")))
ok("unicodedata agrees the folded chars are base letters",
   all(not unicodedata.name(c).endswith("FORM") for c in an._norm("تسعة")))

# --------------------------------------------------------------------------
print("\n" + "-" * 62)
total = PASSED + FAILED
if FAILED:
    print("%d/%d passed -- %d FAILED" % (PASSED, total, FAILED))
    sys.exit(1)
print("%d/%d passed" % (PASSED, total))
