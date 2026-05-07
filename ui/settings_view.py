from PyQt6.QtCore import pyqtSignal
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


class SettingsView(QWidget):
    settings_saved = pyqtSignal()

    def __init__(self, config_manager, brightspace_client):
        super().__init__()

        self.config_manager = config_manager
        self.brightspace_client = brightspace_client

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)
        self.setLayout(layout)

        title = QLabel("Settings")
        title.setStyleSheet("font-size: 24px; font-weight: 600;")
        layout.addWidget(title)

        brightspace_group = QGroupBox("Brightspace")
        form = QFormLayout()
        brightspace_group.setLayout(form)

        self.enabled_box = QCheckBox("Enable Brightspace on dashboard")
        form.addRow(self.enabled_box)

        self.base_url_input = QLineEdit()
        self.base_url_input.setPlaceholderText("https://school.brightspace.com")
        form.addRow("Base URL", self.base_url_input)

        self.org_unit_input = QLineEdit()
        self.org_unit_input.setPlaceholderText("Course or org unit ID")
        form.addRow("Org Unit ID", self.org_unit_input)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Brightspace username or email")
        form.addRow("Username", self.username_input)

        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setPlaceholderText("Access token placeholder for future sync")
        form.addRow("Access Token", self.token_input)

        layout.addWidget(brightspace_group)

        google_group = QGroupBox("Google Calendar and Gmail")
        google_form = QFormLayout()
        google_group.setLayout(google_form)

        self.google_enabled_box = QCheckBox("Enable Google integrations")
        google_form.addRow(self.google_enabled_box)

        self.google_email_input = QLineEdit()
        self.google_email_input.setPlaceholderText("name@gmail.com")
        google_form.addRow("Account Email", self.google_email_input)

        self.google_client_id_input = QLineEdit()
        self.google_client_id_input.setPlaceholderText("Google OAuth client ID")
        google_form.addRow("Client ID", self.google_client_id_input)

        self.google_client_secret_input = QLineEdit()
        self.google_client_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.google_client_secret_input.setPlaceholderText("Google OAuth client secret")
        google_form.addRow("Client Secret", self.google_client_secret_input)

        self.google_refresh_token_input = QLineEdit()
        self.google_refresh_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.google_refresh_token_input.setPlaceholderText("Refresh token")
        google_form.addRow("Refresh Token", self.google_refresh_token_input)

        layout.addWidget(google_group)

        outlook_group = QGroupBox("Outlook")
        outlook_form = QFormLayout()
        outlook_group.setLayout(outlook_form)

        self.outlook_enabled_box = QCheckBox("Enable Outlook integration")
        outlook_form.addRow(self.outlook_enabled_box)

        self.outlook_email_input = QLineEdit()
        self.outlook_email_input.setPlaceholderText("name@outlook.com or Microsoft 365 account")
        outlook_form.addRow("Account Email", self.outlook_email_input)

        self.outlook_tenant_input = QLineEdit()
        self.outlook_tenant_input.setPlaceholderText("Tenant ID or common")
        outlook_form.addRow("Tenant ID", self.outlook_tenant_input)

        self.outlook_client_id_input = QLineEdit()
        self.outlook_client_id_input.setPlaceholderText("Microsoft app client ID")
        outlook_form.addRow("Client ID", self.outlook_client_id_input)

        self.outlook_client_secret_input = QLineEdit()
        self.outlook_client_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.outlook_client_secret_input.setPlaceholderText("Microsoft app client secret")
        outlook_form.addRow("Client Secret", self.outlook_client_secret_input)

        self.outlook_refresh_token_input = QLineEdit()
        self.outlook_refresh_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.outlook_refresh_token_input.setPlaceholderText("Refresh token")
        outlook_form.addRow("Refresh Token", self.outlook_refresh_token_input)

        layout.addWidget(outlook_group)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        button_row = QHBoxLayout()
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self.save_settings)
        button_row.addWidget(self.save_button)

        self.test_button = QPushButton("Test Connection")
        self.test_button.clicked.connect(self.test_connection)
        button_row.addWidget(self.test_button)

        button_row.addStretch()
        layout.addLayout(button_row)
        layout.addStretch()

        self.load_settings()

    def load_settings(self):
        settings = self.config_manager.get_brightspace_settings()
        self.enabled_box.setChecked(bool(settings.get("enabled")))
        self.base_url_input.setText(settings.get("base_url", ""))
        self.org_unit_input.setText(settings.get("org_unit_id", ""))
        self.username_input.setText(settings.get("username", ""))
        self.token_input.setText(settings.get("access_token", ""))
        self._set_status(
            settings.get("last_sync_status", "Not connected"),
            settings.get("last_sync_at", ""),
        )

        google = self.config_manager.get_google_workspace_settings()
        self.google_enabled_box.setChecked(bool(google.get("enabled")))
        self.google_email_input.setText(google.get("account_email", ""))
        self.google_client_id_input.setText(google.get("client_id", ""))
        self.google_client_secret_input.setText(google.get("client_secret", ""))
        self.google_refresh_token_input.setText(google.get("refresh_token", ""))

        outlook = self.config_manager.get_outlook_settings()
        self.outlook_enabled_box.setChecked(bool(outlook.get("enabled")))
        self.outlook_email_input.setText(outlook.get("account_email", ""))
        self.outlook_tenant_input.setText(outlook.get("tenant_id", ""))
        self.outlook_client_id_input.setText(outlook.get("client_id", ""))
        self.outlook_client_secret_input.setText(outlook.get("client_secret", ""))
        self.outlook_refresh_token_input.setText(outlook.get("refresh_token", ""))

    def _collect_settings(self):
        return {
            "enabled": self.enabled_box.isChecked(),
            "base_url": self.base_url_input.text().strip(),
            "org_unit_id": self.org_unit_input.text().strip(),
            "username": self.username_input.text().strip(),
            "access_token": self.token_input.text().strip(),
        }

    def _collect_google_settings(self):
        return {
            "enabled": self.google_enabled_box.isChecked(),
            "account_email": self.google_email_input.text().strip(),
            "client_id": self.google_client_id_input.text().strip(),
            "client_secret": self.google_client_secret_input.text().strip(),
            "refresh_token": self.google_refresh_token_input.text().strip(),
        }

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

    def save_settings(self):
        settings = self.config_manager.update_brightspace_settings(self._collect_settings())
        self.config_manager.update_google_workspace_settings(self._collect_google_settings())
        self.config_manager.update_outlook_settings(self._collect_outlook_settings())
        self._set_status(
            settings.get("last_sync_status", "Settings saved"),
            settings.get("last_sync_at", ""),
        )
        self.settings_saved.emit()
        QMessageBox.information(self, "Settings", "Settings saved.")

    def test_connection(self):
        self.config_manager.update_brightspace_settings(self._collect_settings())
        ok, settings = self.brightspace_client.sync_status()
        google_message = self._validate_google_settings()
        outlook_message = self._validate_outlook_settings()
        self._set_status(settings["last_sync_status"], settings["last_sync_at"])
        self.settings_saved.emit()

        if ok:
            QMessageBox.information(
                self,
                "Connection Status",
                (
                    f"Brightspace: {settings['last_sync_status']}\n"
                    f"Google: {google_message}\n"
                    f"Outlook: {outlook_message}"
                ),
            )
        else:
            QMessageBox.warning(
                self,
                "Connection Status",
                (
                    f"Brightspace: {settings['last_sync_status']}\n"
                    f"Google: {google_message}\n"
                    f"Outlook: {outlook_message}"
                ),
            )

    def _validate_google_settings(self):
        settings = self._collect_google_settings()
        required = ("account_email", "client_id", "client_secret", "refresh_token")
        missing = [field for field in required if not settings.get(field)]

        status = (
            f"Missing {', '.join(missing)}"
            if missing
            else "Ready for Google Calendar and Gmail token exchange"
        )
        self.config_manager.update_google_workspace_settings(
            {
                **settings,
                "last_sync_at": "",
                "last_sync_status": status,
            }
        )
        return status

    def _validate_outlook_settings(self):
        settings = self._collect_outlook_settings()
        required = ("account_email", "tenant_id", "client_id", "client_secret", "refresh_token")
        missing = [field for field in required if not settings.get(field)]

        status = (
            f"Missing {', '.join(missing)}"
            if missing
            else "Ready for Outlook/Microsoft Graph token exchange"
        )
        self.config_manager.update_outlook_settings(
            {
                **settings,
                "last_sync_at": "",
                "last_sync_status": status,
            }
        )
        return status
