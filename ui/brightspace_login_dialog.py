"""
ui/brightspace_login_dialog.py
==============================
Embedded Brightspace sign-in. Loads the real Brightspace login in a QtWebEngine
view; the user signs in with StarID + MFA as usual. Once a Brightspace app page
loads, we grab the session cookies and the anti-CSRF token and hand them back.

Requires the optional ``PyQt6-WebEngine`` package. Callers should import this
module lazily and show a friendly message if the import fails.
"""

from __future__ import annotations

from typing import Dict, Optional

from PyQt6.QtCore import QTimer, QUrl, Qt
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)
from PyQt6.QtWebEngineCore import QWebEngineProfile
from PyQt6.QtWebEngineWidgets import QWebEngineView

_WANTED_COOKIES = ("d2lSessionVal", "d2lSecureSessionVal")

_XSRF_JS = (
    "(function(){try{"
    "var t=window.localStorage.getItem('XSRF.Token');"
    "if(t)return t;"
    "if(window.D2L&&D2L.LP&&D2L.LP.Web&&D2L.LP.Web.Authentication&&"
    "D2L.LP.Web.Authentication.Xsrf)return D2L.LP.Web.Authentication.Xsrf.GetXsrfToken();"
    "}catch(e){}return '';})()"
)


class BrightspaceLoginDialog(QDialog):
    def __init__(self, lms_url: str, cache_dir: str, parent=None) -> None:
        super().__init__(parent)
        self.lms_url = (lms_url or "").rstrip("/")
        self.result_data: Optional[Dict[str, object]] = None
        self._cookies: Dict[str, str] = {}
        self._capturing = False

        self.setWindowTitle("Sign in to Brightspace")
        self.resize(560, 760)

        layout = QVBoxLayout(self)
        info = QLabel(
            "Sign in with your StarID exactly as you would on the website. "
            "This window closes on its own once your Brightspace home page loads."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.profile = QWebEngineProfile("aegis-brightspace", self)
        if cache_dir:
            self.profile.setPersistentStoragePath(cache_dir)
            self.profile.setCachePath(cache_dir)
            self.profile.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
            )
        self.profile.cookieStore().cookieAdded.connect(self._on_cookie)

        self.view = QWebEngineView(self.profile, self)
        self.view.loadFinished.connect(self._on_load_finished)
        self.view.setUrl(QUrl(f"{self.lms_url}/d2l/home"))
        layout.addWidget(self.view, 1)

        self.status = QLabel("Loading Brightspace…")
        self.status.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.capture_btn = QPushButton("I'm signed in — capture")
        self.capture_btn.clicked.connect(lambda: self._try_capture(force=True))
        row.addWidget(self.capture_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        row.addWidget(cancel_btn)
        row.addStretch()
        layout.addLayout(row)

    # ── cookie / page tracking ──────────────────────────────────────────
    def _on_cookie(self, cookie) -> None:
        try:
            name = bytes(cookie.name()).decode("ascii", "ignore")
        except Exception:
            return
        if name in _WANTED_COOKIES:
            self._cookies[name] = bytes(cookie.value()).decode("ascii", "ignore")

    def _on_load_finished(self, ok: bool) -> None:
        url = self.view.url().toString()
        if not ok:
            self.status.setText("Page failed to load — check your connection.")
            return
        if self._is_app_page(url):
            self.status.setText("Signed in — capturing session…")
            QTimer.singleShot(600, self._try_capture)
        else:
            self.status.setText("Waiting for you to finish signing in…")

    def _is_app_page(self, url: str) -> bool:
        if not url.startswith(f"{self.lms_url}/d2l/"):
            return False
        lowered = url.lower()
        return not any(
            marker in lowered
            for marker in ("/d2l/lp/auth/login", "/d2l/login", "saml", "/oauth", "processloginactions")
        )

    # ── capture ─────────────────────────────────────────────────────────
    def _try_capture(self, force: bool = False) -> None:
        if self._capturing:
            return
        if not force and not self._is_app_page(self.view.url().toString()):
            return
        if len(self._cookies) < 1 or "d2lSessionVal" not in self._cookies:
            if force:
                self.status.setText(
                    "Couldn't read the session cookie yet — finish loading your "
                    "home page, then click capture again."
                )
            return
        self._capturing = True
        self.view.page().runJavaScript(_XSRF_JS, self._got_xsrf)

    def _got_xsrf(self, token) -> None:
        self.result_data = {
            "cookies": dict(self._cookies),
            "xsrf": token or "",
            "lms_url": self.lms_url,
        }
        self.accept()
