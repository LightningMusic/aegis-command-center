"""
core/oauth_loopback.py
======================
Shared bits for "sign in with your browser" OAuth flows: PKCE, a random state,
and a one-shot localhost HTTP listener that catches the ``?code=…&state=…``
redirect. Used by both the Brightspace and Google OAuth clients.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse


def pkce_pair() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def random_state(nbytes: int = 16) -> str:
    return secrets.token_urlsafe(nbytes)


class _Handler(BaseHTTPRequestHandler):
    captured: Dict[str, str] = {}

    def do_GET(self):  # noqa: N802
        params = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        if "code" not in params and "error" not in params:
            self.send_response(404)
            self.end_headers()
            return
        type(self).captured = params
        ok = "code" in params and "error" not in params
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            (
                "<html><body style='font-family:system-ui;padding:40px'>"
                "<h2>{}</h2><p>You can close this tab and return to Aegis.</p>"
                "</body></html>"
            )
            .format("Signed in ✓" if ok else "Sign-in failed")
            .encode("utf-8")
        )

    def log_message(self, *_args):
        return


def loopback_capture(
    auth_url: str,
    redirect_uri: str,
    *,
    timeout: int = 180,
    open_browser: Optional[Callable[[str], None]] = None,
) -> Dict[str, str]:
    """Open *auth_url* in the browser and block until the loopback redirect is
    hit (or *timeout* seconds pass). Returns the redirect's query params."""
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1"):
        raise ValueError(
            "Redirect URI must be http://localhost:<port>/… for desktop sign-in."
        )
    port = parsed.port or 80

    _Handler.captured = {}
    try:
        httpd = HTTPServer(("127.0.0.1", port), _Handler)
    except OSError as exc:
        raise RuntimeError(f"Could not open local port {port}: {exc}") from exc

    httpd.timeout = 2
    (open_browser or webbrowser.open)(auth_url)

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline and not _Handler.captured:
            httpd.handle_request()
    finally:
        httpd.server_close()

    if not _Handler.captured:
        raise TimeoutError("Timed out waiting for the browser sign-in to finish.")
    return dict(_Handler.captured)
