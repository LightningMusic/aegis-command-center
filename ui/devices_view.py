from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QThread, QObject
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
    QTabWidget,
)
import shutil
from pathlib import Path
from typing import Optional
from core.phone_backup_manager import PhoneBackupManager
from ui.phone_backup_view import PhoneBackupView
from core.config import ConfigManager
from core.util import human_bytes


class PhoneDetectionWorker(QObject):
    finished = pyqtSignal(list)

    def __init__(self, device_manager):
        super().__init__()
        self.device_manager = device_manager

    def run(self):
        try:
            detected = self.device_manager._detect_connected_phones()
        except Exception:
            self.finished.emit([])
            return

        # Resolve free space here (a stale mount can block for seconds) so the
        # GUI thread never calls shutil.disk_usage itself.
        for phone in detected:
            mount = phone.get("path") or phone.get("drive_letter") or ""
            if mount and not mount.startswith("::"):
                try:
                    phone["available_space"] = shutil.disk_usage(mount).free
                except Exception:
                    phone["available_space"] = None
            else:
                phone["available_space"] = None

        self.finished.emit(detected)


class DeviceDetectionWorker(QObject):
    finished = pyqtSignal(list)

    def __init__(self, device_manager):
        super().__init__()
        self.device_manager = device_manager

    def run(self):
        try:
            detected = self.device_manager.detect_connected_devices()
            self.finished.emit(detected)
        except Exception:
            self.finished.emit([])


class DeviceListWorker(QObject):
    """Gathers the registered-device table data and phone backup history off
    the GUI thread — both hit the 1.6 GB SQLite file and parse per-device
    manifest JSON, which froze the view on every switch to the Devices page."""

    finished = pyqtSignal(dict)

    def __init__(self, device_manager, phone_backup_manager):
        super().__init__()
        self.device_manager = device_manager
        self.phone_backup_manager = phone_backup_manager

    def run(self):
        payload = {"devices": [], "history": []}

        try:
            devices = self.device_manager.get_all_devices(active_only=True)
            rows = []
            for device in devices:
                history = self.device_manager.get_backup_history(
                    device.get("device_id"), limit=1
                )
                rows.append({
                    "device": device,
                    "last_run": history[0] if history else None,
                })
            payload["devices"] = rows
        except Exception:
            pass

        try:
            payload["history"] = self.phone_backup_manager.list_all_backed_up_devices()
        except Exception:
            pass

        self.finished.emit(payload)


