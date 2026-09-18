"""Assemble the submission video. No narration required.

lablab's own guidance is 0:00-0:30 problem, 0:30-2:30 working demo, 2:30-4:00
business case, five minutes maximum. Video is a judged category and most
entrants lose points here rather than on the code.

WHY THIS NEEDS NO VOICE. A recorded narration means a recording session, a
retake when a number changes, and a voice the owner may not want on a public
submission. Every claim in this entry is a MEASUREMENT, and a measurement reads
better on screen than it sounds spoken: a judge can pause a frame and check a
figure, which is exactly the behaviour this entry wants to invite. So the video
is built from the deck slides that already exist, a real screen capture of the
live demo, and title cards, with the timing carrying the emphasis instead of a
voice. The owner can lay narration over it later without rebuilding anything.

Everything here is assembled from artifacts that are already verified: the deck
pages are rendered from build_deck.py, which refuses to build if the test suites
fail, and the demo capture is the deployed site driven in a real browser.
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PAGES = HERE / "pages"
CAP = Path("/tmp/vid")
OUT = HERE / "SUBMISSION_VIDEO.mp4"
W, H = 1920, 1080
FPS = 25

SANS = "/System/Library/Fonts/SFNS.ttf"
MONO = "/System/Library/Fonts/SFNSMono.ttf"
BG = (10, 12, 14)
INK = (238, 242, 245)
DIM = (150, 160, 168)
ACC = (64, 224, 170)


def font(path, size, weight="Regular"):
    f = ImageFont.truetype(path, size)
    try:
        f.set_variation_by_name(weight)
    except Exception:
        pass
    return f


def card(lines, seconds, path):
    """A title card. `lines` is a list of (text, size, colour, weight)."""
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    heights = []
    for text, size, col, weight in lines:
        fo = font(SANS if weight != "mono" else MONO, size,
                  "Bold" if weight == "bold" else "Regular")
        bb = d.multiline_textbbox((0, 0), text, font=fo, spacing=12)
        heights.append((text, fo, col, bb[3] - bb[1]))
    total = sum(h for *_, h in heights) + 34 * (len(heights) - 1)
    y = (H - total) // 2
    for text, fo, col, h in heights:
        bb = d.multiline_textbbox((0, 0), text, font=fo, spacing=12)
        d.multiline_text(((W - (bb[2] - bb[0])) // 2, y), text, font=fo, fill=col,
                         spacing=12, align="center")
        y += h + 34
    im.save(path)
    return seconds


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()

    work = HERE / "_video"
    work.mkdir(exist_ok=True)
    seq = []          # (image path, seconds)

    # ---- 0:00 the problem, stated as a fact a judge can check -------------
    seq.append((work / "c1.png", card([
        ("An Arabic voice agent is not an English one", 76, INK, "bold"),
        ("with the language code changed.", 76, INK, "bold"),
        ("", 20, INK, "r"),
        ("Every claim that follows was measured against the live API", 34, DIM, "r"),
        ("and is encoded as a test.", 34, DIM, "r"),
    ], 6.0, work / "c1.png")))

    seq.append((work / "c2.png", card([
        ("AssemblyAI's Voice Agent API", 46, DIM, "r"),
        ("18 input languages, including Arabic", 56, INK, "r"),
        ("16 output voices. 11 English, 5 European.", 56, INK, "r"),
        ("Arabic: “coming soon”", 60, ACC, "bold"),
        ("", 16, INK, "r"),
        ("So a managed Arabic agent hears Arabic", 34, DIM, "r"),
        ("and answers in a European voice.", 34, DIM, "r"),
    ], 7.0, work / "c2.png")))

    # ---- the four findings, one slide each, from the built deck ------------
    seq.append((work / "c3.png", card([
        ("Four things the live API does", 66, INK, "bold"),
        ("that the documentation does not say.", 66, INK, "bold"),
    ], 3.5, work / "c3.png")))
    for n in (3, 4, 5):
        p = PAGES / f"slide_{n:02d}.png"
        if p.exists():
            seq.append((p, 7.0))

    # ---- the demo ---------------------------------------------------------
    seq.append((work / "c4.png", card([
        ("The demo is live.", 72, INK, "bold"),
        ("arabic-voice-agent-nu.vercel.app", 44, ACC, "mono"),
        ("", 16, INK, "r"),
        ("Static files plus two stateless functions.", 32, DIM, "r"),
        ("The browser holds the AssemblyAI socket itself.", 32, DIM, "r"),
    ], 5.0, work / "c4.png")))

    # ---- measurements -----------------------------------------------------
    seq.append((work / "c5.png", card([
        ("End of speech to a spoken Arabic answer", 46, DIM, "r"),
        ("1.05 – 1.36 seconds", 96, ACC, "bold"),
        ("", 16, INK, "r"),
        ("Not from a faster model. From not calling one.", 36, INK, "r"),
        ("The agent is deterministic: 0.018 ms per turn.", 32, DIM, "r"),
    ], 6.5, work / "c5.png")))

    seq.append((work / "c6.png", card([
        ("Accuracy on real human Arabic", 46, DIM, "r"),
        ("0.092 WER   read Modern Standard Arabic", 50, INK, "mono"),
        ("0.588 WER   spontaneous Emirati dialect", 50, ACC, "mono"),
        ("", 16, INK, "r"),
        ("The gap is the finding. At 0.588 an agent is working", 32, DIM, "r"),
        ("from about three wrong words in five.", 32, DIM, "r"),
    ], 7.5, work / "c6.png")))

    # ---- limits, deliberately included ------------------------------------
    if (PAGES / "slide_09.png").exists():
        seq.append((PAGES / "slide_09.png", 8.0))

    seq.append((work / "c7.png", card([
        ("github.com/Syamjith-NK/arabic-voice-agent", 44, INK, "mono"),
        ("arabic-voice-agent-nu.vercel.app", 44, ACC, "mono"),
        ("", 20, INK, "r"),
        ("AssemblyAI Voice Agent Hackathon · Syamjith NK", 30, DIM, "r"),
    ], 5.0, work / "c7.png")))

    # ---- normalise EVERY image to exactly 1920x1080 before concat ---------
    # The concat demuxer does not rescale per input: it takes the first stream's
    # size and everything after is stretched or squeezed to it. The sources here
    # are genuinely different shapes (deck pages 2560x1440, cards 1920x1080,
    # browser captures 1920x993), and the first render showed the demo capture
    # shrunk to about two thirds width because of exactly that. Normalising here
    # means the concat only ever sees one size.
    norm = work / "norm"
    norm.mkdir(exist_ok=True)
    for f in norm.glob("*.png"):
        f.unlink()

    def place(src: Path, dst: Path):
        im = Image.open(src).convert("RGB")
        r = min(W / im.width, H / im.height)
        im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))),
                       Image.LANCZOS)
        canvas = Image.new("RGB", (W, H), BG)
        canvas.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
        canvas.save(dst)

    caps = sorted(CAP.glob("f*.png"))
    entries = []          # (normalised path, seconds)
    k = 0
    for src, secs in seq:
        secs = round(secs * FPS) / FPS
        d = norm / f"n{k:04d}.png"; place(Path(src), d); entries.append((d, secs)); k += 1
        if Path(src).name == "c4.png" and caps:
            for c in caps:
                d = norm / f"n{k:04d}.png"; place(c, d)
                # Durations must be exact multiples of the frame interval.
                # At 25 fps a 0.1428 s entry cannot be represented and ffmpeg
                # rounds it up, which across 128 capture frames added 7.5 s to
                # a planned 87.8 s. The duration check below is what caught it.
                entries.append((d, 4 / FPS)); k += 1

    # ---- pipe raw frames, do not use the concat demuxer -------------------
    # Three attempts with `-f concat` all came out 7.5 s longer than planned,
    # and the duration check below is what exposed it. Rather than keep guessing
    # at how the demuxer treats a trailing entry, the frames are written
    # straight to the encoder: each image repeated round(seconds * FPS) times.
    # Duration is then frames divided by frame rate BY CONSTRUCTION, and the
    # assertion at the end cannot fail without something being genuinely wrong.
    import numpy as np

    enc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
         "-i", "-", "-c:v", "libx264", "-crf", "18", "-preset", "slow",
         "-pix_fmt", "yuv420p", a.out, "-y"],
        stdin=subprocess.PIPE)

    written = 0
    for pth, secs in entries:
        buf = np.asarray(Image.open(pth).convert("RGB"), dtype=np.uint8).tobytes()
        for _ in range(max(1, round(secs * FPS))):
            enc.stdin.write(buf)
            written += 1
    enc.stdin.close()
    enc.wait()

    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", a.out],
                         capture_output=True, text=True).stdout.strip()
    planned = written / FPS
    print(f"  {len(seq)} cards/slides + {len(caps)} captured demo frames")
    print(f"  {written} frames -> planned {planned:.2f}s, actual {float(dur):.2f}s")
    assert abs(float(dur) - planned) < 0.5, (
        f"duration mismatch: planned {planned:.2f}s, got {dur}s")
    print(f"  limit is 300s, so there is room for narration or a longer demo")
    print(f"  {a.out}")


if __name__ == "__main__":
    main()
