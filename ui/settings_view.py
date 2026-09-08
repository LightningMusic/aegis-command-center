from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.brightspace import BrightspaceError


class _BrightspaceTestWorker(QObject):
    done = pyqtSignal(bool, str, bool)  # ok, message, ran_sync

    def __init__(self, client, sync, run_sync: bool) -> None:
        super().__init__()
        self.client = client
        self.sync = sync
        self.run_sync = run_sync

    def run(self) -> None:
        ok, message = self.client.test_connection()
        ran_sync = False
        if ok and self.run_sync:
            try:
                s_ok, s_msg, _ = self.sync.run()
                ran_sync = True
                ok, message = s_ok, s_msg
            except Exception as exc:  # noqa: BLE001
                ok, message = False, str(exc)
        self.done.emit(ok, message, ran_sync)


class _OAuthConnectWorker(QObject):
    done = pyqtSignal(bool, str)

    def __init__(self, oauth) -> None:
        super().__init__()
        self.oauth = oauth

    def run(self) -> None:
        try:
            ok, message = self.oauth.run_interactive_login()
        except Exception as exc:  # noqa: BLE001
            ok, message = False, str(exc)
        self.done.emit(ok, message)


class SettingsView(QWidget):
    settings_saved = pyqtSignal()
    brightspace_synced = pyqtSignal()

    def __init__(self, config_manager, brightspace_client, brightspace_sync,
                 webcache_dir="", google_oauth=None):
        super().__init__()

        self.config_manager = config_manager
        self.brightspace_client = brightspace_client
        self.brightspace_sync = brightspace_sync
        self.oauth = brightspace_client.oauth
        self.session = brightspace_client.session
        self.google_oauth = google_oauth
        self.webcache_dir = webcache_dir
        self._test_thread = None
        self._test_worker = None
        self._oauth_thread = None
        self._oauth_worker = None
        self._google_thread = None
        self._google_worker = None
        self._thread_pairs = (
            ("_test_thread", "_test_worker"),
            ("_oauth_thread", "_oauth_worker"),
            ("_google_thread", "_google_worker"),
        )

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)
        self.setLayout(layout)

        title = QLabel("Settings")
        title.setStyleSheet("font-size: 24px; font-weight: 600;")
        layout.addWidget(title)

        layout.addWidget(self._build_brightspace_group())
        layout.addWidget(self._build_google_group())
        layout.addWidget(self._build_outlook_group())

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        button_row = QHBoxLayout()
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self.save_settings)
        button_row.addWidget(self.save_button)

        self.test_button = QPushButton("Test && Sync Brightspace")
        self.test_button.clicked.connect(self.test_connection)
        button_row.addWidget(self.test_button)

        button_row.addStretch()
        layout.addLayout(button_row)
        layout.addStretch()

        self.load_settings()

    # ── group builders ───────────────────────────────────────────────────
    def _build_brightspace_group(self) -> QGroupBox:
        group = QGroupBox("Brightspace calendar sync")
        outer = QVBoxLayout()
        group.setLayout(outer)

        self.enabled_box = QCheckBox("Sync Brightspace due dates into Tasks and Calendar")
        outer.addWidget(self.enabled_box)

        # ---- Browser sign-in (recommended) ---------------------------
        session_box = QGroupBox("Sign in with your browser  (recommended)")
        session_layout = QVBoxLayout()
        session_box.setLayout(session_layout)

        session_help = QLabel(
            "Opens a Brightspace sign-in window. Log in with your StarID + MFA "
            "as usual; Aegis keeps the session (encrypted) and reads your calendar "
            "through Brightspace's own API. No IT setup needed. You'll re-sign-in "
            "when the session eventually expires."
        )
        session_help.setWordWrap(True)
        session_help.setStyleSheet("color: #888; font-size: 12px;")
        session_layout.addWidget(session_help)

        self.session_lms_url_input = QLineEdit()
        self.session_lms_url_input.setPlaceholderText("https://alextech.learn.minnstate.edu")
        url_row = QFormLayout()
        url_row.addRow("Brightspace URL", self.session_lms_url_input)
        session_layout.addLayout(url_row)

        session_buttons = QHBoxLayout()
        self.signin_button = QPushButton("Sign in to Brightspace")
        self.signin_button.clicked.connect(self.sign_in_brightspace)
        session_buttons.addWidget(self.signin_button)
        self.signout_button = QPushButton("Sign out")
        self.signout_button.clicked.connect(self.sign_out_brightspace)
        session_buttons.addWidget(self.signout_button)
        session_buttons.addStretch()
        session_layout.addLayout(session_buttons)

        self.session_state_label = QLabel()
        self.session_state_label.setStyleSheet("color: #888; font-size: 12px;")
        session_layout.addWidget(self.session_state_label)

        outer.addWidget(session_box)

        # ---- OAuth 2.0 (advanced) -------------------------------------
        oauth_box = QGroupBox("Advanced: OAuth 2.0 client (needs a D2L admin)")
        form = QFormLayout()
        oauth_box.setLayout(form)

        help_text = QLabel(
            "Only if a Brightspace administrator registers an OAuth 2.0 client for "
            "you. Set its <b>Redirect URI</b> and <b>Scope</b> to the values below, "
            "then paste the Client ID / Secret. Stored encrypted."
        )
        help_text.setWordWrap(True)
        help_text.setStyleSheet("color: #888; font-size: 12px;")
        form.addRow(help_text)

        self.lms_url_input = QLineEdit()
        self.lms_url_input.setPlaceholderText("https://alextech.learn.minnstate.edu")
        form.addRow("Brightspace URL", self.lms_url_input)

        self.oauth_client_id_input = QLineEdit()
        self.oauth_client_id_input.setPlaceholderText("OAuth Client ID")
        form.addRow("Client ID", self.oauth_client_id_input)

        self.oauth_client_secret_input = QLineEdit()
        self.oauth_client_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.oauth_client_secret_input.setPlaceholderText("OAuth Client Secret")
        form.addRow("Client Secret", self.oauth_client_secret_input)

        self.oauth_redirect_input = QLineEdit()
        self.oauth_redirect_input.setPlaceholderText("http://localhost:8713/callback")
        form.addRow("Redirect URI", self.oauth_redirect_input)

        self.oauth_scope_input = QLineEdit()
        form.addRow("Scope", self.oauth_scope_input)

        oauth_buttons = QHBoxLayout()
        self.connect_button = QPushButton("Connect to Brightspace")
        self.connect_button.clicked.connect(self.connect_oauth)
        oauth_buttons.addWidget(self.connect_button)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.clicked.connect(self.disconnect_oauth)
        oauth_buttons.addWidget(self.disconnect_button)
        oauth_buttons.addStretch()
        form.addRow(oauth_buttons)

        self.oauth_state_label = QLabel()
        self.oauth_state_label.setStyleSheet("color: #888; font-size: 12px;")
        form.addRow(self.oauth_state_label)

        outer.addWidget(oauth_box)

        # ---- iCal feed (fallback) -------------------------------------
        feed_box = QGroupBox("Or: personal calendar feed URL")
        feed_form = QFormLayout()
        feed_box.setLayout(feed_form)

        feed_help = QLabel(
            "If you can find <b>Calendar → Subscribe</b> in Brightspace, paste the "
            "<code>…/feed.ics?token=…</code> URL here instead. Used only when OAuth "
            "is not connected."
        )
        feed_help.setWordWrap(True)
        feed_help.setStyleSheet("color: #888; font-size: 12px;")
        feed_form.addRow(feed_help)

        self.feed_url_input = QLineEdit()
        self.feed_url_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.feed_url_input.setPlaceholderText(
            "https://alextech.learn.minnstate.edu/d2l/le/calendar/feed/user/feed.ics?token=…"
        )
        feed_form.addRow("Feed URL", self.feed_url_input)

        feed_buttons = QHBoxLayout()
        self.show_feed_button = QPushButton("Show")
        self.show_feed_button.setCheckable(True)
        self.show_feed_button.toggled.connect(self._toggle_feed_visibility)
        feed_buttons.addWidget(self.show_feed_button)
        self.clear_feed_button = QPushButton("Forget saved URL")
        self.clear_feed_button.clicked.connect(self._forget_feed)
        feed_buttons.addWidget(self.clear_feed_button)
        feed_buttons.addStretch()
        feed_form.addRow(feed_buttons)

        self.feed_state_label = QLabel()
        self.feed_state_label.setStyleSheet("color: #888; font-size: 12px;")
        feed_form.addRow(self.feed_state_label)

        outer.addWidget(feed_box)
        return group

    def _build_google_group(self) -> QGroupBox:
        group = QGroupBox("Google Calendar")
        form = QFormLayout()
        group.setLayout(form)

        self.google_enabled_box = QCheckBox("Also push due items to Google Calendar")
        form.addRow(self.google_enabled_box)

        google_help = QLabel(
            "One-time setup: in Google Cloud Console create an OAuth client of type "
            "<b>Desktop app</b>, enable the Calendar API, and paste the Client ID / "
            "Secret below. Then click <b>Sign in with Google</b> — your browser opens "
            "the consent screen and Aegis stores the token encrypted."
        )
        google_help.setWordWrap(True)
        google_help.setStyleSheet("color: #888; font-size: 12px;")
        form.addRow(google_help)

        self.google_client_id_input = QLineEdit()
        self.google_client_id_input.setPlaceholderText("OAuth client ID")
        form.addRow("Client ID", self.google_client_id_input)

        self.google_client_secret_input = QLineEdit()
        self.google_client_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.google_client_secret_input.setPlaceholderText("OAuth client secret")
        form.addRow("Client Secret", self.google_client_secret_input)

        self.google_calendar_id_input = QLineEdit()
        self.google_calendar_id_input.setPlaceholderText("primary")
        form.addRow("Calendar ID", self.google_calendar_id_input)

        google_buttons = QHBoxLayout()
        self.google_signin_button = QPushButton("Sign in with Google")
        self.google_signin_button.clicked.connect(self.connect_google)
        google_buttons.addWidget(self.google_signin_button)
        self.google_signout_button = QPushButton("Sign out")
        self.google_signout_button.clicked.connect(self.disconnect_google)
        google_buttons.addWidget(self.google_signout_button)
        google_buttons.addStretch()
        form.addRow(google_buttons)

        self.google_state_label = QLabel()
        self.google_state_label.setStyleSheet("color: #888; font-size: 12px;")
        form.addRow(self.google_state_label)

        # kept for compatibility with older configs / manual entry
        self.google_email_input = QLineEdit()
        self.google_refresh_token_input = QLineEdit()

        return group

    def _build_outlook_group(self) -> QGroupBox:
        group = QGroupBox("Outlook")
        form = QFormLayout()
        group.setLayout(form)

        self.outlook_enabled_box = QCheckBox("Enable Outlook integration")
        form.addRow(self.outlook_enabled_box)

        self.outlook_email_input = QLineEdit()
        self.outlook_email_input.setPlaceholderText("name@outlook.com or Microsoft 365 account")
        form.addRow("Account Email", self.outlook_email_input)

        self.outlook_tenant_input = QLineEdit()
        self.outlook_tenant_input.setPlaceholderText("Tenant ID or common")
        form.addRow("Tenant ID", self.outlook_tenant_input)

        self.outlook_client_id_input = QLineEdit()
        self.outlook_client_id_input.setPlaceholderText("Microsoft app client ID")
        form.addRow("Client ID", self.outlook_client_id_input)

        self.outlook_client_secret_input = QLineEdit()
        self.outlook_client_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.outlook_client_secret_input.setPlaceholderText("Microsoft app client secret")
        form.addRow("Client Secret", self.outlook_client_secret_input)

        self.outlook_refresh_token_input = QLineEdit()
        self.outlook_refresh_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.outlook_refresh_token_input.setPlaceholderText("Refresh token")
        form.addRow("Refresh Token", self.outlook_refresh_token_input)

        return group

    # ── load / collect ───────────────────────────────────────────────────
    def load_settings(self):
        settings = self.config_manager.get_brightspace_settings()
        self.enabled_box.setChecked(bool(settings.get("enabled")))

        lms_url = settings.get("lms_url", "")
        self.session_lms_url_input.setText(lms_url)
        self.lms_url_input.setText(lms_url)
        self.oauth_client_id_input.setText(settings.get("oauth_client_id", ""))
        self.oauth_client_secret_input.clear()
        self.oauth_redirect_input.setText(
            settings.get("oauth_redirect_uri", "http://localhost:8713/callback")
        )
        self.oauth_scope_input.setText(settings.get("oauth_scope", ""))

        self.feed_url_input.clear()
        self._refresh_feed_state()
        self._refresh_oauth_state()
        self._refresh_session_state()
        self._set_status(
            settings.get("last_sync_status", "Not connected"),
            settings.get("last_sync_at", ""),
        )

        google = self.config_manager.get_google_workspace_settings()
        self.google_enabled_box.setChecked(bool(google.get("enabled")))
        self.google_email_input.setText(google.get("account_email", ""))
        self.google_client_id_input.setText(google.get("client_id", ""))
        self.google_client_secret_input.clear()
        self.google_refresh_token_input.setText(google.get("refresh_token", ""))
        self.google_calendar_id_input.setText(google.get("calendar_id", "primary"))
        self._refresh_google_state()

        outlook = self.config_manager.get_outlook_settings()
        self.outlook_enabled_box.setChecked(bool(outlook.get("enabled")))
        self.outlook_email_input.setText(outlook.get("account_email", ""))
        self.outlook_tenant_input.setText(outlook.get("tenant_id", ""))
        self.outlook_client_id_input.setText(outlook.get("client_id", ""))
        self.outlook_client_secret_input.setText(outlook.get("client_secret", ""))
        self.outlook_refresh_token_input.setText(outlook.get("refresh_token", ""))

    def _refresh_feed_state(self):
        settings = self.config_manager.get_brightspace_settings()
        if self.brightspace_client.has_feed():
            host = settings.get("feed_host") or "saved"
            self.feed_state_label.setText(
                f"Feed URL saved for {host}. Paste a new URL above to replace it."
            )
        else:
            self.feed_state_label.setText("No feed URL saved.")

    def _refresh_oauth_state(self):
        if self.oauth is None:
            self.oauth_state_label.setText("OAuth unavailable.")
            self.connect_button.setEnabled(False)
            self.disconnect_button.setEnabled(False)
            return
        connected = self.oauth.is_connected()
        settings = self.config_manager.get_brightspace_settings()
        if connected:
            self.oauth_state_label.setText(
                f"Connected via OAuth · {settings.get('last_item_count', 0)} item(s) at last sync."
            )
        elif self.oauth.is_configured():
            self.oauth_state_label.setText("Configured — click Connect to sign in.")
        else:
            self.oauth_state_label.setText("Not configured.")
        self.disconnect_button.setEnabled(connected)

    def _refresh_google_state(self):
        if self.google_oauth is None:
            self.google_state_label.setText("Google sign-in unavailable.")
            self.google_signin_button.setEnabled(False)
            self.google_signout_button.setEnabled(False)
            return
        if self.google_oauth.is_connected():
            self.google_state_label.setText("Signed in to Google.")
            self.google_signout_button.setEnabled(True)
        elif self.google_oauth.is_configured():
            self.google_state_label.setText("Client saved — click Sign in with Google.")
            self.google_signout_button.setEnabled(False)
        else:
            self.google_state_label.setText("Enter the Client ID and Secret, then sign in.")
            self.google_signout_button.setEnabled(False)

    def _refresh_session_state(self):
        if self.session is None:
            self.session_state_label.setText("Browser sign-in unavailable.")
            self.signin_button.setEnabled(False)
            self.signout_button.setEnabled(False)
            return
        if self.session.is_active():
            import time as _t
            captured = self.session.captured_at()
            when = ""
            if captured:
                mins = int((_t.time() - captured) // 60)
                when = f" · signed in {mins} min ago" if mins < 120 else f" · signed in {mins // 60} h ago"
            self.session_state_label.setText(f"Signed in via browser{when}.")
            self.signout_button.setEnabled(True)
        else:
            self.session_state_label.setText("Not signed in.")
            self.signout_button.setEnabled(False)

    def _collect_google_settings(self):
        payload = {
            "enabled": self.google_enabled_box.isChecked(),
            "account_email": self.google_email_input.text().strip(),
            "client_id": self.google_client_id_input.text().strip(),
            "refresh_token": self.google_refresh_token_input.text().strip(),
            "calendar_id": self.google_calendar_id_input.text().strip() or "primary",
        }
        # Only overwrite the stored secret if the user typed a new one; the live
        # value lives in the encrypted store via GoogleOAuth.
        secret = self.google_client_secret_input.text().strip()
        if secret:
            payload["client_secret"] = secret
        return payload

    def _apply_google_settings(self):
        self.config_manager.update_google_workspace_settings(self._collect_google_settings())
        secret = self.google_client_secret_input.text().strip()
        if secret and self.google_oauth is not None:
            self.google_oauth.set_client_secret(secret)
            self.google_client_secret_input.clear()

    def _collect_outlook_settings(self):
        return {
            "enabled": self.outlook_enabled_box.isChecked(),
            "account_email": self.outlook_email_input.text().strip(),
            "tenant_id": self.outlook_tenant_input.text().strip(),
            "client_id": self.outlook_client_id_input.text().strip(),
            "client_secret": self.outlook_client_secret_input.text().strip(),
            "refresh_token": self.outlook_refresh_token_input.text().strip(),
        }

    def _set_status(self, message, timestamp=""):
        suffix = f"\nLast checked: {timestamp}" if timestamp else ""
        self.status_label.setText(f"Status: {message}{suffix}")

    # ── feed url helpers ─────────────────────────────────────────────────
    def _toggle_feed_visibility(self, shown: bool):
        self.feed_url_input.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        )
        self.show_feed_button.setText("Hide" if shown else "Show")

    def _forget_feed(self):
        self.brightspace_client.clear_feed_url()
        self.feed_url_input.clear()
        self._refresh_feed_state()
        QMessageBox.information(self, "Brightspace", "Saved feed URL removed.")

    # ── persistence ──────────────────────────────────────────────────────
    def _apply_brightspace_settings(self) -> bool:
        lms_url = (
            self.session_lms_url_input.text().strip()
            or self.lms_url_input.text().strip()
        ).rstrip("/")
        # keep the two URL fields in sync
        self.session_lms_url_input.setText(lms_url)
        self.lms_url_input.setText(lms_url)
        self.config_manager.update_brightspace_settings(
            {
                "enabled": self.enabled_box.isChecked(),
                "lms_url": lms_url,
                "oauth_client_id": self.oauth_client_id_input.text().strip(),
                "oauth_redirect_uri": self.oauth_redirect_input.text().strip()
                or "http://localhost:8713/callback",
                "oauth_scope": self.oauth_scope_input.text().strip(),
            }
        )
        secret = self.oauth_client_secret_input.text().strip()
        if secret and self.oauth is not None:
            self.oauth.set_client_secret(secret)
            self.oauth_client_secret_input.clear()

        url = self.feed_url_input.text().strip()
        if url:
            try:
                self.brightspace_client.set_feed_url(url)
            except BrightspaceError as exc:
                QMessageBox.warning(self, "Brightspace", str(exc))
                return False
            self.feed_url_input.clear()

        self._refresh_feed_state()
        self._refresh_oauth_state()
        self._refresh_session_state()
        return True

    # ── browser sign-in ──────────────────────────────────────────────────
    def sign_in_brightspace(self):
        if self.session is None:
            return
        if not self._apply_brightspace_settings():
            return
        lms_url = self.session_lms_url_input.text().strip().rstrip("/")
        if not lms_url:
            QMessageBox.information(self, "Brightspace", "Enter your Brightspace URL first.")
            return

        try:
            from ui.brightspace_login_dialog import BrightspaceLoginDialog
        except Exception as exc:  # noqa: BLE001 - optional dependency
            QMessageBox.warning(
                self,
                "Browser sign-in unavailable",
                "The embedded browser needs the PyQt6-WebEngine package:\n\n"
                "  .venv\\Scripts\\python.exe -m pip install PyQt6-WebEngine\n\n"
                f"({exc})",
            )
            return

        dialog = BrightspaceLoginDialog(lms_url, self.webcache_dir, self)
        if dialog.exec() != dialog.DialogCode.Accepted or not dialog.result_data:
            self._refresh_session_state()
            return

        data = dialog.result_data
        self.session.save(data["cookies"], data.get("xsrf", ""), data["lms_url"])
        self._refresh_session_state()
        self.settings_saved.emit()
        QMessageBox.information(self, "Brightspace", "Signed in. Syncing your calendar…")
        if self.enabled_box.isChecked():
            self._start_test(run_sync=True)

    def sign_out_brightspace(self):
        if self.session is None:
            return
        self.session.clear()
        self._refresh_session_state()
        self.settings_saved.emit()
        QMessageBox.information(self, "Brightspace", "Signed out.")

    # ── Google sign-in / sign-out ────────────────────────────────────────
    def connect_google(self):
        if self.google_oauth is None or self._google_thread is not None:
            return
        self._apply_google_settings()
        if not self.google_oauth.is_configured():
            QMessageBox.information(
                self, "Google", "Enter the Google Client ID and Client Secret first."
            )
            return

        self.google_signin_button.setEnabled(False)
        self.google_state_label.setText("Opening your browser to sign in to Google…")

        self._google_thread = QThread()
        self._google_worker = _OAuthConnectWorker(self.google_oauth)
        self._google_worker.moveToThread(self._google_thread)
        self._google_thread.started.connect(self._google_worker.run)
        self._google_worker.done.connect(self._on_google_done)
        self._google_worker.done.connect(self._google_thread.quit)
        self._google_worker.done.connect(self._google_worker.deleteLater)
        self._google_thread.finished.connect(self._cleanup_google_thread)
        self._google_thread.finished.connect(self._google_thread.deleteLater)
        self._google_thread.start()

    def _cleanup_google_thread(self):
        self._google_thread = None
        self._google_worker = None
        self.google_signin_button.setEnabled(True)

    def _on_google_done(self, ok: bool, message: str):
        self._refresh_google_state()
        self.settings_saved.emit()
        box = QMessageBox.information if ok else QMessageBox.warning
        box(self, "Google", message)

    def disconnect_google(self):
        if self.google_oauth is None:
            return
        self.google_oauth.disconnect()
        self._refresh_google_state()
        self.settings_saved.emit()
        QMessageBox.information(self, "Google", "Signed out of Google.")

    def save_settings(self):
        if not self._apply_brightspace_settings():
            return
        self._apply_google_settings()
        self.config_manager.update_outlook_settings(self._collect_outlook_settings())

        settings = self.config_manager.get_brightspace_settings()
        self._set_status(
            settings.get("last_sync_status", "Settings saved"),
            settings.get("last_sync_at", ""),
        )
        self.settings_saved.emit()
        QMessageBox.information(self, "Settings", "Settings saved.")

    # ── OAuth connect / disconnect ───────────────────────────────────────
    def connect_oauth(self):
        if self.oauth is None or self._oauth_thread is not None:
            return
        if not self._apply_brightspace_settings():
            return
        if not self.oauth.is_configured():
            QMessageBox.information(
                self, "Brightspace",
                "Enter the Brightspace URL and OAuth Client ID first.",
            )
            return

        self.connect_button.setEnabled(False)
        self.oauth_state_label.setText("Opening your browser to sign in…")

        self._oauth_thread = QThread()
        self._oauth_worker = _OAuthConnectWorker(self.oauth)
        self._oauth_worker.moveToThread(self._oauth_thread)
        self._oauth_thread.started.connect(self._oauth_worker.run)
        self._oauth_worker.done.connect(self._on_oauth_done)
        self._oauth_worker.done.connect(self._oauth_thread.quit)
        self._oauth_worker.done.connect(self._oauth_worker.deleteLater)
        self._oauth_thread.finished.connect(self._cleanup_oauth_thread)
        self._oauth_thread.finished.connect(self._oauth_thread.deleteLater)
        self._oauth_thread.start()

    def _cleanup_oauth_thread(self):
        self._oauth_thread = None
        self._oauth_worker = None
        self.connect_button.setEnabled(True)

    def _on_oauth_done(self, ok: bool, message: str):
        self._refresh_oauth_state()
        self.settings_saved.emit()
        if ok:
            QMessageBox.information(self, "Brightspace", message)
            if self.enabled_box.isChecked():
                self._start_test(run_sync=True)
        else:
            QMessageBox.warning(self, "Brightspace", message)

    def disconnect_oauth(self):
        if self.oauth is None:
            return
        self.oauth.disconnect()
        self._refresh_oauth_state()
        self.settings_saved.emit()
        QMessageBox.information(self, "Brightspace", "Disconnected from Brightspace.")

    # ── test & sync ──────────────────────────────────────────────────────
    def test_connection(self):
        if self._test_thread is not None:
            return
        if not self._apply_brightspace_settings():
            return
        self._apply_google_settings()
        self.config_manager.update_outlook_settings(self._collect_outlook_settings())
        self.settings_saved.emit()

        if not self.brightspace_client.is_configured():
            QMessageBox.information(
                self, "Brightspace",
                "Connect with OAuth 2.0, or paste a calendar feed URL, first.",
            )
            return

        self._start_test(run_sync=self.enabled_box.isChecked())

    def _start_test(self, run_sync: bool):
        if self._test_thread is not None:
            return
        self.test_button.setEnabled(False)
        self._set_status("Checking Brightspace connection…")

        self._test_thread = QThread()
        self._test_worker = _BrightspaceTestWorker(
            self.brightspace_client, self.brightspace_sync, run_sync
        )
        self._test_worker.moveToThread(self._test_thread)
        self._test_thread.started.connect(self._test_worker.run)
        self._test_worker.done.connect(self._on_test_done)
        self._test_worker.done.connect(self._test_thread.quit)
        self._test_worker.done.connect(self._test_worker.deleteLater)
        self._test_thread.finished.connect(self._cleanup_test_thread)
        self._test_thread.finished.connect(self._test_thread.deleteLater)
        self._test_thread.start()

    def _cleanup_test_thread(self):
        self._test_thread = None
        self._test_worker = None
        self.test_button.setEnabled(True)

    def _on_test_done(self, ok: bool, message: str, ran_sync: bool):
        settings = self.config_manager.get_brightspace_settings()
        self._set_status(message, settings.get("last_sync_at", ""))
        self._refresh_feed_state()
        self._refresh_oauth_state()
        self._refresh_session_state()
        self._refresh_google_state()
        if ran_sync:
            self.brightspace_synced.emit()

        box = QMessageBox.information if ok else QMessageBox.warning
        box(self, "Brightspace", message)

    def shutdown(self):
        from ui.thread_utils import stop_thread

        for tname, wname in self._thread_pairs:
            stop_thread(getattr(self, tname, None), getattr(self, wname, None))
