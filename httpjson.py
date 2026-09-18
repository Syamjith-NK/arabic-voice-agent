"""One JSON response writer, shared by the serverless functions and the local runner.

Small on purpose, and separate on purpose. Both `api/token.py` and
`api/agent.py` need to write a JSON response, and `api_local.py` dispatches into
those handlers from its own request class. If each of the three carried its own
copy, the local runner would be exercising a different response path from the
deployed one, which defeats the reason the local runner imports the real
handlers in the first place.

`ensure_ascii=False` matters here rather than being a style choice: these
responses carry Arabic, and escaping it to `\\uXXXX` would triple the payload
and make every response body unreadable while debugging.
"""
import json


def send_json(h, code: int, payload: dict) -> None:
    """Write `payload` as a JSON response on the BaseHTTPRequestHandler `h`."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(body)