class DevicesView(QWidget):
    """UI for managing backup devices and phone backups."""

    backup_requested = pyqtSignal(str, str)  # device_name, device_type
    phone_backup_requested = pyqtSignal(str, str)  # phone_path, device_name

    def __init__(self, task_manager, device_manager, backup_manager):
        super().__init__()
        self.task_manager = task_manager
        self.device_manager = device_manager
        self.backup_manager = backup_manager
        self.dev_detection_thread = None
        self.dev_detection_worker = None
        self.phone_detection_thread = None
        self.phone_detection_worker = None
        self.device_list_thread = None
        self.device_list_worker = None
        self._pending_detect_phones = False

        # Initialize ConfigManager and PhoneBackupManager
        self.config_manager = ConfigManager()
        backup_settings = self.config_manager.get_backup_settings()
        dest = backup_settings.get("default_destination") or "C:\\Aegis_Backups"
        self.phone_backup_manager = PhoneBackupManager(dest)

        self.init_ui()
        self.refresh_device_list(detect_phones=False)

    def init_ui(self):
        """Initialize the UI layout with tabs."""
        self.main_layout = QVBoxLayout()
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(self.main_layout)

        self.tabs = QTabWidget()
        self.main_layout.addWidget(self.tabs)

        # Tab 1: Overview & Registered Devices
        self.overview_tab = QWidget()
        overview_layout = QVBoxLayout()
        overview_layout.setContentsMargins(14, 14, 14, 14)
        self.overview_tab.setLayout(overview_layout)

        title = QLabel("📱 Device Backup Management")
        title.setStyleSheet("font-size: 16px; font-weight: bold; margin-bottom: 10px;")
        overview_layout.addWidget(title)

        top_section = QHBoxLayout()

        self.detect_btn = QPushButton("🔍 Detect Connected Devices")
        self.detect_btn.clicked.connect(self.detect_devices)
        top_section.addWidget(self.detect_btn)

        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.clicked.connect(self.refresh_device_list)
        top_section.addWidget(refresh_btn)

        add_device_btn = QPushButton("➕ Add Device Manually")
        add_device_btn.clicked.connect(self.show_add_device_dialog)
        top_section.addWidget(add_device_btn)

        overview_layout.addLayout(top_section)

        section_label = QLabel("Registered Devices")
        section_label.setStyleSheet("font-weight: bold; margin-top: 15px;")
        overview_layout.addWidget(section_label)

        self.devices_table = QTableWidget()
        self.devices_table.setColumnCount(6)
        self.devices_table.setHorizontalHeaderLabels(
            ["Device Name", "Type", "Last Backup", "Files", "Size", "Actions"]
        )
        devices_header = self.devices_table.horizontalHeader()
        if devices_header is not None:
            devices_header.setStretchLastSection(True)
        overview_layout.addWidget(self.devices_table)

        phones_section = QLabel("📱 Connected Phones")
        phones_section.setStyleSheet("font-weight: bold; margin-top: 15px;")
        overview_layout.addWidget(phones_section)

        self.phones_table = QTableWidget()
        self.phones_table.setColumnCount(5)
        self.phones_table.setHorizontalHeaderLabels(
            ["Phone Name", "Mount Point", "Status", "Available Space", "Actions"]
        )
        phones_header = self.phones_table.horizontalHeader()
        if phones_header is not None:
            phones_header.setStretchLastSection(True)
        overview_layout.addWidget(self.phones_table)

        # Tab 2: Phone Backup & Restore (using pre-built PhoneBackupView)
        self.phone_backup_view = PhoneBackupView(self.phone_backup_manager)
        self.phone_backup_view.device_manager = self.device_manager

        self.tabs.addTab(self.overview_tab, "Registered Devices")
        self.tabs.addTab(self.phone_backup_view, "Phone Backup & Restore")

    def detect_devices(self):
        """Detect connected devices and phones in the background."""
        if self._thread_is_running(self.dev_detection_thread):
            return

        self.detect_btn.setEnabled(False)
        self.detect_btn.setText("Detecting...")

        self.dev_detection_thread = QThread()
        self.dev_detection_worker = DeviceDetectionWorker(self.device_manager)
        self.dev_detection_worker.moveToThread(self.dev_detection_thread)

        self.dev_detection_thread.started.connect(self.dev_detection_worker.run)
        self.dev_detection_worker.finished.connect(self.on_dev_detection_finished)
        self.dev_detection_worker.finished.connect(self.dev_detection_thread.quit)
        self.dev_detection_worker.finished.connect(self.dev_detection_worker.deleteLater)
        self.dev_detection_thread.finished.connect(self._cleanup_dev_detection_thread)
        self.dev_detection_thread.finished.connect(self.dev_detection_thread.deleteLater)

        self.dev_detection_thread.start()

    def on_dev_detection_finished(self, detected):
        self.detect_btn.setEnabled(True)
        self.detect_btn.setText("🔍 Detect Connected Devices")

        if not detected:
            QMessageBox.information(self, "Detection", "No new devices detected.")
            return

        # Explicitly sort detected devices by label
        detected.sort(key=lambda d: d.get("label", d.get("phone_name", "Unknown")).lower())

        message = "Detected devices:\n\n"
        for device in detected:
            device_type = device.get("device_type", "unknown")
            label = device.get("label", device.get("phone_name", "Unknown"))
            message += f"- {label} ({device_type})\n"

        QMessageBox.information(self, "Device Detection", message)
        self.refresh_device_list()

    def _cleanup_dev_detection_thread(self):
        self.dev_detection_thread = None
        self.dev_detection_worker = None

    def refresh_device_list(self, detect_phones=True):
        """Refresh the list of registered devices and phone backup settings.

        The slow parts (DB reads against a large file, manifest JSON parsing)
        run on a worker thread; the table is filled in from the result.
        """
        # Refresh backup root from config (cheap — a small JSON read).
        self.config_manager = ConfigManager()
        backup_settings = self.config_manager.get_backup_settings()
        dest = backup_settings.get("default_destination") or "C:\\Aegis_Backups"

        self.phone_backup_manager.backup_root = Path(dest)
        try:
            self.phone_backup_manager._phones_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # Remember the caller's intent; the actual scan is kicked off once the
        # list data comes back so the two threads don't pile up.
        self._pending_detect_phones = bool(detect_phones)

        if self._thread_is_running(self.device_list_thread):
            return

        self.device_list_thread = QThread()
        self.device_list_worker = DeviceListWorker(
            self.device_manager, self.phone_backup_manager
        )
        self.device_list_worker.moveToThread(self.device_list_thread)

        self.device_list_thread.started.connect(self.device_list_worker.run)
        self.device_list_worker.finished.connect(self._on_device_list_ready)
        self.device_list_worker.finished.connect(self.device_list_thread.quit)
        self.device_list_worker.finished.connect(self.device_list_worker.deleteLater)
        self.device_list_thread.finished.connect(self._cleanup_device_list_thread)
        self.device_list_thread.finished.connect(self.device_list_thread.deleteLater)

        self.device_list_thread.start()

    def _cleanup_device_list_thread(self):
        self.device_list_thread = None
        self.device_list_worker = None

    def _on_device_list_ready(self, payload):
        self.phone_backup_view.populate_history(payload.get("history") or [])

        rows = payload.get("devices") or []
        self.devices_table.setRowCount(len(rows))

        for row, entry in enumerate(rows):
            device = entry["device"]
            last_run = entry.get("last_run") or {}

            device_name = device.get("device_name", "Unknown")
            device_type = device.get("device_type", "unknown")
            last_backup = device.get("last_backup", "Never")
            file_count = last_run.get("file_count", 0)
            total_size = last_run.get("size_bytes", 0)

            self.devices_table.setItem(row, 0, QTableWidgetItem(device_name))
            self.devices_table.setItem(row, 1, QTableWidgetItem(device_type))
            self.devices_table.setItem(row, 2, QTableWidgetItem(last_backup[:10] if last_backup else "Never"))
            self.devices_table.setItem(row, 3, QTableWidgetItem(str(file_count)))
            self.devices_table.setItem(row, 4, QTableWidgetItem(self._format_bytes(total_size)))

            actions_layout = QHBoxLayout()
            actions_layout.setContentsMargins(0, 0, 0, 0)
            actions_layout.setSpacing(4)

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

        if self._pending_detect_phones:
            self._pending_detect_phones = False
            self.detect_connected_phones()

    def detect_connected_phones(self):
        """Detect connected phones in a background thread to prevent UI freezing."""
        if self._thread_is_running(self.phone_detection_thread):
            return

        if self.phones_table.rowCount() == 0:
            self.phones_table.setRowCount(1)
            self.phones_table.setItem(0, 0, QTableWidgetItem("Scanning..."))
            self.phones_table.setItem(0, 1, QTableWidgetItem("-"))
            self.phones_table.setItem(0, 2, QTableWidgetItem("-"))
            self.phones_table.setItem(0, 3, QTableWidgetItem("-"))
            self.phones_table.setItem(0, 4, QTableWidgetItem("-"))

        self.phone_detection_thread = QThread()
        self.phone_detection_worker = PhoneDetectionWorker(self.device_manager)
        self.phone_detection_worker.moveToThread(self.phone_detection_thread)

        self.phone_detection_thread.started.connect(self.phone_detection_worker.run)
        self.phone_detection_worker.finished.connect(self.on_phone_detection_finished)
        self.phone_detection_worker.finished.connect(self.phone_detection_thread.quit)
        self.phone_detection_worker.finished.connect(self.phone_detection_worker.deleteLater)
        self.phone_detection_thread.finished.connect(self._cleanup_phone_detection_thread)
        self.phone_detection_thread.finished.connect(self.phone_detection_thread.deleteLater)

        self.phone_detection_thread.start()

    def on_phone_detection_finished(self, phones):
        # Sort explicitly by phone name (label)
        phones.sort(key=lambda p: p.get("label", "").lower())

        self.phones_table.setRowCount(len(phones))

        for row, phone in enumerate(phones):
            phone_name = phone.get("label", "Unknown Phone")
            mount_point = phone.get("path", phone.get("drive_letter", "Unknown"))

            self.phones_table.setItem(row, 0, QTableWidgetItem(phone_name))
            self.phones_table.setItem(row, 1, QTableWidgetItem(mount_point))
            self.phones_table.setItem(row, 2, QTableWidgetItem("Ready"))

            # Free space was resolved on the worker thread (see PhoneDetectionWorker).
            free_bytes = phone.get("available_space")
            if free_bytes is None:
                available = "N/A" if str(mount_point).startswith("::") else "Unknown"
            else:
                available = self._format_bytes(free_bytes)

            self.phones_table.setItem(row, 3, QTableWidgetItem(available))

            actions_layout = QHBoxLayout()
            actions_layout.setContentsMargins(0, 0, 0, 0)
            actions_layout.setSpacing(4)

            backup_phone_btn = QPushButton("Backup Phone")
            backup_phone_btn.clicked.connect(
                lambda checked, mp=mount_point, pn=phone_name: self.backup_phone(mp, pn)
            )
            actions_layout.addWidget(backup_phone_btn)

            actions_widget = QWidget()
            actions_widget.setLayout(actions_layout)
            self.phones_table.setCellWidget(row, 4, actions_widget)

    def _cleanup_phone_detection_thread(self):
        self.phone_detection_thread = None
        self.phone_detection_worker = None

    def _thread_is_running(self, thread):
        if thread is None:
            return False

        try:
            return thread.isRunning()
        except RuntimeError:
            return False

    def backup_device(self, device_id):
        """Initiate backup for a device."""
        device = self.device_manager.get_device_by_id(device_id)
        if not device:
            QMessageBox.warning(self, "Error", "Device not found.")
            return

        device_name = device.get("device_name")
        device_type = device.get("device_type")

        if device_type == "phone":
            # Detection happens on a background thread inside backup_phone();
            # passing no path means "match this registered phone by name".
            self.tabs.setCurrentIndex(1)
            self.backup_phone(None, device_name)
            return

        self.backup_requested.emit(device_name, device_type)
        QMessageBox.information(self, "Backup", f"Starting backup for {device_name}...")

    def backup_phone(self, phone_path: Optional[str], phone_name: str):
        """Initiate backup for a phone.

        Phone detection runs off the GUI thread; the actual match-and-start
        happens in a callback once the connected-device list is ready.
        """
        # Check if phone is already registered to avoid duplicates
        existing = self.device_manager.get_device_by_name(phone_name)
        if not existing:
            self.device_manager.register_device(
                phone_name,
                "phone",
                phone_os="android",
            )

        # Switch to the Phone Backup tab
        self.tabs.setCurrentIndex(1)

        def _start_matched_phone():
            for i in range(self.phone_backup_view.phone_list.count()):
                item = self.phone_backup_view.phone_list.item(i)
                if item is None:
                    continue

                device = item.data(Qt.ItemDataRole.UserRole)
                if not device:
                    continue

                dev_path = device.shell_path or device.drive_root
                if (phone_path and dev_path == phone_path) or device.name == phone_name:
                    self.phone_backup_view.phone_list.setCurrentRow(i)
                    self.phone_backup_view._start_or_stop()
                    return

            QMessageBox.warning(
                self,
                "Phone Not Connected",
                f"Phone '{phone_name}' is not currently connected.\n"
                "Please connect your phone via USB and enable File Transfer (MTP) mode.",
            )

        self.phone_backup_view.detect_phones(on_complete=_start_matched_phone)
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
        return human_bytes(bytes_value, 2)
