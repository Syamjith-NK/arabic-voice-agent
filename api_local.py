"""Run the SERVERLESS shape of the demo locally, with no deploy and no Vercel.

The hosted demo is static files plus two stateless functions: `api/token.py`
mints a 60 second AssemblyAI streaming token, `api/agent.py` takes one
conversational turn. This file serves exactly those three things from stdlib
`http.server`, using the SAME handler classes Vercel imports.

That last part is the point. A local mock of a deployment is a second
implementation that drifts from the real one and hides the bug you deployed. By
importing `api.token.handler` and `api.agent.handler` rather than reimplementing
them, anything that works here is the same code path that runs in production,
and anything broken here is broken there.

    python3 api_local.py                # http://127.0.0.1:8812/
    python3 api_local.py --check        # exercise both endpoints, exit non-zero on failure

`--check` is the honest test: it does a real token mint against the live service
(a few hundred milliseconds, no socket opened, so effectively free) and a real
agent turn, and prints what came back.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"

sys.path.insert(0, str(HERE))

# Vercel injects the key as an env var. Locally it lives in the credentials
# file, so bridge the two here and nowhere else, so that no module has to know
# about both.
if not os.environ.get("ASSEMBLYAI_API_KEY"):
    keyfile = Path.home() / "jarvis" / ".credentials" / "assemblyai_key"
    if keyfile.exists():
        os.environ["ASSEMBLYAI_API_KEY"] = keyfile.read_text(encoding="utf-8").strip()

from api import agent as agent_fn                   # noqa: E402
from api import token as token_fn                   # noqa: E402


class Router(BaseHTTPRequestHandler):
    """Dispatch to the real function handlers, or serve a static file."""

    server_version = "arabic-voice-agent-local"

    def _delegate(self, mod, method: str) -> None:
        # The function handlers are BaseHTTPRequestHandler subclasses. Rather
        # than instantiate them (which would try to read the socket again), bind
        # the method to this live request. Same code, same request, one socket.
        fn = getattr(mod.handler, method, None)
        if fn is None:
            self.send_error(405)
            return
        fn(self)

    def _static(self) -> None:
        rel = self.path.split("?")[0].lstrip("/") or "index.html"
        if ".." in rel:
            self.send_error(403)
            return
        f = WEB / rel
        if not f.is_file():
            self.send_error(404, f"no {rel} under {WEB}")
            return
        import mimetypes
        body = f.read_bytes()
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        p = self.path.split("?")[0]
        if p == "/api/token":
            self._delegate(token_fn, "do_GET")
        elif p == "/api/agent":
            self._delegate(agent_fn, "do_GET")
        else:
            self._static()

    def do_POST(self) -> None:
        if self.path.split("?")[0] == "/api/agent":
            self._delegate(agent_fn, "do_POST")
        else:
            self.send_error(404)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % args))


def check(port: int) -> int:
    """Exercise both endpoints for real. No mocks, no assumptions."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), Router)
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    failures = []

    def show(label: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'ok ' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    # 1. token mint, live
    try:
        with urllib.request.urlopen(f"{base}/api/token", timeout=20) as r:
            tok = json.loads(r.read())
        good = bool(tok.get("token")) and tok.get("speech_model") == "universal-3-5-pro"
        show("token mint", good,
             f"ttl={tok.get('expires_in_seconds')}s model={tok.get('speech_model')} "
             f"{tok.get('sample_rate')}Hz {tok.get('encoding')}")
    except Exception as exc:                                # noqa: BLE001
        show("token mint", False, repr(exc))

    # 2. agent health
    try:
        with urllib.request.urlopen(f"{base}/api/agent", timeout=10) as r:
            h = json.loads(r.read())
        show("agent import", bool(h.get("ok")), h.get("detail", ""))
        agent_ready = bool(h.get("ok"))
    except Exception as exc:                                # noqa: BLE001
        show("agent import", False, repr(exc))
        agent_ready = False

    # 3. a real turn, and a second turn resumed from the returned state
    if agent_ready:
        try:
            req = urllib.request.Request(
                f"{base}/api/agent",
                data=json.dumps({"text": "", "state": {}}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                first = json.loads(r.read())
            print(f"         agent says: {first.get('reply','')}")
            show("opening turn", bool(first.get("reply")), f"{first.get('ms')} ms")

            follow = "أبغى تصوير فيديو بكرة الساعة تسعة صباحاً"
            req = urllib.request.Request(
                f"{base}/api/agent",
                data=json.dumps({"text": follow, "state": first.get("state", {})},
                                ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                second = json.loads(r.read())
            print(f"         caller:     {follow}")
            print(f"         agent says: {second.get('reply','')}")
            print(f"         slots:      {json.dumps(second.get('slots', {}), ensure_ascii=False)}")
            show("stateless resume", bool(second.get("reply")),
                 f"{second.get('ms')} ms, used_llm={second.get('used_llm')}")
        except Exception as exc:                            # noqa: BLE001
            show("agent turn", False, repr(exc))

        # 4. a hostile state must not crash the function
        try:
            req = urllib.request.Request(
                f"{base}/api/agent",
                data=json.dumps({"text": "مرحبا", "state": {"slots": "not-a-dict",
                                                            "junk": [1, 2, 3]}}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                hostile = json.loads(r.read())
            show("malformed state degrades, does not 500", bool(hostile.get("reply")))
        except Exception as exc:                            # noqa: BLE001
            show("malformed state degrades, does not 500", False, repr(exc))

    srv.shutdown()
    print()
    if failures:
        print(f"  {len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("  PASS")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8812)
    p.add_argument("--check", action="store_true")
    args = p.parse_args()

    if args.check:
        raise SystemExit(check(args.port))

    if not WEB.is_dir():
        print(f"  note: {WEB} does not exist yet, so only /api/* will answer")
    print(f"  http://127.0.0.1:{args.port}/            the demo")
    print(f"  http://127.0.0.1:{args.port}/api/token   mints a 60s AssemblyAI token")
    print(f"  http://127.0.0.1:{args.port}/api/agent   one conversational turn")
    ThreadingHTTPServer(("127.0.0.1", args.port), Router).serve_forever()


if __name__ == "__main__":
    main()
