#!/usr/bin/env python3
"""
Build ARABIC_VOICE_AGENT.pdf, the ten slide deck for the AssemblyAI Voice Agent
Hackathon submission.

    python3 build_deck.py

Renderer choice, and why it is not arbitrary.

Arabic has to be SHAPED (letters take initial/medial/final forms and join) and
REORDERED (right to left) at render time. The wrong way to get that is to call
arabic_reshaper + python-bidi on the string before drawing it, which is the
exact bug this repo exists to document: on a renderer that already shapes, that
recipe reverses the text.

So: Pillow draws every page, because this build reports
`PIL.features.check("raqm") is True`, meaning ImageDraw.text() shapes and
reorders logical Arabic itself. Every Arabic string in this file is stored in
LOGICAL order, exactly as it came off the wire, and handed to Pillow untouched.
`assert_logical()` below refuses to run if any of them contains an Arabic
presentation-form codepoint, which is the signature of pre-shaping.

matplotlib is used only to assemble the finished rasters into the PDF, because
its PDF backend embeds images losslessly (Flate), where Pillow's own PDF writer
re-encodes RGB pages as JPEG and would soften the type.

Every figure and every string on these slides comes from SUBMISSION.md,
README.md or NOTES.md in the parent directory. Nothing is invented here.
"""

from __future__ import annotations

import os
import sys
import unicodedata

from PIL import Image, ImageDraw, ImageFont, features

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PDF = os.path.join(HERE, "ARABIC_VOICE_AGENT.pdf")
PAGES_DIR = os.path.join(HERE, "pages")
DEMO_SHOT = "/tmp/demo_mock.png"
LOCAL_SHOT = os.path.join(HERE, "demo_mock.png")

# ---------------------------------------------------------------- canvas ----

W, H = 2560, 1440          # 16:9
DPI = 192                  # -> 13.333 x 7.5 in page
ML = 168                   # left margin
MR = W - 168               # right edge of the text column
TOP = 132

# ---------------------------------------------------------------- colour ----
# Sampled from the demo screenshot so the deck and the product are one object.

BG =       (9, 11, 14)
PANEL =    (15, 19, 24)
PANEL_HI = (20, 25, 31)
RULE =     (34, 41, 49)
RULE_SOFT =(24, 30, 37)
INK =      (233, 238, 243)
INK_2 =    (150, 162, 174)
INK_3 =    (98, 110, 122)
ACCENT =   (79, 214, 184)
ACCENT_DIM=(38, 92, 82)
WARN =     (224, 168, 94)
DEAD =     (118, 84, 92)

# ----------------------------------------------------------------- fonts ----

SANS = "/System/Library/Fonts/SFNS.ttf"
MONO = "/System/Library/Fonts/SFNSMono.ttf"
ARAB = "/System/Library/Fonts/SFArabic.ttf"

_fc: dict = {}


def F(kind: str, size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    key = (kind, size, weight)
    if key not in _fc:
        path = {"sans": SANS, "mono": MONO, "ar": ARAB}[kind]
        f = ImageFont.truetype(path, size)
        f.set_variation_by_name(weight)
        _fc[key] = f
    return _fc[key]


# ------------------------------------------------------- Arabic integrity ----

def assert_logical(*strings: str) -> None:
    """Refuse to render anything that has already been shaped or reversed.

    An Arabic presentation form (U+FB50..U+FDFF, U+FE70..U+FEFF) in a source
    string means somebody ran arabic_reshaper over it. Pillow would then shape
    the already-shaped text and reverse it a second time.
    """
    for s in strings:
        for ch in s:
            name = unicodedata.name(ch, "")
            if name.endswith(("ISOLATED FORM", "INITIAL FORM",
                              "MEDIAL FORM", "FINAL FORM")):
                raise SystemExit(
                    "pre-shaped Arabic in a deck string: %r (%s). "
                    "Store the logical string." % (ch, name))
            if ch in "\u200e\u200f\u202a\u202b\u202c\u202d\u202e":
                raise SystemExit("bidi control character in a deck string: %r" % ch)


# ------------------------------------------------------------- primitives ----

def tw(d, s, font, rtl=False) -> float:
    if rtl:
        return d.textlength(s, font=font, direction="rtl", language="ar")
    return d.textlength(s, font=font, direction="ltr")


def tracked(d, xy, s, font, fill, track=6, anchor="la"):
    """Letter-spaced small caps label. Pillow has no tracking, so step it."""
    x, y = xy
    if anchor.startswith("r"):
        total = sum(d.textlength(c, font=font) + track for c in s) - track
        x -= total
    for c in s:
        d.text((x, y), c, font=font, fill=fill, anchor="l" + anchor[1],
               direction="ltr")
        x += d.textlength(c, font=font) + track
    return x


def wrap(d, s, font, width) -> list:
    words, lines, cur = s.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font, direction="ltr") <= width or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def para(d, xy, s, font, fill, width, lead, anchor="la"):
    x, y = xy
    for line in wrap(d, s, font, width):
        d.text((x, y), line, font=font, fill=fill, anchor=anchor, direction="ltr")
        y += lead
    return y


