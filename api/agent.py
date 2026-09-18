"""One agent turn, statelessly, over HTTP.

The browser holds the AssemblyAI socket itself (see `api/token.py`), so the only
thing left for a server to do is decide what to say back. That is a pure
function of the caller's committed sentence plus the conversation so far, which
makes it a natural serverless endpoint and means the demo has no process to keep
alive and no machine to expose.

    POST /api/agent
    {"text": "<the committed Arabic transcript>", "state": {...}}

    200
    {"reply": "...", "slots": {...}, "done": false, "used_llm": false,
     "ms": 1.2, "extracted": {...}, "state": {...}}

THE STATE COMES FROM A STRANGER'S BROWSER. It is echoed back to the client each
turn and handed straight back to us the next turn, so it is untrusted input in
the ordinary sense: `load_state` must ignore unknown keys, refuse wrong types
and degrade to a fresh conversation rather than raise. There is no session store
on purpose, because a session store is a thing that has to be paid for, secured
and cleaned up, and this conversation is worth neither.

`llm=None` here deliberately. The rules path answers in single-digit
milliseconds; the local model this repo can call lives on a Mac on a tailnet and
is not reachable from a serverless function, and a hosted LLM would put a
metered dependency in the hot path of a public demo. An utterance the rules do
not cover gets a scripted Arabic clarification, which is a worse answer than a
model would give and an honest one.
"""
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from httpjson import send_json

try:
    from agent import BookingAgent
    IMPORT_ERROR = ""
except Exception as exc:                                    # noqa: BLE001
    BookingAgent = None
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

MAX_TEXT = 2000
MAX_BODY = 64 * 1024


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if BookingAgent is None:
            send_json(self, 503, {"error": "agent unavailable", "detail": IMPORT_ERROR})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            send_json(self, 413, {"error": "body too large"})
            return

        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
        except Exception as exc:                            # noqa: BLE001
            send_json(self, 400, {"error": "bad json", "detail": str(exc)[:200]})
            return

        text = payload.get("text")
        state = payload.get("state")
        if not isinstance(state, dict):
            state = {}
        if text is not None and not isinstance(text, str):
            send_json(self, 400, {"error": "text must be a string"})
            return
        if isinstance(text, str) and len(text) > MAX_TEXT:
            text = text[:MAX_TEXT]

        try:
            agent = BookingAgent(llm=None)
            if state:
                agent.load_state(state)
            # No text means "open the conversation".
            reply = agent.greet() if not (text or "").strip() else agent.handle(text)
            send_json(self, 200, {
                "reply": reply.text,
                "slots": reply.slots,
                "done": reply.done,
                "used_llm": reply.used_llm,
                "ms": round(reply.ms, 3),
                "extracted": reply.debug,
                "state": agent.export_state(),
            })
        except Exception as exc:                            # noqa: BLE001
            # A crash here must not return corrupted or half-formed Arabic. Say
            # nothing in Arabic rather than say something wrong in it.
            send_json(self, 500, {
                "error": type(exc).__name__,
                "detail": str(exc)[:300],
                "trace": traceback.format_exc()[-800:] if os.environ.get("DEBUG") else "",
            })

    def do_GET(self) -> None:
        send_json(self, 200, {
            "ok": BookingAgent is not None,
            "detail": IMPORT_ERROR or "post {text, state} to take one turn",
        })
