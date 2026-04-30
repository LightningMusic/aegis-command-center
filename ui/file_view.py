from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import (
    QComboBox,
    QLabel,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.scan_worker import ScanWorker


def _format_gb(size_bytes):
    return f"{round(size_bytes / (1024**3), 2)} GB"


def _format_mb(size_bytes):
    return f"{round(size_bytes / (1024**2), 2)} MB"


class FilesView(QWidget):
    def __init__(self, file_manager):
        super().__init__()

        self.file_manager = file_manager
        self.scan_thread = None
        self.worker = None
        self.drive_tabs = {}

        self.main_layout = QVBoxLayout()
        self.setLayout(self.main_layout)

        title = QLabel("Aegis Storage Intelligence")
        title.setStyleSheet("font-size: 26px; font-weight: 600;")
        self.main_layout.addWidget(title)

        self.drive_selector = QComboBox()
        self.drive_selector.addItem("C Drive Only")
        self.drive_selector.addItem("All Drives")
        self.main_layout.addWidget(self.drive_selector)

        self.scan_button = QPushButton("Start Scan")
        self.scan_button.clicked.connect(self.run_scan)
        self.main_layout.addWidget(self.scan_button)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("Idle")
        self.main_layout.addWidget(self.progress_bar)

        self.tabs = QTabWidget()
        self.main_layout.addWidget(self.tabs)

        self._build_drive_tabs()
        self.load_dashboard()

    def _build_drive_tabs(self):
        for drive in self.file_manager.get_available_drives():
            inner_tabs = QTabWidget()
            sections = {}

            for tab_name in ("Overview", "Folders", "Duplicates", "Steam", "Cleanup"):
                widget = QTextEdit()
                widget.setReadOnly(True)
                widget.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
                inner_tabs.addTab(widget, tab_name)
                sections[tab_name.lower()] = widget

            self.tabs.addTab(inner_tabs, drive)
            self.drive_tabs[drive] = sections

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
        drives = ["C:\\"] if option == "C Drive Only" else self.file_manager.get_available_drives()

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

    def update_progress(self, count):
        self.progress_bar.setFormat(f"Indexed {count} files")
        self.progress_bar.setValue(min(count % 100, 100))

    def on_scan_finished(self, total_files):
        self.scan_button.setEnabled(True)
        self.progress_bar.setValue(100)
        self.progress_bar.setFormat(f"Scan Complete - {total_files} files")
        self.load_dashboard()

    def _render_overview(self, widget, drive):
        widget.clear()
        overview = self.file_manager.get_drive_overview(drive)
        widget.append(f"Drive: {drive}")
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
            "Cleanup suggestions are conservative: system files, app files, game installs, and project files are filtered out."
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

        widget.append("Exact duplicates use full file content hashes. Probable duplicates use sampled signatures or matching size/name when a full hash is unavailable.")

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