def rule(d, y, x0=ML, x1=MR, fill=RULE, h=2):
    d.rectangle([x0, y, x1, y + h - 1], fill=fill)


def panel(d, box, fill=PANEL, outline=RULE, r=14, w=2):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=w)


# ---------------------------------------------------------------- chrome ----

DECK_TITLE = "ARABIC REALTIME VOICE AGENT"


def new_slide(n: int, kicker: str, rail: str):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)

    # top hairline and running head
    tracked(d, (ML, TOP - 76), DECK_TITLE, F("mono", 22, "Medium"), INK_3, 5)
    tracked(d, (MR, TOP - 76), rail, F("mono", 22, "Medium"), INK_3, 5, anchor="ra")
    rule(d, TOP - 34, fill=RULE_SOFT)

    # slide number, bottom right
    d.text((MR, H - 78), "%02d" % n, font=F("mono", 30, "Medium"),
           fill=INK_3, anchor="rs", direction="ltr")
    d.text((ML, H - 78), "Syamjith NK", font=F("sans", 28),
           fill=INK_3, anchor="ls", direction="ltr")

    y = TOP + 30
    if kicker:
        tracked(d, (ML, y), kicker, F("mono", 24, "Semibold"), ACCENT, 6)
        y += 62
    return im, d, y


def headline(d, y, text, size=76, width=None, fill=INK, lead=None):
    f = F("sans", size, "Semibold")
    width = width or (MR - ML)
    lead = lead or int(size * 1.18)
    for line in wrap(d, text, f, width):
        d.text((ML, y), line, font=f, fill=fill, anchor="la", direction="ltr")
        y += lead
    return y


def deck(d, y, text, size=34, fill=INK_2, width=None, lead=None):
    return para(d, (ML, y), text, F("sans", size), fill,
                width or int((MR - ML) * 0.78), lead or int(size * 1.45))


# ================================================================ slides ====

def slide_01():
    im, d, y = new_slide(1, "THE FACT THAT DECIDES THE ARCHITECTURE",
                         "ASSEMBLYAI VOICE AGENT HACKATHON")
    y = headline(d, y,
                 "Arabic is a supported input language and not a "
                 "supported output voice.", 78)
    y += 24
    y = deck(d, y,
             "AssemblyAI's managed Voice Agent API, from their own docs, "
             "re-verified 2026-09-18.")

    # three number blocks
    top = y + 118
    cw = (MR - ML - 2 * 44) // 3
    stats = [
        ("18", "INPUT LANGUAGES", "Arabic among them", INK),
        ("16", "OUTPUT VOICES", "11 English, 5 European", INK),
        ("0", "ARABIC VOICES", "listed as coming soon", ACCENT),
    ]
    for i, (big, label, sub, col) in enumerate(stats):
        x = ML + i * (cw + 44)
        panel(d, [x, top, x + cw, top + 396])
        tracked(d, (x + 40, top + 48), label, F("mono", 22, "Medium"), INK_3, 5)
        d.text((x + 34, top + 118), big, font=F("sans", 178, "Bold"),
               fill=col, anchor="la", direction="ltr")
        d.text((x + 40, top + 330), sub, font=F("sans", 28), fill=INK_2,
               anchor="la", direction="ltr")

    y = top + 396 + 122
    d.rectangle([ML, y, ML + 6, y + 112], fill=ACCENT)
    para(d, (ML + 40, y - 4),
         "So a managed Arabic agent hears Arabic and answers in a European "
         "voice, and a judge who tests it hears exactly that.",
         F("sans", 40, "Medium"), INK, MR - ML - 60, 56)
    return im


