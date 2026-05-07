from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
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
        self.tools_tabs.setMinimumHeight(360)
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

        self.tools_tabs.addTab(scan_tab, "Scanner")
        self.tools_tabs.addTab(backup_tab, "Backup Manager")

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
        backup_form.addRow("Backup Drive", self.backup_destination)

        self.custom_backup_path = QLineEdit()
        self.custom_backup_path.setPlaceholderText("Optional custom folder, for example E:\\Aegis_Backups")
        backup_form.addRow("Custom Path", self.custom_backup_path)

        self.set_default_destination_box = QCheckBox("Save selected destination as default")
        backup_form.addRow(self.set_default_destination_box)

        backup_layout.addLayout(backup_form)

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
        self.backup_output.setMinimumHeight(180)
        backup_layout.addWidget(self.backup_output)

        backup_tab_layout.addWidget(backup_group)
        backup_tab_layout.addStretch()

        self.tabs = QTabWidget()
        self.tabs.setMinimumHeight(520)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.content_layout.addWidget(self.tabs)
        self.content_layout.setStretchFactor(self.tools_tabs, 1)
        self.content_layout.setStretchFactor(self.tabs, 3)

        self.refresh_drive_panels()
        self.load_dashboard()

    def refresh_drive_panels(self):
        inventory = self.file_manager.get_drive_inventory()
        self._populate_backup_destinations(inventory)
        self._populate_drive_checks(inventory)
        self._rebuild_drive_tabs(inventory)

    def _populate_backup_destinations(self, inventory):
        self.backup_destination.clear()
        backup_settings = self.config_manager.get_backup_settings()
        default_destination = backup_settings.get("default_destination", "")

        for drive in inventory:
            if not drive["is_backup_destination"]:
                continue
            label = (
                f"{drive['root']} | {drive['type_name']} | "
                f"Free { _format_gb(drive['free_bytes']) } | "
                f"{'Writable' if drive['is_writable'] else 'Read-only'}"
            )
            self.backup_destination.addItem(label, drive["root"])

        if default_destination:
            index = self.backup_destination.findData(default_destination)
            if index >= 0:
                self.backup_destination.setCurrentIndex(index)
                self.set_default_destination_box.setChecked(True)

    def _populate_drive_checks(self, inventory):
        while self.drives_container.count():
            item = self.drives_container.takeAt(0)
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
            self.backup_status.setText(
                f"Backup completed to {result['backup_root']}.\n"
                f"Copied {result['copied_files']} files totaling {_format_bytes(result['copied_bytes'])}.\n"
                f"Log: {result['log_file']}"
            )
        self.backup_output.setPlainText(
            "\n".join(
                [
                    f"Status: {'Cancelled' if result.get('cancelled') else 'Completed'}",
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

    def _selected_backup_destination(self):
        custom = self.custom_backup_path.text().strip()
        if custom:
            return custom
        return self.backup_destination.currentData() or ""

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
