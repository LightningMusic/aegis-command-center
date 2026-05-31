from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QLineEdit,
    QComboBox,
    QMessageBox,
    QProgressBar,
    QDialog,
)
from pathlib import Path


class DevicesView(QWidget):
    """UI for managing backup devices and phone backups."""

    backup_requested = pyqtSignal(str, str)  # device_name, device_type
    phone_backup_requested = pyqtSignal(str, str)  # phone_path, device_name

    def __init__(self, task_manager, device_manager, backup_manager):
        super().__init__()
        self.task_manager = task_manager
        self.device_manager = device_manager
        self.backup_manager = backup_manager

        self.init_ui()
        self.refresh_device_list()

    def init_ui(self):
        """Initialize the UI layout."""
        layout = QVBoxLayout()

        title = QLabel("📱 Device Backup Management")
        title.setStyleSheet("font-size: 16px; font-weight: bold; margin-bottom: 10px;")
        layout.addWidget(title)

        top_section = QHBoxLayout()

        detect_btn = QPushButton("🔍 Detect Connected Devices")
        detect_btn.clicked.connect(self.detect_devices)
        top_section.addWidget(detect_btn)

        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.clicked.connect(self.refresh_device_list)
        top_section.addWidget(refresh_btn)

        add_device_btn = QPushButton("➕ Add Device Manually")
        add_device_btn.clicked.connect(self.show_add_device_dialog)
        top_section.addWidget(add_device_btn)

        layout.addLayout(top_section)

        section_label = QLabel("Registered Devices")
        section_label.setStyleSheet("font-weight: bold; margin-top: 15px;")
        layout.addWidget(section_label)

        self.devices_table = QTableWidget()
        self.devices_table.setColumnCount(6)
        self.devices_table.setHorizontalHeaderLabels(
            ["Device Name", "Type", "Last Backup", "Files", "Size", "Actions"]
        )
        self.devices_table.horizontalHeader().setStretchLastSection(False)
        layout.addWidget(self.devices_table)

        phones_section = QLabel("📱 Connected Phones")
        phones_section.setStyleSheet("font-weight: bold; margin-top: 15px;")
        layout.addWidget(phones_section)

        self.phones_table = QTableWidget()
        self.phones_table.setColumnCount(5)
        self.phones_table.setHorizontalHeaderLabels(
            ["Phone Name", "Mount Point", "Status", "Available Space", "Actions"]
        )
        self.phones_table.horizontalHeader().setStretchLastSection(False)
        layout.addWidget(self.phones_table)

        self.setLayout(layout)

    def detect_devices(self):
        """Detect connected devices and phones."""
        detected = self.device_manager.detect_connected_devices()

        if not detected:
            QMessageBox.information(self, "Detection", "No new devices detected.")
            return

        message = "Detected devices:\n\n"
        for device in detected:
            device_type = device.get("device_type", "unknown")
            label = device.get("label", device.get("phone_name", "Unknown"))
            message += f"- {label} ({device_type})\n"

        QMessageBox.information(self, "Device Detection", message)
        self.refresh_device_list()

    def refresh_device_list(self):
        """Refresh the list of registered devices."""
        devices = self.device_manager.get_all_devices(active_only=True)

        self.devices_table.setRowCount(len(devices))

        for row, device in enumerate(devices):
            device_name = device.get("device_name", "Unknown")
            device_type = device.get("device_type", "unknown")
            last_backup = device.get("last_backup", "Never")

            backup_history = self.device_manager.get_backup_history(
                device.get("device_id"), limit=1
            )
            file_count = 0
            total_size = 0

            if backup_history:
                file_count = backup_history[0].get("file_count", 0)
                total_size = backup_history[0].get("size_bytes", 0)

            self.devices_table.setItem(row, 0, QTableWidgetItem(device_name))
            self.devices_table.setItem(row, 1, QTableWidgetItem(device_type))
            self.devices_table.setItem(row, 2, QTableWidgetItem(last_backup[:10] if last_backup else "Never"))
            self.devices_table.setItem(row, 3, QTableWidgetItem(str(file_count)))
            self.devices_table.setItem(row, 4, QTableWidgetItem(self._format_bytes(total_size)))

            actions_layout = QHBoxLayout()
            backup_btn = QPushButton("Backup")
            backup_btn.clicked.connect(
                lambda checked, did=device.get("device_id"): self.backup_device(did)
            )
            actions_layout.addWidget(backup_btn)

            rename_btn = QPushButton("Rename")
            rename_btn.clicked.connect(
                lambda checked, did=device.get("device_id"): self.rename_device(did)
            )
            actions_layout.addWidget(rename_btn)

            actions_widget = QWidget()
            actions_widget.setLayout(actions_layout)
            self.devices_table.setCellWidget(row, 5, actions_widget)

    def detect_connected_phones(self):
        """Detect connected phones."""
        phones = []
        try:
            detected = self.device_manager._detect_connected_phones()
            phones.extend(detected)
        except Exception:
            pass

        self.phones_table.setRowCount(len(phones))

        for row, phone in enumerate(phones):
            phone_name = phone.get("label", "Unknown Phone")
            mount_point = phone.get("path", phone.get("drive_letter", "Unknown"))

            self.phones_table.setItem(row, 0, QTableWidgetItem(phone_name))
            self.phones_table.setItem(row, 1, QTableWidgetItem(mount_point))
            self.phones_table.setItem(row, 2, QTableWidgetItem("Ready"))

            try:
                import shutil
                usage = shutil.disk_usage(mount_point)
                available = self._format_bytes(usage.free)
            except Exception:
                available = "Unknown"

            self.phones_table.setItem(row, 3, QTableWidgetItem(available))

            actions_layout = QHBoxLayout()
            backup_phone_btn = QPushButton("Backup Phone")
            backup_phone_btn.clicked.connect(
                lambda checked, mp=mount_point, pn=phone_name: self.backup_phone(mp, pn)
            )
            actions_layout.addWidget(backup_phone_btn)

            actions_widget = QWidget()
            actions_widget.setLayout(actions_layout)
            self.phones_table.setCellWidget(row, 4, actions_widget)

    def backup_device(self, device_id):
        """Initiate backup for a device."""
        device = self.device_manager.get_device_by_id(device_id)
        if not device:
            QMessageBox.warning(self, "Error", "Device not found.")
            return

        device_name = device.get("device_name")
        device_type = device.get("device_type")

        self.backup_requested.emit(device_name, device_type)
        QMessageBox.information(self, "Backup", f"Starting backup for {device_name}...")

    def backup_phone(self, phone_path: str, phone_name: str):
        """Initiate backup for a phone."""
        device_id = self.device_manager.register_device(
            phone_name,
            "phone",
            phone_os="android",
        )
        self.phone_backup_requested.emit(phone_path, phone_name)
        QMessageBox.information(self, "Phone Backup", f"Starting backup for {phone_name}...")

    def show_add_device_dialog(self):
        """Show dialog to manually add a device."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Add Device")
        layout = QVBoxLayout()

        device_name_label = QLabel("Device Name:")
        device_name_input = QLineEdit()
        layout.addWidget(device_name_label)
        layout.addWidget(device_name_input)

        device_type_label = QLabel("Device Type:")
        device_type_combo = QComboBox()
        device_type_combo.addItems(["pc", "external_drive", "phone"])
        layout.addWidget(device_type_label)
        layout.addWidget(device_type_combo)

        drive_letter_label = QLabel("Drive Letter (optional):")
        drive_letter_input = QLineEdit()
        drive_letter_input.setPlaceholderText("e.g., C, D, E")
        layout.addWidget(drive_letter_label)
        layout.addWidget(drive_letter_input)

        add_btn = QPushButton("Add Device")

        def add_device():
            device_name = device_name_input.text().strip()
            device_type = device_type_combo.currentText()
            drive_letter = drive_letter_input.text().strip() or None

            if not device_name:
                QMessageBox.warning(dialog, "Error", "Please enter a device name.")
                return

            try:
                self.device_manager.register_device(
                    device_name, device_type, drive_letter=drive_letter
                )
                self.refresh_device_list()
                dialog.accept()
                QMessageBox.information(self, "Success", f"Device '{device_name}' added.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to add device: {str(e)}")

        add_btn.clicked.connect(add_device)
        layout.addWidget(add_btn)

        dialog.setLayout(layout)
        dialog.exec()

    def rename_device(self, device_id):
        """Rename a device."""
        device = self.device_manager.get_device_by_id(device_id)
        if not device:
            QMessageBox.warning(self, "Error", "Device not found.")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Rename Device")
        layout = QVBoxLayout()

        label = QLabel(f"New name for '{device.get('device_name')}':")
        name_input = QLineEdit()
        name_input.setText(device.get("device_name"))

        layout.addWidget(label)
        layout.addWidget(name_input)

        rename_btn = QPushButton("Rename")

        def do_rename():
            new_name = name_input.text().strip()
            if not new_name:
                QMessageBox.warning(dialog, "Error", "Please enter a device name.")
                return

            self.device_manager.rename_device(device_id, new_name)
            self.refresh_device_list()
            dialog.accept()
            QMessageBox.information(self, "Success", f"Device renamed to '{new_name}'.")

        rename_btn.clicked.connect(do_rename)
        layout.addWidget(rename_btn)

        dialog.setLayout(layout)
        dialog.exec()

    def _format_bytes(self, bytes_value):
        """Format bytes to human-readable size."""
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(bytes_value)
        for unit in units:
            if size < 1024 or unit == units[-1]:
                return f"{size:.2f} {unit}"
            size /= 1024
