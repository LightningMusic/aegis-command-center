from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QProgressBar,
    QTabWidget,
    QTextEdit
)

from PyQt6.QtCore import QThread
from ui.scan_worker import ScanWorker


class FilesView(QWidget):
    def __init__(self, file_manager):
        super().__init__()

        self.file_manager = file_manager
        self.scan_thread = None
        self.worker = None

        self.main_layout = QVBoxLayout()
        self.setLayout(self.main_layout)

        title = QLabel("Aegis Storage Intelligence")
        title.setStyleSheet("font-size: 26px; font-weight: 600;")
        self.main_layout.addWidget(title)

        self.drive_selector = QComboBox()
        self.drive_selector.addItem("C Drive Only")
        self.drive_selector.addItem("All Drives")
        self.main_layout.addWidget(self.drive_selector)

        self.scan_button = QPushButton("Scan")
        self.scan_button.clicked.connect(self.run_scan)
        self.main_layout.addWidget(self.scan_button)

        self.progress_bar = QProgressBar()
        self.main_layout.addWidget(self.progress_bar)

        # Tabs
        self.tabs = QTabWidget()
        self.main_layout.addWidget(self.tabs)

        # Create tab content
        self.overview_tab = QTextEdit()
        self.folder_tab = QTextEdit()
        self.duplicate_tab = QTextEdit()
        self.steam_tab = QTextEdit()
        self.cleanup_tab = QTextEdit()

        for tab in [
            self.overview_tab,
            self.folder_tab,
            self.duplicate_tab,
            self.steam_tab,
            self.cleanup_tab,
        ]:
            tab.setReadOnly(True)

        self.tabs.addTab(self.overview_tab, "Overview")
        self.tabs.addTab(self.folder_tab, "Folders")
        self.tabs.addTab(self.duplicate_tab, "Duplicates")
        self.tabs.addTab(self.steam_tab, "Steam")
        self.tabs.addTab(self.cleanup_tab, "Cleanup")

        self.load_dashboard()

    # -------------------------
    # SCANNING
    # -------------------------

    def run_scan(self):
        self.scan_button.setEnabled(False)
        self.progress_bar.setValue(0)

        option = self.drive_selector.currentText()
        drives = ["C:\\"] if option == "C Drive Only" else self.file_manager.get_available_drives()

        self.scan_thread = QThread()
        self.worker = ScanWorker(self.file_manager, drives)
        self.worker.moveToThread(self.scan_thread)

        self.scan_thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.update_progress)
        self.worker.finished.connect(self.scan_finished)

        self.worker.finished.connect(self.scan_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.scan_thread.finished.connect(self.scan_thread.deleteLater)

        self.scan_thread.start()

    def update_progress(self, count):
        self.progress_bar.setFormat(f"Indexed {count} files")
        self.progress_bar.setValue(0)

    def scan_finished(self, total_files):
        self.scan_button.setEnabled(True)
        self.progress_bar.setValue(100)
        self.progress_bar.setFormat("Scan Complete")

        self.load_dashboard()

    # -------------------------
    # DASHBOARD LOAD
    # -------------------------

    def load_dashboard(self):
        self.load_overview()
        self.load_folders()
        self.load_duplicates()
        self.load_steam()
        self.load_cleanup()

    # -------------------------
    # TAB LOADERS
    # -------------------------

    def load_overview(self):
        self.overview_tab.clear()

        total_files = self.file_manager.get_indexed_file_count()
        total_storage = self.file_manager.get_total_storage_used()

        self.overview_tab.append(f"Total Files Indexed: {total_files}")
        self.overview_tab.append(f"Total Storage Indexed: {round(total_storage / (1024**3), 2)} GB\n")

        self.overview_tab.append("Top 5 Largest Files:\n")
        for path, size in self.file_manager.get_largest_files(5):
            size_gb = round(size / (1024**3), 2)
            self.overview_tab.append(f"{size_gb} GB — {path}")

    def load_folders(self):
        self.folder_tab.clear()

        folders = self.file_manager.get_storage_by_folder(10)
        self.folder_tab.append("Top 10 Largest Folders:\n")

        for folder, size in folders:
            size_gb = round(size / (1024**3), 2)
            self.folder_tab.append(f"{size_gb} GB — {folder}")

    def load_duplicates(self):
        self.duplicate_tab.clear()

        duplicates = self.file_manager.get_duplicate_files()

        if duplicates:
            self.duplicate_tab.append("Duplicate Files:\n")
            for path, size, h in duplicates[:20]:
                size_mb = round(size / (1024**2), 2)
                self.duplicate_tab.append(f"{size_mb} MB — {path}")
        else:
            self.duplicate_tab.append("No duplicates found.")

    def load_steam(self):
        self.steam_tab.clear()

        games = self.file_manager.get_steam_games_usage()

        if games:
            self.steam_tab.append("Steam / Game Storage:\n")
            for game, size in games:
                size_gb = round(size / (1024**3), 2)
                self.steam_tab.append(f"{size_gb} GB — {game}")
        else:
            self.steam_tab.append("No Steam installations detected.")

    def load_cleanup(self):
        self.cleanup_tab.clear()

        suggestions = self.file_manager.get_cleanup_suggestions()

        if suggestions:
            self.cleanup_tab.append("Cleanup Suggestions:\n")
            for suggestion in suggestions:
                self.cleanup_tab.append(suggestion)
        else:
            self.cleanup_tab.append("No cleanup suggestions available.")