def slide_02():
    im, d, y = new_slide(2, "CONSEQUENCE", "ARCHITECTURE")
    y = headline(d, y, "So this is not the managed agent.", 78)
    y += 18
    y = deck(d, y,
             "Transcription only from AssemblyAI, on the one model that takes "
             "Arabic. Everything after the transcript is ours, and every hop "
             "below was run against the live API.")

    top = y + 100
    box_h = 300
    labels = [
        ("CAPTURE", ["browser mic, 16 kHz PCM16", "or Twilio, 8 kHz mu-law",
                     "aggregated to 100 ms frames"], INK_2),
        ("ASSEMBLYAI v3", ["wss /v3/ws", "universal-3-5-pro",
                           "60 s token",
                           "no Authorization header"], ACCENT),
        ("PARSER", ["Arabic number words to values",
                    "clock, quantity, ordinals", "232 checks"], INK_2),
        ("AGENT + VOICE", ["deterministic booking rules",
                           "macOS Majed, ar_001",
                           "no vendor, no key, no bill"], INK_2),
    ]
    gap = 44
    cw = (MR - ML - 3 * gap) // 4
    for i, (name, lines, col) in enumerate(labels):
        x = ML + i * (cw + gap)
        panel(d, [x, top, x + cw, top + box_h],
              fill=PANEL_HI if col is ACCENT else PANEL,
              outline=ACCENT_DIM if col is ACCENT else RULE)
        tracked(d, (x + 34, top + 42), name, F("mono", 23, "Semibold"), col, 5)
        yy = top + 106
        for ln in lines:
            d.text((x + 34, yy), ln, font=F("mono", 24), fill=INK_2,
                   anchor="la", direction="ltr")
            yy += 44
        if i < 3:
            ax = x + cw + gap // 2
            ay = top + box_h // 2
            d.line([ax - 13, ay, ax + 11, ay], fill=INK_3, width=3)
            d.polygon([(ax + 14, ay), (ax + 2, ay - 8), (ax + 2, ay + 8)],
                      fill=INK_3)

    y = top + box_h + 96
    rule(d, y, fill=RULE_SOFT)
    y += 52
    cols = [
        ("WHAT THIS BUYS", "One socket carries a whole conversation: two "
         "utterances, turn_order 0 then 1, zero reconnects. The free tier caps "
         "new streaming connections at 5 per minute, so a socket-per-turn "
         "design rate-limits itself after five exchanges."),
        ("WHAT IT COSTS", "A browser cannot set an Authorization header on a "
         "WebSocket, so the token mint is public and cannot be made private. "
         "TTL and rate limits are speed bumps. The account spend cap is the "
         "only real bound."),
    ]
    half = (MR - ML - 90) // 2
    for i, (h, body) in enumerate(cols):
        x = ML + i * (half + 90)
        tracked(d, (x, y), h, F("mono", 22, "Semibold"), INK_3, 5)
        para(d, (x, y + 48), body, F("sans", 28), INK_2, half, 42)
    return im


def slide_03():
    im, d, y = new_slide(3, "FINDING 1", "MEASURED LIVE")
    y = headline(d, y, "Numbers come back as words, not digits.", 78)
    y += 18
    y = deck(d, y, "The audio said nine. The transcript says this.")

    top = y + 100
    # the word, huge, beside the digit it should have been
    panel(d, [ML, top, MR, top + 402])
    tracked(d, (ML + 48, top + 46), "WHAT THE SERVICE RETURNS",
            F("mono", 22, "Medium"), INK_3, 5)

    ar = "\u062a\u0633\u0639\u0629"          # tis'a
    assert_logical(ar)
    fa = F("ar", 190, "Semibold")
    d.text((ML + 48, top + 238), ar, font=fa, fill=ACCENT, anchor="lm",
           direction="rtl", language="ar")
    wa = tw(d, ar, fa, rtl=True)

    xm = ML + 48 + wa + 100
    d.line([xm, top + 128, xm, top + 312], fill=RULE, width=3)
    d.text((xm + 70, top + 238), "9", font=F("sans", 190, "Bold"),
           fill=INK_3, anchor="lm", direction="ltr")
    d.text((xm + 70, top + 336), "what every downstream regex is looking for",
           font=F("sans", 28), fill=INK_3, anchor="la", direction="ltr")
    d.text((ML + 48, top + 336), "one word, no digit anywhere in the turn",
           font=F("sans", 28), fill=INK_3, anchor="la", direction="ltr")

    y = top + 402 + 110
    fm = F("mono", 34, "Medium")
    d.text((ML, y), "re.search(r\"\\d{1,2}\", transcript)", font=fm,
           fill=INK_2, anchor="la", direction="ltr")
    d.text((ML + tw(d, "re.search(r\"\\d{1,2}\", transcript)", fm) + 40, y),
           "->  None", font=fm, fill=DEAD, anchor="la", direction="ltr")

    y += 122
    d.rectangle([ML, y, ML + 6, y + 108], fill=WARN)
    para(d, (ML + 40, y - 4),
         "Any agent matching \\d finds nothing, books no time, and raises no "
         "error. It is a silent failure, not a crash.",
         F("sans", 38, "Medium"), INK, MR - ML - 60, 54)
    return im


