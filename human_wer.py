"""Word error rate on genuinely HUMAN Arabic speech, against the live v3 socket.

Every accuracy figure in this repo up to now came from synthetic or
captured-synthetic audio: `number_e2e.py` is a macOS voice reading clean text,
and `fixtures/v3_session_arabic.jsonl` is one captured sentence from the same
voice. §7 item 1 and §16 item 5 both say so, and both call it the single
biggest gap. A TTS voice on a clean line is plausibly EASIER for an ASR model
than a human, so 12/12 is a floor on failure modes, not evidence of robustness.

This file closes that gap with a measurement instead of an opinion. It pulls
real recorded human Arabic with ground-truth transcripts from public corpora,
streams each clip to the same live socket the agent uses, and computes word
error rate.

    python3 human_wer.py                     # both corpora, 30 clips each, live
    python3 human_wer.py --corpus fleurs --n 5
    python3 human_wer.py --cached            # re-score the saved run, no streaming
    python3 human_wer.py --cached --examples 8

TWO CORPORA, DELIBERATELY, BECAUSE THEY MEASURE DIFFERENT THINGS
----------------------------------------------------------------
`fleurs`      google/fleurs, config ar_eg, CC BY 4.0.
              Real humans, but READ speech: Egyptian speakers reading written
              Modern Standard Arabic sentences into a decent microphone. One
              speaker per clip, no crosstalk, no phone codec.

`casablanca`  UBC-NLP/Casablanca, config UAE, CC BY-NC-ND 4.0.
              Spontaneous Emirati dialect lifted from television: real
              conversation, real interruptions, background music and effects,
              and a dialect whose vocabulary is not MSA. This is much closer to
              what a Gulf voice agent actually hears, and much harder.

Reporting both is the point. A single WER on read MSA would flatter this repo
in exactly the way the synthetic number was already flattering it.

WHAT IS AND IS NOT NORMALISED
-----------------------------
Arabic WER is extremely sensitive to normalisation, so a WER quoted without its
normalisation is meaningless. Two figures are always printed: raw, on the
strings exactly as they arrive, and normalised, with the orthographic
transformations listed in `NORMALISATIONS` below and nothing else. The gap
between them is how much of the apparent error is spelling rather than hearing.

Nothing here can turn a wrong word into a right one, with two admitted
exceptions stated in the notes: ta-marbuta to ha and alef-maqsura to ya can, in
rare pairs, merge two genuinely different words.

LICENCE HANDLING
----------------
No audio is committed and no audio leaves the cache directory, which is
gitignored. FLEURS is CC BY 4.0, so its reference and hypothesis text is stored
in full in the results JSON. Casablanca is CC BY-NC-ND 4.0, so the committed
JSON carries metrics plus short excerpts only; the full text stays in the local
cache. Both are cited with dataset id, config, split and exact row offsets, so
the sample is reproducible by anyone.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from aai_stream import (
    AAIStream,
    PCM16_BYTES_PER_MS_16K,
    PCM16_SILENCE,
    Turn,
    build_url,
)

HERE = Path(__file__).resolve().parent
CACHE = HERE / "audio_cache"
OUT = HERE / "fixtures" / "human_wer.json"
ROWS_API = "https://datasets-server.huggingface.co/rows"
HF_TOKEN_PATH = Path.home() / "jarvis" / ".credentials" / "hf_token"

FRAME_MS = 20
BYTES_PER_FRAME = FRAME_MS * PCM16_BYTES_PER_MS_16K      # 640 bytes of 16 kHz PCM16
EXCERPT = 110                                            # chars kept for an NC-ND corpus


# ---------------------------------------------------------------------------
# Corpora
# ---------------------------------------------------------------------------

@dataclass
class Corpus:
    key: str
    dataset: str
    config: str
    split: str
    licence: str
    text_field: str
    id_field: str
    kind: str
    quote_in_full: bool          # may the committed JSON carry the whole string?
    alt_text_field: str = ""     # a second reference the corpus ships, if any
    note: str = ""


CORPORA = {
    "fleurs": Corpus(
        key="fleurs",
        dataset="google/fleurs", config="ar_eg", split="test",
        licence="CC BY 4.0",
        text_field="raw_transcription", alt_text_field="transcription",
        id_field="id",
        kind="read Modern Standard Arabic, Egyptian speakers, clean single-speaker recordings",
        quote_in_full=True,
        note="FLEURS ships two references: raw_transcription with punctuation and "
             "original orthography, and transcription already lowercased and stripped. "
             "raw_transcription is used here so the raw WER is genuinely raw.",
    ),
    "casablanca": Corpus(
        key="casablanca",
        dataset="UBC-NLP/Casablanca", config="UAE", split="test",
        licence="CC BY-NC-ND 4.0",
        text_field="transcription", id_field="seg_id",
        kind="spontaneous Emirati dialect from television, real conversation and background audio",
        quote_in_full=False,
        note="Non-commercial, no-derivatives. Audio is never committed and the "
             "committed JSON carries excerpts only.",
    ),
}


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

NORMALISATIONS = [
    "NFC compose",
    "strip tashkeel and Quranic marks (U+0610-061A, U+064B-065F, U+0670, U+06D6-06ED)",
    "strip tatweel U+0640",
    "alef forms: أ إ آ ٱ -> ا",
    "alef maqsura: ى -> ي",
    "ta marbuta: ة -> ه",
    "Arabic-Indic digits ٠-٩ and ۰-۹ -> ASCII 0-9",
    "drop every Unicode punctuation codepoint (category P*)",
    "collapse whitespace",
]

# Deliberately NOT applied, and each omission is a decision:
#   ؤ -> و and ئ -> ي     a heavier hamza fold that merges more real word pairs
#   ال- definite article   dropping it hides a genuine agreement error
#   any stemming          a stem match is not a word match

_TASHKEEL = set(
    list(range(0x0610, 0x061B)) + list(range(0x064B, 0x0660)) +
    [0x0670] + list(range(0x06D6, 0x06EE))
)
_ALEFS = {"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"}
_DIGITS = {**{chr(0x0660 + i): str(i) for i in range(10)},
           **{chr(0x06F0 + i): str(i) for i in range(10)}}


def normalise(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    out = []
    for ch in s:
        cp = ord(ch)
        if cp in _TASHKEEL or cp == 0x0640:
            continue
        ch = _ALEFS.get(ch, ch)
        ch = _DIGITS.get(ch, ch)
        if ch == "ى":
            ch = "ي"
        elif ch == "ة":
            ch = "ه"
        if unicodedata.category(ch).startswith("P"):
            ch = " "
        out.append(ch)
    return " ".join("".join(out).split())


def words(s: str) -> list[str]:
    return s.split()


# ---------------------------------------------------------------------------
# Word error rate
# ---------------------------------------------------------------------------

@dataclass
class WER:
    sub: int = 0
    dele: int = 0
    ins: int = 0
    n_ref: int = 0

    @property
    def errors(self) -> int:
        return self.sub + self.dele + self.ins

    @property
    def rate(self) -> float:
        # A reference with no words cannot have a rate. Returning 0.0 there would
        # quietly pull a corpus average down, so callers must skip empty refs.
        return self.errors / self.n_ref if self.n_ref else float("nan")

    def __add__(self, o: "WER") -> "WER":
        return WER(self.sub + o.sub, self.dele + o.dele,
                   self.ins + o.ins, self.n_ref + o.n_ref)

    def as_dict(self) -> dict:
        return {"sub": self.sub, "del": self.dele, "ins": self.ins,
                "ref_words": self.n_ref, "errors": self.errors,
                "wer": None if self.n_ref == 0 else round(self.rate, 4)}


def wer(ref: list[str], hyp: list[str]) -> WER:
    """Standard word error rate: Levenshtein over word sequences, (S+D+I)/N.

    Not a similarity ratio and not a character diff. The backtrace is what gives
    the S/D/I split, and that split is worth having: deletions dominating means
    the service dropped speech, insertions dominating means it invented it, and
    those are different bugs.
    """
    n, m = len(ref), len(hyp)
    if n == 0:
        return WER(ins=m, n_ref=0)
    # d[i][j] = (cost, op) where op is how we got here.
    d = [[(0, "")] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        d[i][0] = (i, "d")
    for j in range(1, m + 1):
        d[0][j] = (j, "i")
    for i in range(1, n + 1):
        ri = ref[i - 1]
        for j in range(1, m + 1):
            if ri == hyp[j - 1]:
                d[i][j] = (d[i - 1][j - 1][0], "=")
                continue
            sub = d[i - 1][j - 1][0] + 1
            dele = d[i - 1][j][0] + 1
            ins = d[i][j - 1][0] + 1
            best = min(sub, dele, ins)
            d[i][j] = (best, "s" if best == sub else ("d" if best == dele else "i"))
    r = WER(n_ref=n)
    i, j = n, m
    while i > 0 or j > 0:
        if i == 0:
            r.ins += 1
            j -= 1
            continue
        if j == 0:
            r.dele += 1
            i -= 1
            continue
        op = d[i][j][1]
        if op == "=":
            i, j = i - 1, j - 1
        elif op == "s":
            r.sub += 1
            i, j = i - 1, j - 1
        elif op == "d":
            r.dele += 1
            i -= 1
        else:
            r.ins += 1
            j -= 1
    return r


# ---------------------------------------------------------------------------
# Corruption signature: is the Arabic that comes back CLEAN?
# ---------------------------------------------------------------------------

_BIDI = {0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
         0x2066, 0x2067, 0x2068, 0x2069}
_FORM_WORDS = ("ISOLATED FORM", "INITIAL FORM", "MEDIAL FORM", "FINAL FORM")


def is_presentation_form(ch: str) -> bool:
    """A positional form, derived from the codepoint NAME rather than a range.

    The Arabic Presentation Forms blocks are INTERLEAVED: the ornate parentheses
    U+FD3E/U+FD3F and the honorifics just above them sit inside the range but are
    not positional forms at all, and they are common in Islamic heritage text. A
    range test flags those and reports corruption that is not there.
    """
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return False
    return name.startswith("ARABIC ") and any(w in name for w in _FORM_WORDS)


def corruption(s: str) -> dict:
    forms = sum(1 for ch in s if is_presentation_form(ch))
    bidi = sum(1 for ch in s if ord(ch) in _BIDI)
    repl = s.count("�")
    return {"presentation_forms": forms, "bidi_controls": bidi, "replacement_char": repl}


def ascii_digits(s: str) -> int:
    return sum(1 for ch in s if ch.isdigit() and ch.isascii())


# ---------------------------------------------------------------------------
# Corpus fetch and audio cache
# ---------------------------------------------------------------------------

def hf_token() -> str:
    env = os.environ.get("HF_TOKEN", "").strip()
    if env:
        return env
    try:
        return HF_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _get(url: str, token: str = "", timeout: float = 90.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "lablab-assemblyai/human_wer"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_rows(c: Corpus, offset: int, length: int, token: str) -> list[dict]:
    """One page of the datasets-server rows API. No full-corpus download."""
    url = (f"{ROWS_API}?dataset={urllib.parse.quote(c.dataset, safe='')}"
           f"&config={urllib.parse.quote(c.config)}&split={c.split}"
           f"&offset={offset}&length={length}")
    return json.loads(_get(url, token))["rows"]


def clip_seconds(row: dict) -> float:
    if "duration" in row:
        return float(row["duration"])
    if "num_samples" in row:
        return float(row["num_samples"]) / 16000.0
    return 0.0


def to_pcm16(src_url: str, dest: Path) -> bytes:
    """Download one clip and transcode once, up front, to 16 kHz mono PCM16.

    The API wants exactly that. FLEURS ships wav, Casablanca ships wav, Common
    Voice ships mp3 at 32 or 48 kHz; ffmpeg flattens all of it and the raw bytes
    are cached so a re-run never re-downloads or re-transcodes.
    """
    if dest.exists() and dest.stat().st_size > 0:
        return dest.read_bytes()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_in = dest.with_suffix(".src")
    tmp_in.write_bytes(_get(src_url))
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(tmp_in),
         "-ac", "1", "-ar", "16000", "-f", "s16le", "-acodec", "pcm_s16le",
         str(dest)],
        capture_output=True,
    )
    tmp_in.unlink(missing_ok=True)
    if proc.returncode != 0 or not dest.exists():
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode()[:300]}")
    return dest.read_bytes()


@dataclass
class Clip:
    corpus: str
    offset: int
    clip_id: str
    ref: str
    ref_alt: str
    seconds: float
    pcm_path: Path
    sha256: str = ""
    extra: dict = field(default_factory=dict)


def build_cache(c: Corpus, n: int, start: int, min_s: float, max_s: float,
                token: str) -> list[Clip]:
    """Select n usable clips from consecutive row offsets, transcode, cache.

    Selection is deterministic: consecutive offsets from `start`, skipping only
    clips outside the duration window. Every offset kept is recorded, so the
    sample is reproducible rather than 'some clips'.
    """
    clips: list[Clip] = []
    offset, page = start, 20
    skipped = 0
    while len(clips) < n:
        rows = fetch_rows(c, offset, page, token)
        if not rows:
            break
        for r in rows:
            if len(clips) >= n:
                break
            row, idx = r["row"], r["row_idx"]
            secs = clip_seconds(row)
            if not (min_s <= secs <= max_s):
                skipped += 1
                continue
            ref = (row.get(c.text_field) or "").strip()
            if not ref:
                skipped += 1
                continue
            src = row["audio"][0]["src"]
            cid = str(row.get(c.id_field, idx))
            dest = CACHE / c.key / f"{idx:05d}_{cid.replace('/', '_')}.raw"
            try:
                pcm = to_pcm16(src, dest)
            except Exception as exc:                     # noqa: BLE001
                print(f"    skip offset {idx}: {exc}")
                skipped += 1
                continue
            clips.append(Clip(
                corpus=c.key, offset=idx, clip_id=cid, ref=ref,
                ref_alt=(row.get(c.alt_text_field) or "").strip() if c.alt_text_field else "",
                seconds=round(secs, 2), pcm_path=dest,
                sha256=hashlib.sha256(pcm).hexdigest()[:16],
                extra={k: row[k] for k in ("gender",) if k in row},
            ))
        offset += page
    if skipped:
        print(f"    ({skipped} rows skipped: outside {min_s}-{max_s}s, empty text, or unfetchable)")
    return clips


# ---------------------------------------------------------------------------
# Live transcription
# ---------------------------------------------------------------------------

async def transcribe(pcm: bytes, *, pace: bool) -> tuple[str, int, float, list]:
    """Stream one clip and return (hypothesis, n_turns, elapsed_s, errors).

    A clip of real speech is long enough to be committed as SEVERAL turns, so
    the hypothesis is every committed turn joined in order. Taking the last one,
    which is enough for a one-sentence probe, would silently discard most of the
    words and report a beautiful deletion-heavy WER.
    """
    finals: list[Turn] = []

    def on_turn(t: Turn) -> None:
        if t.end_of_turn:
            finals.append(t)

    stream = AAIStream(
        language="ar",
        url=build_url(language="ar", sample_rate=16000, encoding="pcm_s16le"),
        on_turn=on_turn,
        chunk_ms=100,
        bytes_per_ms=PCM16_BYTES_PER_MS_16K,
        pad_byte=PCM16_SILENCE,
        idle_timeout_s=90.0,
    )
    t0 = time.monotonic()
    await stream.start()
    try:
        for off in range(0, len(pcm), BYTES_PER_FRAME):
            await stream.feed(pcm[off:off + BYTES_PER_FRAME])
            if pace:
                await asyncio.sleep(FRAME_MS / 1000)
        await stream.finish(timeout=15.0)
    finally:
        await stream.close()
    by_order: dict[int, str] = {}
    for t in finals:
        by_order[t.turn_order] = t.transcript
    hyp = " ".join(by_order[k] for k in sorted(by_order)).strip()
    return hyp, len(by_order), time.monotonic() - t0, list(stream.errors)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_row(ref: str, hyp: str, ref_alt: str = "") -> dict:
    raw = wer(words(ref), words(hyp))
    nrm = wer(words(normalise(ref)), words(normalise(hyp)))
    row = {
        "ref_words": len(words(ref)),
        "hyp_words": len(words(hyp)),
        "wer_raw": raw.as_dict(),
        "wer_norm": nrm.as_dict(),
        "corruption": corruption(hyp),
        "hyp_ascii_digits": ascii_digits(hyp),
        "ref_ascii_digits": ascii_digits(ref),
    }
    if ref_alt:
        row["wer_norm_alt_ref"] = wer(words(normalise(ref_alt)),
                                      words(normalise(hyp))).as_dict()
    return row


def aggregate(rows: list[dict], key: str) -> WER:
    total = WER()
    for r in rows:
        d = r.get(key)
        if not d:
            continue
        total = total + WER(d["sub"], d["del"], d["ins"], d["ref_words"])
    return total


def summarise(corpus_key: str, rows: list[dict]) -> dict:
    scored = [r for r in rows if r.get("wer_raw")]
    raw, nrm = aggregate(scored, "wer_raw"), aggregate(scored, "wer_norm")
    alt = aggregate([r for r in scored if r.get("wer_norm_alt_ref")], "wer_norm_alt_ref")
    per_clip = sorted(r["wer_norm"]["wer"] for r in scored if r["wer_norm"]["wer"] is not None)
    corr = {k: sum(r["corruption"][k] for r in scored)
            for k in ("presentation_forms", "bidi_controls", "replacement_char")}
    empty = sum(1 for r in scored if r["hyp_words"] == 0)
    return {
        "corpus": corpus_key,
        "clips": len(scored),
        "audio_seconds": round(sum(r["seconds"] for r in scored), 1),
        "empty_hypotheses": empty,
        "wer_raw": raw.as_dict(),
        "wer_normalised": nrm.as_dict(),
        "wer_normalised_alt_reference": alt.as_dict() if alt.n_ref else None,
        "median_per_clip_wer_normalised": (
            round(per_clip[len(per_clip) // 2], 4) if per_clip else None),
        "worst_per_clip_wer_normalised": round(per_clip[-1], 4) if per_clip else None,
        "best_per_clip_wer_normalised": round(per_clip[0], 4) if per_clip else None,
        "corruption_totals": corr,
        "hyp_ascii_digits_total": sum(r["hyp_ascii_digits"] for r in scored),
        "ref_ascii_digits_total": sum(r["ref_ascii_digits"] for r in scored),
    }


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def public_text(c: Corpus, s: str) -> str:
    """What may go in the committed JSON, given the corpus licence."""
    if c.quote_in_full:
        return s
    return s[:EXCERPT] + (" [...]" if len(s) > EXCERPT else "")


async def run_corpus(c: Corpus, n: int, start: int, gap: float, pace: bool,
                     min_s: float, max_s: float, token: str,
                     last_connect: list[float]) -> list[dict]:
    print(f"\n  {c.dataset}  config={c.config}  split={c.split}  [{c.licence}]")
    print(f"  {c.kind}")
    clips = build_cache(c, n, start, min_s, max_s, token)
    print(f"  {len(clips)} clips cached, "
          f"{sum(cl.seconds for cl in clips):.0f}s of audio\n")

    rows = []
    for i, cl in enumerate(clips, 1):
        # A NEW socket per clip, and the free tier caps NEW connections at 5 per
        # minute. Tripping that surfaces as empty transcripts, i.e. as a WER of
        # 1.0 that looks like a finding about Arabic and is really a finding
        # about our own pacing.
        wait = gap - (time.monotonic() - last_connect[0])
        if last_connect[0] and wait > 0:
            print(f"      (waiting {wait:.0f}s: 5 new connections/minute cap)")
            await asyncio.sleep(wait)
        last_connect[0] = time.monotonic()

        pcm = cl.pcm_path.read_bytes()
        try:
            hyp, n_turns, elapsed, errors = await transcribe(pcm, pace=pace)
        except Exception as exc:                          # noqa: BLE001
            print(f"  [{i:>2}/{len(clips)}] offset {cl.offset}: stream failed: {exc!r}")
            rows.append({"corpus": c.key, "offset": cl.offset, "clip_id": cl.clip_id,
                         "seconds": cl.seconds, "sha256": cl.sha256,
                         "stream_error": repr(exc)})
            continue
        row = {
            "corpus": c.key, "offset": cl.offset, "clip_id": cl.clip_id,
            "seconds": cl.seconds, "sha256": cl.sha256, "turns": n_turns,
            "elapsed_s": round(elapsed, 2), "errors": errors,
            "ref": public_text(c, cl.ref), "hyp": public_text(c, hyp),
            "ref_full_withheld": not c.quote_in_full,
            **score_row(cl.ref, hyp, cl.ref_alt),
        }
        rows.append(row)
        w = row["wer_norm"]["wer"]
        flag = "  EMPTY" if row["hyp_words"] == 0 else ""
        print(f"  [{i:>2}/{len(clips)}] offset {cl.offset:>4}  {cl.seconds:>5.1f}s  "
              f"ref {row['ref_words']:>3}w  hyp {row['hyp_words']:>3}w  "
              f"WER(norm) {w if w is None else f'{w:.3f}'}{flag}")
        if errors:
            print(f"        errors: {errors}")
        # Keep the full text for the local cache, never for the committed JSON.
        row["_ref_full"], row["_hyp_full"] = cl.ref, hyp
    return rows


def print_examples(rows: list[dict], k: int) -> None:
    """Print reference against hypothesis and READ them.

    A WER number with no examples is unreviewable, and the metric cannot tell
    you that the model is confidently producing a different dialect, or that a
    whole clip was dropped because the speaker was quiet.
    """
    have = [r for r in rows if r.get("ref")]
    if not have:
        return
    have.sort(key=lambda r: r["wer_norm"]["wer"] if r.get("wer_norm") else 0)
    picks = []
    if k >= 1:
        picks.append(("best", have[0]))
    if k >= 2:
        picks.append(("median", have[len(have) // 2]))
    if k >= 3:
        picks.append(("worst", have[-1]))
    for extra in have[1:len(have) - 1][:max(0, k - 3)]:
        picks.append(("also", extra))
    print("\n  reference vs hypothesis, read by a human before publishing:\n")
    for label, r in picks:
        w = r["wer_norm"]["wer"]
        print(f"    [{label}] {r['corpus']} offset {r['offset']}  "
              f"WER(norm) {w:.3f}  raw {r['wer_raw']['wer']:.3f}")
        print(f"      ref: {r.get('_ref_full', r['ref'])}")
        print(f"      hyp: {r.get('_hyp_full', r['hyp']) or '(nothing)'}")
        print()


async def main_async(a) -> int:
    token = hf_token()
    keys = list(CORPORA) if a.corpus == "both" else [a.corpus]
    all_rows: list[dict] = []
    last_connect = [0.0]
    for key in keys:
        all_rows += await run_corpus(
            CORPORA[key], a.n, a.start, a.gap, not a.fast,
            a.min_seconds, a.max_seconds, token, last_connect)
    write(all_rows, paced=not a.fast)
    report(all_rows, a.examples)
    return 0


def write(rows: list[dict], *, paced: bool) -> None:
    """Split the record in two, because the two have different licences.

    The committed JSON carries everything needed to check the arithmetic and, for
    the CC BY corpus, the full text. The cache file carries the full text for the
    no-derivatives corpus and never leaves this machine.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    full = [{**r} for r in rows]
    (CACHE / "human_wer_full.json").write_text(
        json.dumps({"rows": full}, ensure_ascii=False, indent=2), encoding="utf-8")

    public = []
    for r in rows:
        pr = {k: v for k, v in r.items() if not k.startswith("_")}
        public.append(pr)
    doc = {
        "measured_at": time.strftime("%Y-%m-%d"),
        "paced_at_real_time": paced,
        "normalisations": NORMALISATIONS,
        "sources": {k: {"dataset": c.dataset, "config": c.config, "split": c.split,
                        "licence": c.licence, "kind": c.kind,
                        "reference_field": c.text_field, "note": c.note}
                    for k, c in CORPORA.items()},
        "summary": {k: summarise(k, [r for r in public if r["corpus"] == k])
                    for k in CORPORA
                    if any(r["corpus"] == k for r in public)},
        "rows": public,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  wrote {OUT.relative_to(HERE)} and audio_cache/human_wer_full.json")


def report(rows: list[dict], examples: int) -> None:
    for key in CORPORA:
        sub = [r for r in rows if r.get("corpus") == key and r.get("wer_raw")]
        if not sub:
            continue
        c, s = CORPORA[key], summarise(key, sub)
        print(f"\n  == {key}: {c.dataset} / {c.config} / {c.split}   [{c.licence}]")
        print(f"     {s['clips']} clips, {s['audio_seconds']}s of human speech")
        print(f"     WER raw         {s['wer_raw']['wer']:.3f}   "
              f"(S {s['wer_raw']['sub']}  D {s['wer_raw']['del']}  "
              f"I {s['wer_raw']['ins']}  over {s['wer_raw']['ref_words']} reference words)")
        print(f"     WER normalised  {s['wer_normalised']['wer']:.3f}   "
              f"(S {s['wer_normalised']['sub']}  D {s['wer_normalised']['del']}  "
              f"I {s['wer_normalised']['ins']})")
        if s["wer_normalised_alt_reference"]:
            print(f"     WER normalised against the corpus's own normalised reference "
                  f"{s['wer_normalised_alt_reference']['wer']:.3f}")
        print(f"     per clip normalised: best {s['best_per_clip_wer_normalised']:.3f}  "
              f"median {s['median_per_clip_wer_normalised']:.3f}  "
              f"worst {s['worst_per_clip_wer_normalised']:.3f}")
        print(f"     empty hypotheses {s['empty_hypotheses']}")
        print(f"     corruption: presentation forms {s['corruption_totals']['presentation_forms']}, "
              f"bidi controls {s['corruption_totals']['bidi_controls']}, "
              f"U+FFFD {s['corruption_totals']['replacement_char']}")
        print(f"     ASCII digits: {s['hyp_ascii_digits_total']} in hypotheses, "
              f"{s['ref_ascii_digits_total']} in references")
        print_examples(sub, examples)
    print("  Read speech and broadcast dialect are DIFFERENT measurements and are")
    print("  never blended into one figure. Neither is spontaneous Gulf speech on a")
    print("  phone line, which is what this agent is actually for.")


def rescore(examples: int) -> int:
    """Re-score a finished run with no streaming and no API key.

    Normalisation is the part of this measurement most likely to change, and
    re-running 60 live sockets to try a different fold would be absurd.
    """
    cached = CACHE / "human_wer_full.json"
    if not cached.exists():
        print(f"  no cached run at {cached}; run without --cached first")
        return 2
    rows = json.loads(cached.read_text(encoding="utf-8"))["rows"]
    out = []
    for r in rows:
        if "_ref_full" not in r:
            out.append(r)
            continue
        c = CORPORA[r["corpus"]]
        ref, hyp = r["_ref_full"], r["_hyp_full"]
        out.append({**r, "ref": public_text(c, ref), "hyp": public_text(c, hyp),
                    **score_row(ref, hyp, "")})
    write(out, paced=True)
    report(out, examples)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--corpus", choices=[*CORPORA, "both"], default="both")
    p.add_argument("--n", type=int, default=30, help="clips per corpus (25-40 is the intent)")
    p.add_argument("--start", type=int, default=0, help="first row offset")
    p.add_argument("--gap", type=float, default=13.0,
                   help="min seconds between new sockets (free tier: 5/min)")
    p.add_argument("--fast", action="store_true",
                   help="do not pace to real time (cheaper, different endpointing)")
    p.add_argument("--min-seconds", type=float, default=2.0)
    p.add_argument("--max-seconds", type=float, default=20.0)
    p.add_argument("--examples", type=int, default=3,
                   help="reference/hypothesis pairs to print per corpus")
    p.add_argument("--cached", action="store_true",
                   help="re-score the saved run without streaming anything")
    a = p.parse_args()
    if a.cached:
        sys.exit(rescore(a.examples))
    sys.exit(asyncio.run(main_async(a)))


if __name__ == "__main__":
    main()
