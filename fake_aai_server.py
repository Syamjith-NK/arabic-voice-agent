"""A fake AssemblyAI v3 streaming endpoint, so the whole path runs with no key.

It speaks the v3 protocol back at our client: accepts binary mu-law frames, counts
the bytes, and emits each recorded message once enough audio has arrived. It also
honours the `{"type":"Terminate"}` control message by flushing whatever is left.

WHY GATE ON BYTES RATHER THAN A TIMER
    The audio is fed at true 20 ms wall-clock pace, so byte count IS position in the
    utterance. Gating on bytes makes the replay deterministic on a slow machine and
    under CI, while still producing meaningful wall-clock latency numbers, because
    the pacing happens on the sending side where it belongs. A timer-driven fake
    would measure the fake's timer, which is worthless.

WHAT THIS IS NOT
    It is not a simulator of AssemblyAI's accuracy, endpointing, or timing. It
    replays a script. Its only job is to exercise our client's protocol handling,
    callback plumbing and failure paths without spending money or ringing a phone.

FAULT INJECTION
    --drop-after N   close the socket abruptly after N bytes, to exercise reconnect
    --auth-required  reject a connection with no Authorization header
    --slow-ms N      add N ms of latency before every message

Run standalone:
    python3 fake_aai_server.py --port 8799
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from pathlib import Path

from websockets.asyncio.server import serve

HERE = Path(__file__).parent
DEFAULT_SCRIPT = HERE / "fixtures" / "v3_session_arabic.jsonl"


def load_script(path: Path | str = DEFAULT_SCRIPT) -> list[dict]:
    p = Path(path)
    if not p.exists():
        raise SystemExit(
            f"missing protocol fixture: {p}\n"
            "Regenerate it with:  python3 make_fixture.py"
        )
    recs = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{p}:{i} is not valid JSON: {exc}") from exc
    if not recs:
        raise SystemExit(f"{p} has no messages in it")
    return recs


class FakeAAI:
    def __init__(
        self,
        script: list[dict] | None = None,
        *,
        drop_after: int | None = None,
        auth_required: bool = False,
        slow_ms: int = 0,
    ):
        self.script = script if script is not None else load_script()
        self.drop_after = drop_after
        self.auth_required = auth_required
        self.slow_ms = slow_ms
        # Observability for the tests.
        self.sessions = 0
        self.bytes_received = 0
        self.control_messages: list[dict] = []
        self.last_auth_header: str | None = None
        self.sent: list[dict] = []

    async def handler(self, ws):
        self.sessions += 1
        # websockets normalises header access; the client sends `Authorization: <key>`
        # with NO Bearer prefix, and we assert exactly that in the selftest.
        auth = None
        with contextlib.suppress(Exception):
            auth = ws.request.headers.get("Authorization")
        self.last_auth_header = auth
        if self.auth_required and not auth:
            await ws.close(code=4001, reason="Not Authorized")
            return

        sent_flags = [False] * len(self.script)
        received = 0
        dropped = False

        async def emit_due(force: bool = False) -> None:
            for i, rec in enumerate(self.script):
                if sent_flags[i]:
                    continue
                if not force and received < rec.get("after_bytes", 0):
                    continue
                sent_flags[i] = True
                if self.slow_ms:
                    await asyncio.sleep(self.slow_ms / 1000.0)
                payload = rec["message"]
                self.sent.append(payload)
                await ws.send(json.dumps(payload, ensure_ascii=False))

        # `Begin` and anything else gated at 0 bytes goes out immediately, exactly
        # as the real service sends Begin on handshake.
        await emit_due()

        try:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    received += len(raw)
                    self.bytes_received += len(raw)
                    if self.drop_after is not None and received >= self.drop_after and not dropped:
                        dropped = True
                        # Abrupt close, no close frame courtesies - this is the
                        # failure the reconnect path exists for.
                        await ws.close(code=1011, reason="injected drop")
                        return
                    await emit_due()
                else:
                    try:
                        m = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    self.control_messages.append(m)
                    if m.get("type") == "Terminate":
                        # Flush the rest of the script regardless of byte gates.
                        # The real service does the same: Terminate forces the
                        # final turn out rather than waiting on the endpointer.
                        await emit_due(force=True)
                        await ws.close(code=1000, reason="terminated")
                        return
        except Exception:                                  # noqa: BLE001
            # A client hanging up mid-stream is normal, not an error.
            pass


async def run_server(host="127.0.0.1", port=0, **kw):
    """Start the fake. Returns (server, fake). port=0 picks a free port, which is
    what the tests use - a hardcoded port is how test suites collide with whatever
    else is listening on this machine."""
    fake = FakeAAI(**kw)
    server = await serve(fake.handler, host, port)
    return server, fake


def server_url(server, path: str = "/v3/ws") -> str:
    sock = next(iter(server.sockets))
    host, port = sock.getsockname()[:2]
    return f"ws://{host}:{port}{path}"


async def _main(args):
    server, fake = await run_server(
        args.host, args.port,
        drop_after=args.drop_after,
        auth_required=args.auth_required,
        slow_ms=args.slow_ms,
    )
    print(f"fake AssemblyAI v3 listening on {server_url(server)}")
    print(f"  script: {len(fake.script)} messages from {DEFAULT_SCRIPT.name}")
    print("  ctrl-c to stop")
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        pass
    finally:
        server.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--drop-after", type=int, default=None,
                    help="close the socket after N bytes of audio (tests reconnect)")
    ap.add_argument("--auth-required", action="store_true",
                    help="reject connections with no Authorization header")
    ap.add_argument("--slow-ms", type=int, default=0,
                    help="delay every message by N ms")
    a = ap.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main(a))
