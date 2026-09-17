"""Regenerate fixtures/v3_session_arabic.jsonl.

This writes a HAND-AUTHORED protocol script to AssemblyAI's documented v3 schema.
It is not captured traffic - see fixtures/README.md. It exists so the replay
harness is reproducible and so the honest provenance is in code rather than in a
binary blob nobody can audit.

Once a real key exists, `replay.py --live --record` replaces this file with
genuinely captured frames and this script becomes history.

Run:  python3 make_fixture.py
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "fixtures" / "v3_session_arabic.jsonl"
AUDIO = HERE / "fixtures" / "arabic_caller_8k.ulaw"

# The sentence in fixtures/arabic_caller_8k.ulaw, from call_agent's selftest turn 4.
FULL = "هل يمكنكم تأجيل التصوير إلى الساعة 9 صباحاً؟"

# Progressive partials. v3 resends the WHOLE turn each time, never a delta, so each
# of these is a prefix of the next - that is the property the client must handle by
# replacing its current turn rather than appending.
#
# after_bytes = how much audio the server must have received before emitting this.
# 8000 bytes of 8 kHz mu-law = 1.0 s. The audio is 33,228 bytes = 4.15 s.
PARTIALS = [
    (6_400,  "هل"),
    (11_200, "هل يمكنكم"),
    (16_000, "هل يمكنكم تأجيل"),
    (21_600, "هل يمكنكم تأجيل التصوير"),
    (26_400, "هل يمكنكم تأجيل التصوير إلى الساعة"),
    (31_200, "هل يمكنكم تأجيل التصوير إلى الساعة 9 صباحا"),
]


def words_for(text: str) -> list:
    """Minimal word objects. v3 sends per-word timing and confidence; the client
    stores them untouched, so the shape matters and the values do not."""
    out, t = [], 0
    for w in text.split():
        out.append({"text": w, "start": t, "end": t + 300, "confidence": 0.95,
                    "word_is_final": True})
        t += 360
    return out


def build() -> list[dict]:
    recs: list[dict] = []

    # Begin: sent as soon as the socket is up, before any audio.
    recs.append({"after_bytes": 0, "message": {
        "type": "Begin",
        "id": "replay-fixture-0000-0000-0000",
        "expires_at": 9_999_999_999,
    }})

    for after, text in PARTIALS:
        recs.append({"after_bytes": after, "message": {
            "type": "Turn",
            "turn_order": 0,
            "turn_is_formatted": False,
            "end_of_turn": False,
            # Confidence climbs as the utterance settles. The client must NOT treat
            # a high partial confidence as end-of-turn - only the flag means that.
            "end_of_turn_confidence": round(0.05 + 0.10 * (after / 33_228), 4),
            "transcript": text,
            "words": words_for(text),
        }})

    # The final turn. Emitted after ALL the audio plus the silent tail, which is
    # what a real endpointer would wait for.
    recs.append({"after_bytes": 33_228, "message": {
        "type": "Turn",
        "turn_order": 0,
        # NOTE: turn_is_formatted is false on purpose. format_turns is documented as
        # unavailable on universal-3-5-pro, so we must NOT assume punctuated Arabic
        # comes back. If the live service does format it, this fixture is wrong and
        # --record will correct it.
        "turn_is_formatted": False,
        "end_of_turn": True,
        "end_of_turn_confidence": 0.9312,
        "transcript": FULL,
        "words": words_for(FULL),
    }})

    # Termination closes the session and reports what we were billed on: duration.
    recs.append({"after_bytes": 33_228, "message": {
        "type": "Termination",
        "audio_duration_seconds": round(33_228 / 8000, 3),
        "session_duration_seconds": round(33_228 / 8000, 3),
    }})
    return recs


if __name__ == "__main__":
    if not AUDIO.exists():
        raise SystemExit(
            f"missing {AUDIO}\n"
            "Rebuild it with:\n"
            "  ffmpeg -y -i ../call_agent/out/caller_4.mp3 -ar 8000 -ac 1 -f mulaw "
            f"{AUDIO}"
        )
    n = AUDIO.stat().st_size
    recs = build()
    bad = [r for r in recs if r["after_bytes"] > n]
    if bad:
        raise SystemExit(
            f"fixture asks for up to {max(r['after_bytes'] for r in bad)} bytes but the "
            f"audio is only {n}. A message gated past the end of the audio would never "
            f"fire and the replay would hang."
        )
    OUT.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT}  ({len(recs)} messages, audio {n} bytes / {n/8000:.2f}s)")
