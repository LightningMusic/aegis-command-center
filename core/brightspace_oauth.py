"""
core/brightspace_oauth.py
=========================
OAuth 2.0 (authorization-code + PKCE) client for D2L Brightspace.

Flow, once:
  * You register an app under Brightspace  Account Settings ->
    "Manage applications registered with OAuth 2.0" with the redirect URI and
    scope shown in Aegis Settings, and paste the Client ID / Client Secret.
  * "Connect to Brightspace" opens your browser to the D2L sign-in (StarID SSO
    + MFA happen there, in a real browser), you approve, and D2L redirects to a
    tiny localhost listener Aegis runs. Aegis swaps the code for tokens.
  * The refresh token is stored encrypted; from then on Aegis refreshes access
    tokens silently.

D2L auth server (global, correct for *.learn.minnstate.edu):
  authorize : https://auth.brightspace.com/oauth2/auth
  token     : https://auth.brightspace.com/core/connect/token
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core.oauth_loopback import loopback_capture, pkce_pair, random_state

AUTHORIZE_URL = "https://auth.brightspace.com/oauth2/auth"
TOKEN_URL = "https://auth.brightspace.com/core/connect/token"

DEFAULT_SCOPE = "calendar:my_events:read enrollment:own_enrollment:read core:*:*"
DEFAULT_REDIRECT = "http://localhost:8713/callback"

_CLIENT_SECRET_KEY = "brightspace_oauth_client_secret"
_REFRESH_TOKEN_KEY = "brightspace_oauth_refresh_token"


class BrightspaceOAuthError(RuntimeError):
    pass


class BrightspaceOAuth:
    def __init__(self, config_manager, secure_store) -> None:
        self.config_manager = config_manager
        self.secure_store = secure_store
        self._access_token: Optional[str] = None
        self._expires_at = 0.0

    # ── config helpers ───────────────────────────────────────────────────
    def _cfg(self) -> Dict[str, Any]:
        return self.config_manager.get_brightspace_settings()

    def lms_url(self) -> str:
        return (self._cfg().get("lms_url") or "").rstrip("/")

    def client_id(self) -> str:
        return (self._cfg().get("oauth_client_id") or "").strip()

    def redirect_uri(self) -> str:
        return (self._cfg().get("oauth_redirect_uri") or DEFAULT_REDIRECT).strip()

    def scope(self) -> str:
        return (self._cfg().get("oauth_scope") or DEFAULT_SCOPE).strip()

    def client_secret(self) -> str:
        try:
            return self.secure_store.get(_CLIENT_SECRET_KEY) or ""
        except Exception:
            return ""

    def set_client_secret(self, value: str) -> None:
        value = (value or "").strip()
        if value:
            self.secure_store.set(_CLIENT_SECRET_KEY, value)
        else:
            self.secure_store.delete(_CLIENT_SECRET_KEY)

    def _refresh_token(self) -> Optional[str]:
        try:
            return self.secure_store.get(_REFRESH_TOKEN_KEY)
        except Exception:
            return None

    def is_configured(self) -> bool:
        return bool(self.lms_url() and self.client_id())

    def is_connected(self) -> bool:
        return self.is_configured() and bool(self._refresh_token())

    def disconnect(self) -> None:
        self.secure_store.delete(_REFRESH_TOKEN_KEY)
        self._access_token = None
        self._expires_at = 0.0
        self.config_manager.update_brightspace_settings({"oauth_connected": False})

    # ── interactive login ────────────────────────────────────────────────
    def authorization_url(self, state: str, challenge: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id(),
            "redirect_uri": self.redirect_uri(),
            "scope": self.scope(),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def run_interactive_login(self, timeout: int = 180, open_browser=None) -> Tuple[bool, str]:
        """Blocking. Run this on a worker thread."""
        if not self.is_configured():
            return False, "Set the Brightspace URL and OAuth Client ID first."

        verifier, challenge = pkce_pair()
        state = random_state()
        try:
            result = loopback_capture(
                self.authorization_url(state, challenge),
                self.redirect_uri(),
                timeout=timeout,
                open_browser=open_browser,
            )
        except (ValueError, RuntimeError, TimeoutError) as exc:
            return False, str(exc)

        if result.get("error"):
            return False, f"Brightspace returned: {result.get('error_description') or result['error']}"
        if result.get("state") != state:
            return False, "Sign-in state mismatch — please try again."

        code = result.get("code")
        if not code:
            return False, "No authorization code was returned."

        try:
            self._exchange_code(code, verifier)
        except BrightspaceOAuthError as exc:
            return False, str(exc)

        self.config_manager.update_brightspace_settings({"oauth_connected": True})
        return True, "Connected to Brightspace."

    # ── token endpoint ───────────────────────────────────────────────────
    def _post_token(self, data: Dict[str, str]) -> Dict[str, Any]:
        secret = self.client_secret()
        if secret:
            data = dict(data, client_secret=secret)
        body = urlencode(data).encode("utf-8")
        req = Request(
            TOKEN_URL,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error_description", "")
            except Exception:
                pass
            raise BrightspaceOAuthError(
                f"Token request failed (HTTP {exc.code}). {detail}".strip()
            ) from exc
        except URLError as exc:
            raise BrightspaceOAuthError(f"Could not reach the D2L auth server: {exc.reason}") from exc

    def _store_tokens(self, payload: Dict[str, Any]) -> None:
        access = payload.get("access_token")
        if not access:
            raise BrightspaceOAuthError("Auth server did not return an access token.")
        self._access_token = access
        self._expires_at = time.monotonic() + int(payload.get("expires_in", 1800))
        # D2L rotates the refresh token on every use — always persist the newest.
        refresh = payload.get("refresh_token")
        if refresh:
            self.secure_store.set(_REFRESH_TOKEN_KEY, refresh)

    def _exchange_code(self, code: str, verifier: str) -> None:
        payload = self._post_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri(),
                "client_id": self.client_id(),
                "code_verifier": verifier,
            }
        )
        self._store_tokens(payload)

    def _refresh(self) -> None:
        refresh = self._refresh_token()
        if not refresh:
            raise BrightspaceOAuthError("Not connected to Brightspace — sign in first.")
        payload = self._post_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": self.client_id(),
                "scope": self.scope(),
            }
        )
        self._store_tokens(payload)

    def _token(self, force: bool = False) -> str:
        if force or not self._access_token or time.monotonic() >= self._expires_at - 60:
            self._refresh()
        assert self._access_token
        return self._access_token

    # ── REST helper ──────────────────────────────────────────────────────
    def api_get(self, route: str, params: Optional[Dict[str, Any]] = None) -> Any:
        base = self.lms_url()
        if not base:
            raise BrightspaceOAuthError("Brightspace URL is not set.")
        url = base + route
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})

        for attempt in (1, 2):
            token = self._token(force=(attempt == 2))
            req = Request(url, headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            })
            try:
                with urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code == 401 and attempt == 1:
                    continue
                raise BrightspaceOAuthError(
                    f"Brightspace API {route} failed (HTTP {exc.code})."
                ) from exc
            except URLError as exc:
                raise BrightspaceOAuthError(f"Could not reach Brightspace: {exc.reason}") from exc
        raise BrightspaceOAuthError("Brightspace API call failed after re-authenticating.")

    # ── convenience ──────────────────────────────────────────────────────
    def course_org_unit_ids(self, lp_version: str = "1.43") -> List[int]:
        ids: List[int] = []
        bookmark = None
        route = f"/d2l/api/lp/{lp_version}/enrollments/myenrollments/"
        while True:
            page = self.api_get(route, {"orgUnitTypeId": 3, "bookmark": bookmark})
            items = page.get("Items", []) if isinstance(page, dict) else []
            for entry in items:
                org = (entry or {}).get("OrgUnit") or {}
                oid = org.get("Id")
                if oid is not None:
                    ids.append(int(oid))
            paging = page.get("PagingInfo", {}) if isinstance(page, dict) else {}
            if paging.get("HasMoreItems") and paging.get("Bookmark"):
                bookmark = paging["Bookmark"]
            else:
                break
        return ids
