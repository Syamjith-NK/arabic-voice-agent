"""Mint a short-lived AssemblyAI streaming token for the browser.

WHY THIS EXISTS AT ALL
----------------------
A browser cannot set an `Authorization` header on a WebSocket. That single
limitation decides the whole hosting architecture, so it is worth being precise
about what was measured rather than assumed.

Verified live 2026-09-18:

    GET https://streaming.assemblyai.com/v3/token?expires_in_seconds=60
        Authorization: <raw key>          # no Bearer, same as the socket
    -> 200 {"token": "...", "expires_in_seconds": 60}

    wss://streaming.assemblyai.com/v3/ws?...&token=<token>
    -> Begin, configuration.model = universal-3-5-pro, with NO Authorization header

So the browser can hold the Arabic socket itself. The consequence is that the
demo needs no long-lived server: static files plus two stateless functions. That
is why this repo can put a real, live, talk-to-it demo on a URL that is up
whether or not any machine of ours is awake.

The parameter name is `expires_in_seconds`. `expires_in` returns 422.

WHAT THIS CANNOT DO, STATED PLAINLY
-----------------------------------
This is a PUBLIC mint. Anyone who can load the demo page can request tokens, and
no amount of cleverness in this file changes that, because any secret shipped to
a browser is not a secret. The controls that are real:

  * a 60 second TTL, so a scraped token is worth a minute of streaming
  * a best-effort per-instance rate limit below, which helps against a casual
    loop and does nothing against a distributed one, because serverless
    instances do not share memory
  * the spend cap on the AssemblyAI account itself, which is the only control
    that actually bounds the bill

Do not read the rate limiter as protection. It is a speed bump, and it is
documented as one so nobody later mistakes it for a guarantee.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from httpjson import send_json

TOKEN_URL = "https://streaming.assemblyai.com/v3/token"
DEFAULT_TTL_S = 60
MAX_TTL_S = 120

# Best-effort, per warm instance only. See the docstring: this is a speed bump.
_RECENT: list = []
RATE_WINDOW_S = 60.0
RATE_MAX = 20


def _rate_ok() -> bool:
    now = time.monotonic()
    cutoff = now - RATE_WINDOW_S
    while _RECENT and _RECENT[0] < cutoff:
        _RECENT.pop(0)
    if len(_RECENT) >= RATE_MAX:
        return False
    _RECENT.append(now)
    return True


def _key() -> str:
    key = (os.environ.get("ASSEMBLYAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "ASSEMBLYAI_API_KEY is not set on this deployment. "
            "Set it as an environment variable; it must never be committed."
        )
    return key


def mint(ttl_s: int = DEFAULT_TTL_S) -> dict:
    ttl = max(10, min(int(ttl_s), MAX_TTL_S))
    req = urllib.request.Request(
        f"{TOKEN_URL}?expires_in_seconds={ttl}",
        headers={"Authorization": _key()},          # no Bearer prefix
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not _rate_ok():
            send_json(self, 429, {
                "error": "rate limited",
                "detail": f"more than {RATE_MAX} tokens in {RATE_WINDOW_S:.0f}s from "
                          f"this instance. Streaming is billed per socket-second, so "
                          f"the demo limits how fast it hands out sockets.",
            })
            return
        try:
            data = mint()
        except urllib.error.HTTPError as exc:
            send_json(self, 502, {"error": "assemblyai rejected the token request",
                             "status": exc.code,
                             "detail": exc.read()[:300].decode("utf-8", "replace")})
            return
        except Exception as exc:                            # noqa: BLE001
            send_json(self, 500, {"error": type(exc).__name__, "detail": str(exc)[:300]})
            return
        send_json(self, 200, {
            "token": data.get("token", ""),
            "expires_in_seconds": data.get("expires_in_seconds", DEFAULT_TTL_S),
            # The browser must not invent these. Arabic exists on exactly one
            # model, and the default model would silently return confident
            # English-shaped nonsense for Arabic speech.
            "speech_model": "universal-3-5-pro",
            "language_codes": ["ar"],
            "encoding": "pcm_s16le",
            "sample_rate": 16000,
            "chunk_ms": 100,
        })