def slide_04():
    im, d, y = new_slide(4, "FINDING 2", "MEASURED LIVE")
    y = headline(d, y, "Partials are revised, not extended.", 78)
    y += 18
    y = deck(d, y, "Captured live. Between partial 2 and partial 3 the service "
                   "went back and changed a word it had already emitted. There "
                   "is no retraction event.")

    p1 = "\u0647\u0644 \u064a\u0645\u0643\u0646\u0643\u0645\u061f"
    p2 = ("\u0647\u0644 \u064a\u0645\u0643\u0646\u0643\u0645 "
          "\u062a\u0623\u062c\u064a\u0644 \u0627\u0644\u062a\u0635\u0648\u064a\u0631 "
          "\u0625\u0644\u0649 \u0627\u0644\u0633\u0646\u0629\u061f")
    p3 = ("\u0647\u0644 \u064a\u0645\u0643\u0646\u0643\u0645 "
          "\u062a\u0623\u062c\u064a\u0644 \u0627\u0644\u062a\u0635\u0648\u064a\u0631 "
          "\u0625\u0644\u0649 \u0627\u0644\u0633\u0627\u0639\u0629 "
          "\u062a\u0633\u0639\u0629 \u0635\u0628\u0627\u062d\u0627\u064b\u061f")
    assert_logical(p1, p2, p3)

    rows = [
        ("PARTIAL 1", p1, "", INK_2, PANEL, RULE),
        ("PARTIAL 2", p2, "to the YEAR", WARN, PANEL, RULE),
        ("PARTIAL 3", p3, "to NINE O'CLOCK", ACCENT, PANEL_HI, ACCENT_DIM),
    ]
    top = y + 62
    rh, gapr = 146, 22
    fa = F("ar", 50)
    for i, (tag, s, gloss, col, bg, oc) in enumerate(rows):
        ry = top + i * (rh + gapr)
        panel(d, [ML, ry, MR, ry + rh], fill=bg, outline=oc)
        tracked(d, (ML + 34, ry + 32), tag, F("mono", 21, "Medium"), INK_3, 5)
        if gloss:
            d.text((ML + 34, ry + 84), gloss, font=F("mono", 25, "Medium"),
                   fill=col, anchor="la", direction="ltr")
        d.text((MR - 38, ry + rh // 2), s, font=fa,
               fill=INK if i == 2 else INK_2, anchor="rm",
               direction="rtl", language="ar")

    y = top + 3 * (rh + gapr) + 34

    # the two words, side by side
    w_old = "\u0627\u0644\u0633\u0646\u0629"
    w_new = "\u0627\u0644\u0633\u0627\u0639\u0629 \u062a\u0633\u0639\u0629"
    assert_logical(w_old, w_new)
    fb = F("ar", 62, "Semibold")
    d.text((ML, y + 40), w_old, font=fb, fill=WARN, anchor="lm",
           direction="rtl", language="ar")
    x = ML + tw(d, w_old, fb, rtl=True) + 44
    d.text((x, y + 44), "became", font=F("sans", 30), fill=INK_3,
           anchor="lm", direction="ltr")
    x += tw(d, "became", F("sans", 30)) + 44
    d.text((x, y + 40), w_new, font=fb, fill=ACCENT, anchor="lm",
           direction="rtl", language="ar")

    y += 118
    d.rectangle([ML, y, ML + 6, y + 108], fill=WARN)
    para(d, (ML + 40, y - 4),
         "Prefix-matched barge-in fires on words the caller never said, and "
         "the action cannot be taken back. Act on end_of_turn.",
         F("sans", 38, "Medium"), INK, MR - ML - 60, 54)
    return im


def slide_05():
    im, d, y = new_slide(5, "FINDING 3", "MEASURED LIVE")
    y = headline(d, y, "Twilio's native 20 ms frame is rejected.", 78)
    y += 18
    y = deck(d, y, "The socket closes. This refuted the design's central "
                   "premise, which was to forward the phone bytes unchanged.")

    top = y + 96
    panel(d, [ML, top, MR, top + 300], fill=(19, 13, 14), outline=(74, 44, 46))
    tracked(d, (ML + 44, top + 46), "WHAT THE SOCKET SAID",
            F("mono", 22, "Medium"), (186, 112, 112), 5)
    fm = F("mono", 33)
    d.text((ML + 44, top + 118),
           "error_code 3007: Input Duration Error: Input Duration Violation: 20.0 ms.",
           font=fm, fill=(238, 196, 196), anchor="la", direction="ltr")
    d.text((ML + 44, top + 174),
           "                 Expected between 50 and 1000 ms",
           font=fm, fill=(238, 196, 196), anchor="la", direction="ltr")
    d.text((ML + 44, top + 238),
           "socket closed with code 3007, deterministic, never retried",
           font=F("sans", 27), fill=(168, 118, 118), anchor="la", direction="ltr")

    # legal range bar
    y = top + 300 + 110
    tracked(d, (ML, y), "LEGAL FRAME DURATION", F("mono", 22, "Medium"), INK_3, 5)
    by = y + 66
    bx0, bx1 = ML, MR
    d.rounded_rectangle([bx0, by, bx1, by + 68], radius=12, fill=PANEL,
                        outline=RULE, width=2)
    # 20 ms marker sits outside the legal window
    d.rounded_rectangle([bx0 + 2, by + 2, bx0 + 106, by + 66], radius=11,
                        fill=(74, 32, 36))
    d.rounded_rectangle([bx0 + 142, by + 2, bx1 - 2, by + 66], radius=11,
                        fill=ACCENT_DIM)
    d.text((bx0 + 24, by + 34), "20", font=F("mono", 29, "Semibold"),
           fill=(236, 158, 158), anchor="lm", direction="ltr")
    d.text((bx0 + 174, by + 34), "50 ms", font=F("mono", 29, "Semibold"),
           fill=ACCENT, anchor="lm", direction="ltr")
    d.text((bx1 - 26, by + 34), "1000 ms", font=F("mono", 29, "Semibold"),
           fill=ACCENT, anchor="rm", direction="ltr")
    d.text((bx0 + 6, by + 112), "rejected", font=F("sans", 26),
           fill=(168, 118, 118), anchor="la", direction="ltr")
    d.text((bx1, by + 112), "the legal window, every frame aggregated to 100 ms",
           font=F("sans", 26), fill=INK_3, anchor="ra", direction="ltr")

    y = by + 196
    d.rectangle([ML, y, ML + 6, y + 108], fill=ACCENT)
    para(d, (ML + 40, y - 4),
         "Aggregation is mandatory and costs up to 100 ms. The bytes are still "
         "never resampled: mu-law in, mu-law out.",
         F("sans", 38, "Medium"), INK, MR - ML - 60, 54)
    return im


def slide_06():
    im, d, y = new_slide(6, "LATENCY", "MEASURED LIVE")
    y = headline(d, y, "Three numbers, and blending them hides the one "
                       "that moved.", 74)
    y += 18
    y = deck(d, y, "Measured on the captured Arabic, a 4.15 s clip. Stage 2 is "
                   "bounded below by how long the caller spoke, so it is "
                   "compared to the speech duration, never to zero.")

    top = y + 62
    gap = 40
    cw = (MR - ML - 2 * gap) // 3
    cards = [
        ("1  TO FIRST PARTIAL", "~1,240", "ms", "AssemblyAI", INK),
        ("2  TO END OF TURN", "~4,600", "ms", "AssemblyAI, on 4.15 s of speech", INK),
        ("3  LLM + TTS", "474-586", "ms", "ours", ACCENT),
    ]
    for i, (lab, big, unit, sub, col) in enumerate(cards):
        x = ML + i * (cw + gap)
        panel(d, [x, top, x + cw, top + 288],
              fill=PANEL_HI if col is ACCENT else PANEL,
              outline=ACCENT_DIM if col is ACCENT else RULE)
        tracked(d, (x + 36, top + 38), lab, F("mono", 22, "Medium"),
                ACCENT if col is ACCENT else INK_3, 5)
        fbig = F("sans", 112 if len(big) <= 6 else 96, "Bold")
        d.text((x + 34, top + 100), big, font=fbig, fill=col,
               anchor="la", direction="ltr")
        bw = tw(d, big, fbig)
        d.text((x + 38 + bw, top + 172), unit, font=F("sans", 38),
               fill=INK_3, anchor="ls", direction="ltr")
        d.text((x + 36, top + 228), sub, font=F("sans", 26), fill=INK_2,
               anchor="la", direction="ltr")

    y = top + 288 + 54
    d.text((ML, y), "endpoint lag, caller stops to turn committed",
           font=F("sans", 28), fill=INK_3, anchor="la", direction="ltr")
    d.text((MR, y), "575 - 775 ms", font=F("mono", 30, "Medium"),
           fill=INK_2, anchor="ra", direction="ltr")

    y += 68
    rule(d, y, fill=RULE_SOFT)
    y += 46
    d.rectangle([ML, y, ML + 6, y + 112], fill=ACCENT)
    para(d, (ML + 40, y - 4),
         "End of speech to a spoken Arabic answer is about 1.05 to 1.36 s. Not "
         "from a faster model: from not calling one.",
         F("sans", 40, "Medium"), INK, MR - ML - 60, 56)

    y += 158
    half = (MR - ML - 90) // 2
    notes = [
        ("WHAT THIS REPLACED",
         "3,878 to 16,917 ms, measured over 7 real logged turns, essentially "
         "all of it the LLM. The agent is deterministic instead, so the rules "
         "path answers in 0.018 ms median."),
        ("THE VOICE IS FLAT WITH LENGTH",
         "546 ms of speech costs 421 ms to render. 14,928 ms of speech costs "
         "483 ms. The cost is process startup, not synthesis. Stage 3 marks "
         "COMPLETE audio, not a first byte."),
    ]
    for i, (h, body) in enumerate(notes):
        x = ML + i * (half + 90)
        tracked(d, (x, y), h, F("mono", 22, "Semibold"), INK_3, 5)
        para(d, (x, y + 48), body, F("sans", 27), INK_2, half, 40)
    return im


def slide_07():
    im, d, y = new_slide(7, "THE DEMO", "ONE PAGE, NOTHING DECORATIVE")
    y = headline(d, y, "Every number on the panel is measured on that turn.", 72)

    shot_path = DEMO_SHOT if os.path.exists(DEMO_SHOT) else LOCAL_SHOT
    if not os.path.exists(shot_path):
        raise SystemExit("demo screenshot missing: %s" % DEMO_SHOT)
    shot = Image.open(shot_path).convert("RGB")

    top = y + 44
    avail_h = H - top - 148
    col_w = int((MR - ML) * 0.615)
    scale = min(col_w / shot.width, avail_h / shot.height)
    sw, sh = int(shot.width * scale), int(shot.height * scale)
    shot = shot.resize((sw, sh), Image.LANCZOS)

    # frame it
    d.rounded_rectangle([ML - 8, top - 8, ML + sw + 8, top + sh + 8],
                        radius=14, fill=None, outline=RULE, width=2)
    im.paste(shot, (ML, top))

    tx = ML + sw + 86
    ty = top + 6
    tracked(d, (tx, ty), "WHAT IS ON SCREEN", F("mono", 22, "Semibold"),
            ACCENT, 5)
    ty += 56
    items = [
        ("The partial stream, revisions marked",
         "so the judge sees why acting on partials is unsafe rather than "
         "being told"),
        ("Raw Arabic beside the parsed slots",
         "the number word becoming a value is on screen, not described"),
        ("A latency strip of three numbers",
         "first partial, end of turn, reply, never blended into one"),
        ("Anything not measured on that turn is tagged sim",
         "and the replay is labelled a replay"),
    ]
    tw_col = MR - tx
    for head, body in items:
        d.rectangle([tx, ty + 8, tx + 5, ty + 34], fill=ACCENT)
        ny = para(d, (tx + 26, ty), head, F("sans", 31, "Medium"), INK,
                  tw_col - 26, 42)
        ny = para(d, (tx + 26, ny + 6), body, F("sans", 26), INK_3,
                  tw_col - 26, 36)
        ty = ny + 34

    ty += 10
    rule(d, ty, x0=tx, x1=MR, fill=RULE_SOFT)
    para(d, (tx, ty + 34),
         "Fallback for a judge with no microphone and no Arabic speaker to "
         "hand: ?mock=1 replays the real captured session end to end, clearly "
         "labelled as a replay.",
         F("sans", 26), INK_2, tw_col, 38)
    return im


def slide_08():
    im, d, y = new_slide(8, "WHO THIS IS FOR", "BUSINESS CASE")
    y = headline(d, y, "The failure is silent, so nobody files a bug.", 78)
    y += 18
    y = deck(d, y,
             "A booking agent matching \\d against Arabic finds nothing, books "
             "no time, and raises no error. It looks like a quiet day.")

    top = y + 88
    gap = 44
    cw = (MR - ML - gap) // 2

    # left: the WER gap
    panel(d, [ML, top, ML + cw, top + 366])
    tracked(d, (ML + 38, top + 44), "ASSEMBLYAI'S OWN ACCURACY TIERS",
            F("mono", 22, "Medium"), INK_3, 5)
    d.text((ML + 34, top + 112), "10-25%", font=F("sans", 122, "Bold"),
           fill=ACCENT, anchor="la", direction="ltr")
    d.text((ML + 38, top + 250), "the WER tier Arabic sits in",
           font=F("sans", 31), fill=INK, anchor="la", direction="ltr")
    d.text((ML + 38, top + 302), "English sits under 10%",
           font=F("sans", 28), fill=INK_3, anchor="la", direction="ltr")

    # right: end to end recovery
    x2 = ML + cw + gap
    panel(d, [x2, top, MR, top + 366], fill=PANEL_HI, outline=ACCENT_DIM)
    tracked(d, (x2 + 38, top + 44), "END TO END THROUGH THE LIVE API",
            F("mono", 22, "Medium"), ACCENT, 5)
    d.text((x2 + 34, top + 112), "12 of 12", font=F("sans", 122, "Bold"),
           fill=INK, anchor="la", direction="ltr")
    d.text((x2 + 38, top + 250), "values recovered by the parser",
           font=F("sans", 31), fill=INK, anchor="la", direction="ltr")
    d.text((x2 + 38, top + 302), "7 of 12 transcripts came back verbatim",
           font=F("sans", 28), fill=INK_3, anchor="la", direction="ltr")

    y = top + 366 + 80
    tracked(d, (ML, y), "THE OTHER FIVE WERE ORTHOGRAPHIC VARIATION, AND THE "
                        "VALUE STILL CAME OUT RIGHT",
            F("mono", 22, "Semibold"), INK_3, 5)

    pairs = [
        ("\u0625\u0644\u0627 \u0631\u0628\u0639\u0627\u064b",
         "\u0625\u0644\u0627 \u0631\u0628\u0639"),
        ("\u0648\u0627\u0644\u0631\u0628\u0639", "\u0648\u0631\u0628\u0639"),
        ("\u062e\u0645\u0633\u0645\u0626\u0629", "\u062e\u0645\u0633\u0645\u0627\u0626\u0629"),
        ("\u0648\u0623\u0631\u0628\u0639\u064a\u0646", "\u0648\u0627\u0631\u0628\u0639\u064a\u0646"),
        ("\u0627\u0644\u0627\u0633\u062a\u0648\u062f\u064a\u0648", "\u0627\u0644\u0633\u062a\u0648\u062f\u064a\u0648"),
    ]
    for a, b in pairs:
        assert_logical(a, b)

    ty = y + 62
    pw = (MR - ML) // 5
    fa = F("ar", 52)
    for i, (asked, heard) in enumerate(pairs):
        x = ML + i * pw
        d.text((x + pw - 44, ty + 36), asked, font=fa, fill=INK_3,
               anchor="rm", direction="rtl", language="ar")
        d.text((x + pw - 44, ty + 116), heard, font=fa, fill=INK,
               anchor="rm", direction="rtl", language="ar")
        if i:
            d.line([x - 6, ty + 4, x - 6, ty + 150], fill=RULE_SOFT, width=2)
    tracked(d, (ML, ty + 20), "ASKED", F("mono", 19, "Medium"), INK_3, 4)
    tracked(d, (ML, ty + 100), "HEARD", F("mono", 19, "Medium"), ACCENT, 4)

    y = ty + 216
    para(d, (ML, y),
         "A parser written against the one captured spelling would have looked "
         "perfect on the fixture and failed here. This is a synthetic speaker "
         "on a clean line: a floor on failure modes, not evidence of "
         "robustness on real callers.",
         F("sans", 29), INK_2, MR - ML, 42)
    return im


def slide_09():
    im, d, y = new_slide(9, "LIMITS, STATED AS LIMITS", "WHAT THIS DOES NOT DO")
    y = headline(d, y, "Every other entry will overclaim.", 78)
    y += 18
    y = deck(d, y, "These are the gaps as they stand today, in the order they "
                   "would bite someone who deployed this.")

    rows = [
        ("Accuracy on genuinely human Arabic is unmeasured.",
         "The single biggest gap. Everything measured here has been synthetic "
         "or captured-synthetic audio. No dialect variety, no noise, no mobile "
         "codec.", ACCENT),
        ("Real microphone capture is unverified.",
         "Chrome's fake device exercised the worklet, the resampler and the "
         "framing. It emits a tone, not speech. No Arabic has gone through a "
         "real microphone on the browser path.", INK_2),
        ("The agent has no calendar.",
         "It will happily confirm a slot that is already booked, and done "
         "writes nothing anywhere.", INK_2),
        ("The voice is a system voice.",
         "Intelligible, not warm. No measurement here defends it.", INK_2),
        ("Mid-turn reconnect loses the first half of a sentence.",
         "A new session has no memory of the old one. Untested live.", INK_2),
    ]

    top = y + 64
    rh = 134
    for i, (head, body, col) in enumerate(rows):
        ry = top + i * rh
        d.text((ML, ry + 8), "%02d" % (i + 1), font=F("mono", 30, "Medium"),
               fill=ACCENT if col is ACCENT else INK_3, anchor="la",
               direction="ltr")
        d.text((ML + 96, ry), head, font=F("sans", 38, "Medium"),
               fill=INK, anchor="la", direction="ltr")
        para(d, (ML + 96, ry + 58), body, F("sans", 28), INK_3,
             MR - ML - 96, 38)
        if i < len(rows) - 1:
            rule(d, ry + rh - 22, fill=RULE_SOFT)

    y = top + len(rows) * rh + 40
    d.rectangle([ML, y, ML + 6, y + 108], fill=ACCENT)
    para(d, (ML + 40, y - 4),
         "This slide is not modesty and it is not risk management. It is the "
         "strongest differentiator available.",
         F("sans", 36, "Medium"), INK, MR - ML - 60, 52)
    return im


def live_check_counts():
    """Run the suites and read their own totals, rather than typing them here.

    A count written into a slide is stale the moment a test is added, and
    nothing warns you. This deck already caught that failure twice elsewhere in
    the repo: the demo was showing a reply time from before the answer side
    worked, and a README pinned a version number that had moved on. A slide
    claiming 352 checks when the suite runs 487 is the same defect wearing a
    nicer font, and it is the one number on the deck a judge might actually
    verify.

    Raises rather than guessing. A deck that cannot prove its own numbers
    should fail to build, not print a plausible one.
    """
    import re
    import subprocess

    suites = [
        ("agent", ["python3", "test_agent.py"]),
        ("parser", ["python3", "test_arabic_numbers.py"]),
        ("stream", ["python3", "selftest.py"]),
        ("serverless", ["python3", "api_local.py", "--check"]),
    ]
    out = []
    for label, cmd in suites:
        r = subprocess.run(cmd, cwd=os.path.dirname(HERE) if os.path.basename(HERE) == "deck"
                           else HERE, capture_output=True, text=True,
                           timeout=600)
        if r.returncode != 0:
            raise SystemExit(
                f"deck refuses to build: the {label} suite failed "
                f"(exit {r.returncode}). Fix the suite, not the slide."
            )
        text = r.stdout
        # "487/487 passed" or "97/97 checks passed"
        m = re.search(r"(\d+)\s*/\s*\1\s+(?:checks\s+)?passed", text)
        if m:
            out.append((m.group(1), label))
            continue
        # api_local.py --check prints one "[ok ]" line per assertion.
        ok = len(re.findall(r"^\s*\[ok \]", text, re.M))
        if ok:
            out.append((str(ok), label))
            continue
        raise SystemExit(
            f"deck refuses to build: could not read a check count out of the "
            f"{label} suite. Its output format changed; fix the parser here "
            f"rather than hardcoding a number."
        )
    return out


def slide_10():
    im, d, y = new_slide(10, "WHERE TO LOOK", "SUBMISSION")
    y = headline(d, y, "An Arabic voice agent is not an English one with "
                       "the language code changed.", 72)
    y += 26
    y = deck(d, y, "Everything on these slides was measured against the live "
                   "API and is encoded as a test, so it cannot silently stop "
                   "being true.")

    top = y + 70
    gap = 44
    cw = (MR - ML - gap) // 2

    panel(d, [ML, top, ML + cw, top + 200])
    tracked(d, (ML + 38, top + 38), "REPOSITORY", F("mono", 22, "Medium"),
            ACCENT, 5)
    d.text((ML + 36, top + 96), "github.com/Syamjith-NK/arabic-voice-agent",
           font=F("mono", 32, "Medium"), fill=INK, anchor="la", direction="ltr")
    d.text((ML + 38, top + 148), "MIT licensed. numpy and websockets; "
                                 "everything else is stdlib.",
           font=F("sans", 25), fill=INK_3, anchor="la", direction="ltr")

    x2 = ML + cw + gap
    panel(d, [x2, top, MR, top + 200], fill=(24, 20, 12), outline=(84, 66, 34))
    tracked(d, (x2 + 38, top + 38), "HOSTED DEMO", F("mono", 22, "Medium"),
            WARN, 5)
    d.text((x2 + 36, top + 96), "[ URL NOT YET DECIDED ]",
           font=F("mono", 32, "Medium"), fill=WARN, anchor="la", direction="ltr")
    d.text((x2 + 38, top + 148), "hosting is an open decision in SUBMISSION.md "
                                 "section 9",
           font=F("sans", 25), fill=(150, 124, 84), anchor="la", direction="ltr")

    y = top + 200 + 74
    tracked(d, (ML, y), "BUILT DURING THE WINDOW", F("mono", 22, "Semibold"),
            INK_3, 5)
    ty = y + 56
    built = [
        "v3 streaming client, measured against the live API",
        "Arabic number-word parser",
        "deterministic booking agent with stateless resume",
        "Arabic speech with no vendor, no key and no bill",
        "web demo, driven in a real browser and screenshotted",
        "the serverless shape: mint a token, take one turn",
    ]
    colw = (MR - ML) // 2
    for i, item in enumerate(built):
        x = ML + (i % 2) * colw
        yy = ty + (i // 2) * 52
        d.rectangle([x, yy + 14, x + 12, yy + 18], fill=ACCENT)
        d.text((x + 30, yy), item, font=F("sans", 29), fill=INK_2,
               anchor="la", direction="ltr")

    y = ty + 3 * 52 + 52
    rule(d, y, fill=RULE_SOFT)
    y += 46
    tracked(d, (ML, y), "CHECKS THAT FAIL THE BUILD",
            F("mono", 22, "Semibold"), INK_3, 5)
    ty = y + 58
    counts = live_check_counts()
    x = ML
    for big, lab in counts:
        fbig = F("sans", 76, "Bold")
        d.text((x, ty), big, font=fbig, fill=ACCENT, anchor="la",
               direction="ltr")
        bw = tw(d, big, fbig)
        d.text((x + bw + 16, ty + 54), lab, font=F("sans", 30), fill=INK_2,
               anchor="ls", direction="ltr")
        x += bw + 16 + tw(d, lab, F("sans", 30)) + 78

    d.text((MR, ty + 54),
           "AssemblyAI Voice Agent Hackathon, lablab.ai",
           font=F("sans", 28), fill=INK_3, anchor="rs", direction="ltr")
    return im


# ================================================================= build ====

SLIDES = [slide_01, slide_02, slide_03, slide_04, slide_05,
          slide_06, slide_07, slide_08, slide_09, slide_10]


def main() -> int:
    if not features.check("raqm"):
        raise SystemExit(
            "This Pillow build has no Raqm, so it will not shape Arabic. "
            "Do not reach for arabic_reshaper: use a renderer that shapes.")

    os.makedirs(PAGES_DIR, exist_ok=True)
    images = []
    for i, fn in enumerate(SLIDES, 1):
        im = fn()
        png = os.path.join(PAGES_DIR, "slide_%02d.png" % i)
        im.save(png)
        images.append(im)
        print("  rendered %s" % os.path.basename(png))

    with PdfPages(OUT_PDF) as pdf:
        for im in images:
            fig = plt.figure(figsize=(W / DPI, H / DPI), dpi=DPI)
            fig.patch.set_facecolor([c / 255 for c in BG])
            fig.figimage(im, 0, 0, origin="upper")
            pdf.savefig(fig, dpi=DPI, facecolor=fig.get_facecolor())
            plt.close(fig)
        info = pdf.infodict()
        info["Title"] = "Arabic realtime voice agent, on AssemblyAI streaming"
        info["Author"] = "Syamjith NK"
        info["Subject"] = ("AssemblyAI Voice Agent Hackathon, lablab.ai, "
                           "submission 30 Sep 2026")

    print("\n  %s" % OUT_PDF)
    print("  %d pages, %dx%d px at %d dpi" % (len(images), W, H, DPI))
    return 0


if __name__ == "__main__":
    sys.exit(main())
