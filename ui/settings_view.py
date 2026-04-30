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

    def _collect_settings(self):
        return {
            "enabled": self.enabled_box.isChecked(),
            "base_url": self.base_url_input.text().strip(),
            "org_unit_id": self.org_unit_input.text().strip(),
            "username": self.username_input.text().strip(),
            "access_token": self.token_input.text().strip(),
        }

    def _set_status(self, message, timestamp=""):
        suffix = f"\nLast checked: {timestamp}" if timestamp else ""
        self.status_label.setText(f"Status: {message}{suffix}")

    def save_settings(self):
        settings = self.config_manager.update_brightspace_settings(self._collect_settings())
        self._set_status(
            settings.get("last_sync_status", "Settings saved"),
            settings.get("last_sync_at", ""),
        )
        self.settings_saved.emit()
        QMessageBox.information(self, "Settings", "Settings saved.")

    def test_connection(self):
        self.config_manager.update_brightspace_settings(self._collect_settings())
        ok, settings = self.brightspace_client.sync_status()
        self._set_status(settings["last_sync_status"], settings["last_sync_at"])
        self.settings_saved.emit()

        if ok:
            QMessageBox.information(self, "Brightspace", settings["last_sync_status"])
        else:
            QMessageBox.warning(self, "Brightspace", settings["last_sync_status"])
