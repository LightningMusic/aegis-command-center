from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.scan_worker import ScanWorker
from core.phone_backup_manager import PhoneBackupManager
from ui.phone_backup_view import PhoneBackupView


def _format_gb(size_bytes):
    return f"{round(size_bytes / (1024**3), 2)} GB"


def _format_bytes(size_bytes):
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes or 0)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024


class BackupWorker(QObject):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, backup_manager, mode, drives, destination, set_default):
        super().__init__()
        self.backup_manager = backup_manager
        self.mode = mode
        self.drives = drives
        self.destination = destination
        self.set_default = set_default
        self._stop_requested = False

    def run(self):
        try:
            result = self.backup_manager.run_backup(
                self.mode,
                self.drives,
                self.destination,
                progress_callback=self._report_progress,
                should_stop=self.should_stop,
            )
            if self.set_default and self.destination:
                self.backup_manager.set_default_destination(self.destination)
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _report_progress(self, percent, message):
        self.progress.emit(percent)
        self.status.emit(message)

    def stop(self):
        self._stop_requested = True

    def should_stop(self):
        return self._stop_requested


class FilesView(QWidget):
    def __init__(self, file_manager, config_manager):
        super().__init__()

        self.file_manager = file_manager
        self.config_manager = config_manager
        self.scan_thread = None
        self.worker = None
        self.backup_thread = None
        self.backup_worker = None
        self.drive_tabs = {}
        self.drive_checks = {}
        self._drive_panels_loaded = False

        self.main_layout = QVBoxLayout()
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(self.main_layout)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(
            self.scroll_area.horizontalScrollBarPolicy().ScrollBarAlwaysOff
        )
        self.main_layout.addWidget(self.scroll_area)

        self.content = QWidget()
        self.scroll_area.setWidget(self.content)

        self.content_layout = QVBoxLayout()
        self.content_layout.setContentsMargins(18, 18, 18, 18)
        self.content_layout.setSpacing(12)
        self.content.setLayout(self.content_layout)

        title = QLabel("Aegis Storage Intelligence")
        title.setStyleSheet("font-size: 26px; font-weight: 600;")
        self.content_layout.addWidget(title)

        intro = QLabel("Review storage, scan drives, and run backups from one place.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #555; font-size: 13px;")
        self.content_layout.addWidget(intro)

        self.tools_tabs = QTabWidget()
        self.tools_tabs.setMinimumHeight(220)
        self.content_layout.addWidget(self.tools_tabs)

        scan_tab = QWidget()
        scan_tab_layout = QVBoxLayout()
        scan_tab_layout.setContentsMargins(8, 8, 8, 8)
        scan_tab_layout.setSpacing(12)
        scan_tab.setLayout(scan_tab_layout)

        backup_tab = QWidget()
        backup_tab_layout = QVBoxLayout()
        backup_tab_layout.setContentsMargins(8, 8, 8, 8)
        backup_tab_layout.setSpacing(12)
        backup_tab.setLayout(backup_tab_layout)

        merge_tab = QWidget()
        merge_tab_layout = QVBoxLayout()
        merge_tab_layout.setContentsMargins(8, 8, 8, 8)
        merge_tab_layout.setSpacing(12)
        merge_tab.setLayout(merge_tab_layout)

        self.tools_tabs.addTab(scan_tab, "Scanner")
        self.tools_tabs.addTab(backup_tab, "Backup Manager")
        self.tools_tabs.addTab(merge_tab, "Merge Folders")

        scan_group = QGroupBox("Scanner")
        scan_layout = QVBoxLayout()
        scan_group.setLayout(scan_layout)

        scan_help = QLabel("Choose which drives to index.")
        scan_help.setWordWrap(True)
        scan_help.setStyleSheet("color: #666;")
        scan_layout.addWidget(scan_help)

        self.drive_selector = QComboBox()
        self.drive_selector.addItem("System Drive Only")
        self.drive_selector.addItem("All Connected Drives")
        scan_layout.addWidget(self.drive_selector)

        self.scan_button = QPushButton("Start Scan")
        self.scan_button.clicked.connect(self.run_scan)
        scan_layout.addWidget(self.scan_button)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("Idle")
        scan_layout.addWidget(self.progress_bar)

        scan_tab_layout.addWidget(scan_group)
        scan_tab_layout.addStretch()

        backup_group = QGroupBox("Backup Manager")
        backup_layout = QVBoxLayout()
        backup_group.setLayout(backup_layout)

        backup_help = QLabel(
            "Pick a backup mode, choose a destination, and start or stop safely."
        )
        backup_help.setWordWrap(True)
        backup_help.setStyleSheet("color: #666;")
        backup_layout.addWidget(backup_help)

        backup_form = QFormLayout()
        self.backup_mode = QComboBox()
        self.backup_mode.addItem("Drive Mirror Backup", "mirror")
        self.backup_mode.addItem("Loose/User Files Backup", "loose")
        self.backup_mode.addItem("Windows Backup", "windows")
        backup_form.addRow("Backup Mode", self.backup_mode)

        self.backup_destination = QComboBox()
        self.backup_destination.currentIndexChanged.connect(self._destination_changed)
        backup_form.addRow("Saved Location", self.backup_destination)

        self.custom_backup_path = QLineEdit()
        self.custom_backup_path.setPlaceholderText("Enter a drive or folder path, for example D:\\ or E:\\Archive\\Backups")
        backup_form.addRow("Path", self.custom_backup_path)

        self.set_default_destination_box = QCheckBox("Save selected destination as default")
        backup_form.addRow(self.set_default_destination_box)

        backup_layout.addLayout(backup_form)

        destination_buttons = QHBoxLayout()
        self.save_location_button = QPushButton("Save Location")
        self.save_location_button.clicked.connect(self.save_backup_location)
        destination_buttons.addWidget(self.save_location_button)

        self.remove_location_button = QPushButton("Remove Location")
        self.remove_location_button.clicked.connect(self.remove_backup_location)
        destination_buttons.addWidget(self.remove_location_button)
        destination_buttons.addStretch()
        backup_layout.addLayout(destination_buttons)

        drives_group = QGroupBox("Backup Source Drives")
        drives_layout = QVBoxLayout()
        drives_group.setLayout(drives_layout)
        self.drives_container = drives_layout
        backup_layout.addWidget(drives_group)

        backup_buttons = QHBoxLayout()
        self.refresh_drives_button = QPushButton("Refresh Drives")
        self.refresh_drives_button.clicked.connect(self.refresh_drive_panels)
        backup_buttons.addWidget(self.refresh_drives_button)

        self.verify_destination_button = QPushButton("Verify Destination")
        self.verify_destination_button.clicked.connect(self.verify_backup_destination)
        backup_buttons.addWidget(self.verify_destination_button)

        self.run_backup_button = QPushButton("Start Backup")
        self.run_backup_button.clicked.connect(self.run_backup)
        backup_buttons.addWidget(self.run_backup_button)
        backup_buttons.addStretch()
        backup_layout.addLayout(backup_buttons)

        self.backup_status = QLabel("No backups run yet.")
        self.backup_status.setWordWrap(True)
        backup_layout.addWidget(self.backup_status)

        self.backup_output = QTextEdit()
        self.backup_output.setReadOnly(True)
        self.backup_output.setMinimumHeight(100)
        backup_layout.addWidget(self.backup_output)

        merge_group = QGroupBox("Saved Backup Folders")
        merge_layout = QVBoxLayout()
        merge_group.setLayout(merge_layout)

        merge_help = QLabel("Merge one saved backup folder into another.")
        merge_help.setWordWrap(True)
        merge_help.setStyleSheet("color: #666;")
        merge_layout.addWidget(merge_help)

        merge_form = QFormLayout()
        self.merge_source = QComboBox()
        merge_form.addRow("Source Folder", self.merge_source)
        self.merge_target = QComboBox()
        merge_form.addRow("Target Folder", self.merge_target)
        merge_layout.addLayout(merge_form)

        merge_buttons = QHBoxLayout()
        self.refresh_merge_button = QPushButton("Refresh Folders")
        self.refresh_merge_button.clicked.connect(self.refresh_merge_folders)
        merge_buttons.addWidget(self.refresh_merge_button)

        self.merge_button = QPushButton("Merge Folders")
        self.merge_button.clicked.connect(self.merge_backup_folders)
        merge_buttons.addWidget(self.merge_button)
        merge_buttons.addStretch()
        merge_layout.addLayout(merge_buttons)

        self.merge_status = QLabel("Choose a saved location to see backup folders.")
        self.merge_status.setWordWrap(True)
        merge_layout.addWidget(self.merge_status)

        backup_layout.addWidget(merge_group)

        backup_tab_layout.addWidget(backup_group)
        backup_tab_layout.addStretch()

        merge_manager_group = QGroupBox("Merge Folders")
        merge_manager_layout = QVBoxLayout()
        merge_manager_group.setLayout(merge_manager_layout)

        merge_manager_help = QLabel("Pick any source and target folders, then merge newer or missing files into the target.")
        merge_manager_help.setWordWrap(True)
        merge_manager_help.setStyleSheet("color: #666;")
        merge_manager_layout.addWidget(merge_manager_help)

        merge_paths_form = QFormLayout()

        source_row = QHBoxLayout()
        self.merge_source_path = QLineEdit()
        self.merge_source_path.setPlaceholderText("Source folder path")
        source_row.addWidget(self.merge_source_path)
        self.browse_merge_source_button = QPushButton("Browse")
        self.browse_merge_source_button.clicked.connect(self.browse_merge_source)
        source_row.addWidget(self.browse_merge_source_button)
        merge_paths_form.addRow("Source Folder", source_row)

        target_row = QHBoxLayout()
        self.merge_target_path = QLineEdit()
        self.merge_target_path.setPlaceholderText("Target folder path")
        target_row.addWidget(self.merge_target_path)
        self.browse_merge_target_button = QPushButton("Browse")
        self.browse_merge_target_button.clicked.connect(self.browse_merge_target)
        target_row.addWidget(self.browse_merge_target_button)
        merge_paths_form.addRow("Target Folder", target_row)

        merge_manager_layout.addLayout(merge_paths_form)

        merge_manager_buttons = QHBoxLayout()
        self.use_saved_source_button = QPushButton("Use Saved Source")
        self.use_saved_source_button.clicked.connect(self.use_saved_merge_source)
        merge_manager_buttons.addWidget(self.use_saved_source_button)

        self.use_saved_target_button = QPushButton("Use Saved Target")
        self.use_saved_target_button.clicked.connect(self.use_saved_merge_target)
        merge_manager_buttons.addWidget(self.use_saved_target_button)

        self.run_folder_merge_button = QPushButton("Merge Now")
        self.run_folder_merge_button.clicked.connect(self.run_folder_merge)
        merge_manager_buttons.addWidget(self.run_folder_merge_button)
        merge_manager_buttons.addStretch()
        merge_manager_layout.addLayout(merge_manager_buttons)

        self.full_merge_status = QLabel("Choose folders to merge.")
        self.full_merge_status.setWordWrap(True)
        merge_manager_layout.addWidget(self.full_merge_status)

        self.full_merge_output = QTextEdit()
        self.full_merge_output.setReadOnly(True)
        self.full_merge_output.setMinimumHeight(120)
        merge_manager_layout.addWidget(self.full_merge_output)

        merge_tab_layout.addWidget(merge_manager_group)
        merge_tab_layout.addStretch()

        self.tabs = QTabWidget()
        self.tabs.setMinimumHeight(260)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.content_layout.addWidget(self.tabs)
        self.content_layout.setStretchFactor(self.tools_tabs, 1)
        self.content_layout.setStretchFactor(self.tabs, 3)

        self._show_drive_placeholder()

    def _show_drive_placeholder(self):
        self.tabs.clear()
        placeholder = QTextEdit()
        placeholder.setReadOnly(True)
        placeholder.setPlainText("Drive inventory will load when File Organizer is opened.")
        self.tabs.addTab(placeholder, "Drives")

    def ensure_drive_panels_loaded(self):
        if not self._drive_panels_loaded:
            self.refresh_drive_panels()

    def refresh_drive_panels(self):
        inventory = self.file_manager.get_drive_inventory()
        self._populate_backup_destinations(inventory)
        self._populate_drive_checks(inventory)
        self._rebuild_drive_tabs(inventory)
        self.refresh_merge_folders()
        self._drive_panels_loaded = True
        self.load_dashboard()

    def _populate_backup_destinations(self, inventory):
        current_path = self._selected_backup_destination()
        self.backup_destination.clear()
        if not self.file_manager.backup_manager:
            return

        choices = self.file_manager.backup_manager.get_destination_choices()
        inventory_map = {item["root"]: item for item in inventory}
        default_destination = self.file_manager.backup_manager.get_default_destination()

        for path in choices:
            drive_root = f"{path[:2]}\\"
            drive = inventory_map.get(drive_root)
            if drive:
                label = (
                    f"{path} | {drive['type_name']} | "
                    f"Free {_format_gb(drive['free_bytes'])}"
                )
            else:
                label = path
            self.backup_destination.addItem(label, path)

        preferred = current_path or default_destination
        if preferred:
            index = self.backup_destination.findData(preferred)
            if index >= 0:
                self.backup_destination.setCurrentIndex(index)
                self.custom_backup_path.setText(preferred)
        self.set_default_destination_box.setChecked(bool(default_destination))

    def _populate_drive_checks(self, inventory):
        while self.drives_container.count():
            item = self.drives_container.takeAt(0)
            if item is None:
                continue

            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self.drive_checks = {}

        for drive in inventory:
            if not drive["is_scan_eligible"]:
                continue

            box = QCheckBox(
                f"{drive['root']} ({drive['type_name']}) - "
                f"Free {_format_gb(drive['free_bytes'])} of {_format_gb(drive['total_bytes'])}"
            )
            box.setChecked(drive["root"] == "C:\\" or drive["type_name"] == "Fixed")

            self.drives_container.addWidget(box)
            self.drive_checks[drive["root"]] = box


    def _rebuild_drive_tabs(self, inventory):
        self.tabs.clear()
        self.drive_tabs = {}

        for drive in inventory:
            if not drive["is_scan_eligible"]:
                continue
            inner_tabs = QTabWidget()
            sections = {}

            for tab_name in ("Overview", "Folders", "Duplicates", "Steam", "Cleanup"):
                widget = QTextEdit()
                widget.setReadOnly(True)
                widget.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
                inner_tabs.addTab(widget, tab_name)
                sections[tab_name.lower()] = widget

            self.tabs.addTab(inner_tabs, drive["root"])
            self.drive_tabs[drive["root"]] = sections

    def update_status(self, text):
        self.progress_bar.setFormat(text)

    def run_scan(self):
        if self.scan_thread is not None:
            return

        self.scan_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Starting scan...")
        self.file_manager.remove_missing_files()

        option = self.drive_selector.currentText()
        drives = ["C:\\"] if option == "System Drive Only" else self.file_manager.get_available_drives()

        self.scan_thread = QThread()
        self.worker = ScanWorker(self.file_manager, drives)
        self.worker.moveToThread(self.scan_thread)

        self.scan_thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.update_progress)
        self.worker.status.connect(self.update_status)
        self.worker.finished.connect(self.on_scan_finished)
        self.worker.finished.connect(self.scan_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.scan_thread.finished.connect(self._cleanup_scan_thread)
        self.scan_thread.finished.connect(self.scan_thread.deleteLater)

        self.scan_thread.start()

    def _cleanup_scan_thread(self):
        self.scan_thread = None
        self.worker = None

    def _cleanup_backup_thread(self):
        self.backup_thread = None
        self.backup_worker = None
        self.run_backup_button.setEnabled(True)
        self.run_backup_button.setText("Start Backup")
        self.verify_destination_button.setEnabled(True)
        self.refresh_drives_button.setEnabled(True)

    def update_progress(self, count):
        self.progress_bar.setFormat(f"Indexed {count} files")
        self.progress_bar.setValue(min(count % 100, 100))

    def on_scan_finished(self, total_files):
        self.scan_button.setEnabled(True)
        self.progress_bar.setValue(100)
        self.progress_bar.setFormat(f"Scan Complete - {total_files} files")
        self.refresh_drive_panels()
        self.load_dashboard()

    def verify_backup_destination(self):
        if not self.file_manager.backup_manager:
            QMessageBox.warning(self, "Backup", "Backup manager is not available.")
            return

        destination = self._selected_backup_destination()
        selected_drives = self._selected_backup_drives()
        mode = self.backup_mode.currentData()
        estimated_bytes = self.file_manager.backup_manager.estimate_backup_size(mode, selected_drives)
        ok, message, usage = self.file_manager.backup_manager.validate_destination(
            destination,
            estimated_bytes,
        )

        usage_message = ""
        if usage:
            usage_message = (
                f"\nFree space: {_format_bytes(usage.free)}"
                f"\nEstimated backup size: {_format_bytes(estimated_bytes)}"
            )

        self.backup_status.setText(message + usage_message)
        if ok:
            QMessageBox.information(self, "Backup Destination", message + usage_message)
        else:
            QMessageBox.warning(self, "Backup Destination", message + usage_message)

    def save_backup_location(self):
        if not self.file_manager.backup_manager:
            return

        destination = self._selected_backup_destination()
        try:
            self.file_manager.backup_manager.save_destination(
                destination,
                set_default=self.set_default_destination_box.isChecked(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Save Location", str(exc))
            return

        self.backup_status.setText(f"Saved backup location: {destination}")
        self.refresh_drive_panels()

    def remove_backup_location(self):
        if not self.file_manager.backup_manager:
            return

        destination = self.backup_destination.currentData()
        if not destination:
            QMessageBox.warning(self, "Remove Location", "Select a saved location first.")
            return

        self.file_manager.backup_manager.remove_saved_destination(destination)
        self.backup_status.setText(f"Removed saved location: {destination}")
        self.custom_backup_path.clear()
        self.refresh_drive_panels()

    def run_backup(self):
        if self.backup_thread is not None and self.backup_worker is not None:
            self.backup_worker.stop()
            self.run_backup_button.setEnabled(False)
            self.progress_bar.setFormat("Stopping backup...")
            self.backup_status.setText("Stopping backup after the current file finishes...")
            return

        if self.backup_thread is not None:
            return

        if not self.file_manager.backup_manager:
            QMessageBox.warning(self, "Backup", "Backup manager is not available.")
            return

        selected_drives = self._selected_backup_drives()
        if not selected_drives:
            QMessageBox.warning(self, "Backup", "Select at least one source drive.")
            return

        destination = self._selected_backup_destination()
        mode = self.backup_mode.currentData()

        self.run_backup_button.setEnabled(True)
        self.run_backup_button.setText("Stop Backup")
        self.verify_destination_button.setEnabled(False)
        self.refresh_drives_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Preparing backup...")
        self.backup_output.clear()

        self.backup_thread = QThread()
        self.backup_worker = BackupWorker(
            self.file_manager.backup_manager,
            mode,
            selected_drives,
            destination,
            self.set_default_destination_box.isChecked(),
        )
        self.backup_worker.moveToThread(self.backup_thread)

        self.backup_thread.started.connect(self.backup_worker.run)
        self.backup_worker.progress.connect(self.progress_bar.setValue)
        self.backup_worker.status.connect(self.progress_bar.setFormat)
        self.backup_worker.finished.connect(self.on_backup_finished)
        self.backup_worker.failed.connect(self.on_backup_failed)
        self.backup_worker.finished.connect(self.backup_thread.quit)
        self.backup_worker.failed.connect(self.backup_thread.quit)
        self.backup_worker.finished.connect(self.backup_worker.deleteLater)
        self.backup_worker.failed.connect(self.backup_worker.deleteLater)
        self.backup_thread.finished.connect(self._cleanup_backup_thread)
        self.backup_thread.finished.connect(self.backup_thread.deleteLater)

        self.backup_thread.start()

    def on_backup_finished(self, result):
        self.progress_bar.setValue(100)
        if result.get("cancelled"):
            self.progress_bar.setFormat("Backup stopped")
            self.backup_status.setText(
                f"Backup stopped safely.\n"
                f"Copied {result['copied_files']} files totaling {_format_bytes(result['copied_bytes'])}.\n"
                f"Partial log: {result['log_file']}"
            )
        else:
            self.progress_bar.setFormat("Backup complete")
            reuse_note = "Reused existing backup set." if result.get("reused_existing_set") else "Created new backup set."
            self.backup_status.setText(
                f"Backup completed to {result['backup_root']}.\n"
                f"{reuse_note}\n"
                f"Copied {result['copied_files']} files totaling {_format_bytes(result['copied_bytes'])}.\n"
                f"Log: {result['log_file']}"
            )
        self.backup_output.setPlainText(
            "\n".join(
                [
                    f"Status: {'Cancelled' if result.get('cancelled') else 'Completed'}",
                    f"Backup set: {'Reused existing' if result.get('reused_existing_set') else 'Created new'}",
                    f"Destination root: {result['destination_root']}",
                    f"Backup folder: {result['backup_root']}",
                    f"Copied files: {result['copied_files']}",
                    f"Copied bytes: {_format_bytes(result['copied_bytes'])}",
                    f"Skipped files: {result['skipped_files']}",
                    f"Errors: {result['error_count']}",
                    f"Estimated size: {_format_bytes(result['estimated_bytes'])}",
                    f"Free space before: {_format_bytes(result['free_space_before_bytes'])}",
                    f"Log file: {result['log_file']}",
                ]
            )
        )
        self.refresh_drive_panels()

    def on_backup_failed(self, error_message):
        self.progress_bar.setFormat("Backup failed")
        self.backup_status.setText(error_message)
        self.run_backup_button.setText("Start Backup")
        QMessageBox.warning(self, "Backup Failed", error_message)

    def refresh_merge_folders(self):
        self.merge_source.clear()
        self.merge_target.clear()

        if not self.file_manager.backup_manager:
            return

        destination = self._selected_backup_destination()
        if not destination:
            self.merge_status.setText("Choose a saved location to see backup folders.")
            return

        folders = self.file_manager.backup_manager.list_backup_folders(destination)
        for folder in folders:
            self.merge_source.addItem(folder, folder)
            self.merge_target.addItem(folder, folder)

        if folders:
            if len(folders) > 1:
                self.merge_target.setCurrentIndex(1)
            self.merge_status.setText(f"Found {len(folders)} backup folders.")
        else:
            self.merge_status.setText("No backup folders found in this location yet.")

    def merge_backup_folders(self):
        if not self.file_manager.backup_manager:
            return

        destination = self._selected_backup_destination()
        if not destination:
            QMessageBox.warning(self, "Merge Folders", "Choose a saved backup location first.")
            return

        try:
            result = self.file_manager.backup_manager.merge_backup_folders(
                destination,
                self.merge_source.currentData(),
                self.merge_target.currentData(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Merge Folders", str(exc))
            return

        message = (
            f"Merged into {result['target_folder']} from {result['source_folder']}.\n"
            f"Files merged: {result['merged_files']}\n"
            f"Files skipped: {result['skipped_files']}\n"
            f"Data copied: {_format_bytes(result['copied_bytes'])}\n"
            f"Log: {result['log_file']}"
        )
        self.merge_status.setText(message)
        self.backup_output.setPlainText(message)
        self.refresh_merge_folders()

    def browse_merge_source(self):
        selected = QFileDialog.getExistingDirectory(self, "Choose Source Folder")
        if selected:
            self.merge_source_path.setText(selected)

    def browse_merge_target(self):
        selected = QFileDialog.getExistingDirectory(self, "Choose Target Folder")
        if selected:
            self.merge_target_path.setText(selected)

    def use_saved_merge_source(self):
        destination = self._selected_backup_destination()
        folder = self.merge_source.currentData()
        if not destination or not folder:
            QMessageBox.warning(self, "Use Saved Source", "Choose a saved backup location and source folder first.")
            return

        root_name = self.config_manager.get_backup_settings().get("backup_root_name", "Aegis_Backups")
        self.merge_source_path.setText(f"{destination}\\{root_name}\\{folder}")

    def use_saved_merge_target(self):
        destination = self._selected_backup_destination()
        folder = self.merge_target.currentData()
        if not destination or not folder:
            QMessageBox.warning(self, "Use Saved Target", "Choose a saved backup location and target folder first.")
            return

        root_name = self.config_manager.get_backup_settings().get("backup_root_name", "Aegis_Backups")
        self.merge_target_path.setText(f"{destination}\\{root_name}\\{folder}")

    def run_folder_merge(self):
        if not self.file_manager.backup_manager:
            return

        source = self.merge_source_path.text().strip()
        target = self.merge_target_path.text().strip()

        try:
            result = self.file_manager.backup_manager.merge_folders(source, target)
        except Exception as exc:
            QMessageBox.warning(self, "Merge Folders", str(exc))
            return

        message = (
            f"Source: {result['source_folder']}\n"
            f"Target: {result['target_folder']}\n"
            f"Files merged: {result['merged_files']}\n"
            f"Files skipped: {result['skipped_files']}\n"
            f"Data copied: {_format_bytes(result['copied_bytes'])}\n"
            f"Log: {result['log_file']}"
        )
        self.full_merge_status.setText("Folder merge completed.")
        self.full_merge_output.setPlainText(message)

    def _selected_backup_destination(self):
        custom = self.custom_backup_path.text().strip()
        if custom:
            return custom
        return self.backup_destination.currentData() or ""

    def _destination_changed(self):
        selected = self.backup_destination.currentData()
        if selected:
            self.custom_backup_path.setText(selected)
        self.refresh_merge_folders()

    def _selected_backup_drives(self):
        return [drive for drive, box in self.drive_checks.items() if box.isChecked()]

    def _render_overview(self, widget, drive):
        widget.clear()
        overview = self.file_manager.get_drive_overview(drive)
        drive_meta = next(
            (item for item in self.file_manager.get_drive_inventory() if item["root"] == drive),
            None,
        )
        widget.append(f"Drive: {drive}")
        if drive_meta:
            widget.append(f"Drive Type: {drive_meta['type_name']}")
            widget.append(f"Free Space: {_format_gb(drive_meta['free_bytes'])}")
            widget.append(f"Total Capacity: {_format_gb(drive_meta['total_bytes'])}")
            widget.append(f"Backup Target Ready: {'Yes' if drive_meta['is_writable'] else 'No'}")
            widget.append("")
        widget.append(f"Files Indexed: {overview['file_count']}")
        widget.append(f"Indexed Storage: {_format_gb(overview['total_size_bytes'])}")
        widget.append("")
        widget.append(f"Duplicate Groups Found: {overview['duplicate_groups']}")
        widget.append(
            f"Potential Duplicate Reclaim: {_format_gb(overview['duplicate_reclaimable_bytes'])}"
        )
        widget.append("")
        widget.append(f"Cleanup Candidates: {overview['cleanup_candidates']}")
        widget.append(
            f"Cleanup Candidates Size: {_format_gb(overview['cleanup_candidate_bytes'])}"
        )
        widget.append("")
        widget.append(
            "Downloads are included in Loose/User Files and Windows Backup. Self-backup protection blocks a destination drive from backing itself up."
        )

    def _render_folders(self, widget, drive):
        widget.clear()
        folders = self.file_manager.get_storage_by_folder(drive)
        for folder, size in folders:
            widget.append(f"{_format_gb(size)} - {folder}")

    def _render_duplicates(self, widget, drive):
        widget.clear()
        duplicates = self.file_manager.get_duplicate_files(drive)

        if not duplicates:
            widget.append("No duplicate groups found yet.")
            return

        for index, group in enumerate(duplicates, start=1):
            widget.append(
                f"{index}. {group['match_type']} - {group['file_count']} files - reclaim {_format_gb(group['reclaimable_bytes'])}"
            )
            widget.append(f"Keep: {group['keep_path']}")
            widget.append(f"Drives: {', '.join(group['drives']) or drive}")
            widget.append(f"Risk: {group['risk']}")
            for duplicate_path in group["duplicate_paths"][:4]:
                widget.append(f"Duplicate: {duplicate_path}")
            if len(group["duplicate_paths"]) > 4:
                widget.append(f"... and {len(group['duplicate_paths']) - 4} more")
            widget.append("")

        widget.append(
            "Exact duplicates use full file content hashes. Probable duplicates use sampled signatures or matching size/name when a full hash is unavailable."
        )

    def _render_steam(self, widget, drive):
        widget.clear()
        games = self.file_manager.get_steam_games_usage(drive)
        if games:
            for game, size in games:
                widget.append(f"{_format_gb(size)} - {game}")
        else:
            widget.append("No Steam libraries detected.")

    def _render_cleanup(self, widget, drive):
        widget.clear()
        suggestions = self.file_manager.get_cleanup_suggestions(drive)

        if not suggestions:
            widget.append("No safe cleanup suggestions right now.")
            return

        for index, suggestion in enumerate(suggestions, start=1):
            widget.append(
                f"{index}. {_format_gb(suggestion['size_bytes'])} - {suggestion['path']}"
            )
            widget.append(
                f"Risk: {suggestion['risk']} | Score: {suggestion['score']} | Last used: {suggestion['age_days']} days"
            )
            widget.append("Reasons: " + "; ".join(suggestion["reasons"]))
            widget.append("")

        widget.append(
            "Suggestions exclude known system areas, app/game install locations, and likely project files."
        )

    def load_dashboard(self):
        for drive, tabs in self.drive_tabs.items():
            self._render_overview(tabs["overview"], drive)
            self._render_folders(tabs["folders"], drive)
            self._render_duplicates(tabs["duplicates"], drive)
            self._render_steam(tabs["steam"], drive)
            self._render_cleanup(tabs["cleanup"], drive)
