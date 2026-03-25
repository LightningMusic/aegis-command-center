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
from core.scan_worker import ScanWorker


class FilesView(QWidget):

    def __init__(self, file_manager):
        super().__init__()

        self.file_manager = file_manager
        self.scan_thread = None
        self.worker = None

        self.main_layout = QVBoxLayout()
        self.setLayout(self.main_layout)
        

        # -------------------------
        # TITLE
        # -------------------------

        title = QLabel("Aegis Storage Intelligence")
        title.setStyleSheet("font-size: 26px; font-weight: 600;")
        self.main_layout.addWidget(title)

        # -------------------------
        # DRIVE SELECTOR
        # -------------------------

        self.drive_selector = QComboBox()
        self.drive_selector.addItem("C Drive Only")
        self.drive_selector.addItem("All Drives")
        self.main_layout.addWidget(self.drive_selector)

        # -------------------------
        # SCAN BUTTON
        # -------------------------

        self.scan_button = QPushButton("Start Scan")
        self.scan_button.clicked.connect(self.run_scan)
        self.main_layout.addWidget(self.scan_button)

        # -------------------------
        # PROGRESS BAR
        # -------------------------

        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("Idle")
        self.main_layout.addWidget(self.progress_bar)

        # -------------------------
        # TABS
        # -------------------------

        self.tabs = QTabWidget()
        self.main_layout.addWidget(self.tabs)

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
            self.cleanup_tab
        ]:
            tab.setReadOnly(True)
            tab.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)

        self.drive_tabs = {}

        drives = self.file_manager.get_available_drives()

        for drive in drives:
            inner_tabs = QTabWidget()

            folders = QTextEdit()
            duplicates = QTextEdit()
            steam = QTextEdit()
            cleanup = QTextEdit()

            for t in [folders, duplicates, steam, cleanup]:
                t.setReadOnly(True)

            inner_tabs.addTab(folders, "Folders")
            inner_tabs.addTab(duplicates, "Duplicates")
            inner_tabs.addTab(steam, "Steam")
            inner_tabs.addTab(cleanup, "Cleanup")

            self.tabs.addTab(inner_tabs, drive)

            self.drive_tabs[drive] = {
                "folders": folders,
                "duplicates": duplicates,
                "steam": steam,
                "cleanup": cleanup
            }

        self.load_dashboard()

    # -------------------------
    # SCANNING
    # -------------------------
    def update_status(self, text):
        print(text)  # DEBUG
        self.progress_bar.setFormat(text)
    def run_scan(self):

        
        self.scan_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Starting scan...")
        self.file_manager.remove_missing_files()

        option = self.drive_selector.currentText()

        if option == "C Drive Only":
            drives = ["C:\\"]
        else:
            drives = self.file_manager.get_available_drives()

        self.scan_thread = QThread()
        self.worker = ScanWorker(self.file_manager, drives)
        self.worker.setParent(self)  # Ensure the worker is a child of the view for proper cleanup

        self.worker.moveToThread(self.scan_thread)

        self.scan_thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.update_progress)
        self.worker.finished.connect(lambda total: self.on_scan_finished(total))
        self.worker.status.connect(self.update_status)

        self.worker.finished.connect(self.scan_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.scan_thread.finished.connect(self.scan_thread.deleteLater)

        self.scan_thread.start()
        print("Thread started")

    # -------------------------

    def update_progress(self, count):

        self.progress_bar.setFormat(f"Indexed {count} files")
        self.progress_bar.setValue(min(count % 100, 100))

    # -------------------------

    def on_scan_finished(self, total_files):

        print(f"FINAL COUNT: {total_files}")  # DEBUG

        self.scan_button.setEnabled(True)

        self.progress_bar.setValue(100)
        self.progress_bar.setFormat(f"Scan Complete — {total_files} files")

        self.load_dashboard()

    # -------------------------
    # DASHBOARD
    # -------------------------

    def load_dashboard(self):
        for drive, tabs in self.drive_tabs.items():

            # FOLDERS
            tabs["folders"].clear()
            folders = self.file_manager.get_storage_by_folder(drive)
            for folder, size in folders:
                size_gb = round(size/(1024**3), 2)
                tabs["folders"].append(f"{size_gb} GB — {folder}")

            # DUPLICATES
            tabs["duplicates"].clear()
            duplicates = self.file_manager.get_duplicate_files(drive)
            if duplicates:
                for path, size in duplicates[:20]:
                    size_mb = round(size/(1024**2), 2)
                    tabs["duplicates"].append(f"{size_mb} MB — {path}")
            else:
                tabs["duplicates"].append("No duplicates found.")

            # STEAM
            tabs["steam"].clear()
            games = self.file_manager.get_steam_games_usage(drive)
            if games:
                for game, size in games:
                    size_gb = round(size/(1024**3), 2)
                    tabs["steam"].append(f"{size_gb} GB — {game}")
            else:
                tabs["steam"].append("No Steam libraries detected.")

            # CLEANUP
            tabs["cleanup"].clear()
            suggestions = self.file_manager.get_cleanup_suggestions(drive)
            if suggestions:
                for s in suggestions[:20]:
                    tabs["cleanup"].append(s)
            else:
                tabs["cleanup"].append("No cleanup suggestions.")

    # -------------------------

    def load_overview(self):

        self.overview_tab.clear()
        cursor = self.overview_tab.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)

        total_files = self.file_manager.get_indexed_file_count()
        total_storage = self.file_manager.get_total_storage_used()

        self.overview_tab.append(f"Total Files Indexed: {total_files}")
        self.overview_tab.append(
            f"Total Storage Indexed: {round(total_storage/(1024**3),2)} GB\n"
        )

        self.overview_tab.append("Top 10 Largest Files:\n")

        for path, size in self.file_manager.get_largest_files(10):
            size_gb = round(size/(1024**3),2)
            self.overview_tab.append(f"{size_gb} GB — {path}")

        self.overview_tab.moveCursor(cursor.MoveOperation.Start)

    # -------------------------

    def load_folders(self):

        self.folder_tab.clear()

        folders = self.file_manager.get_storage_by_folder(10)

        for folder, size in folders:
            size_gb = round(size/(1024**3),2)
            self.folder_tab.append(f"{size_gb} GB — {folder}")

        self.folder_tab.moveCursor(self.folder_tab.textCursor().MoveOperation.Start)

    # -------------------------

    def load_duplicates(self):

        self.duplicate_tab.clear()

        duplicates = self.file_manager.get_duplicate_files()

        if duplicates:
            for path, size, _ in duplicates[:20]:
                size_mb = round(size/(1024**2),2)
                self.duplicate_tab.append(f"{size_mb} MB — {path}")
        else:
            self.duplicate_tab.append("No duplicates found.")

        self.duplicate_tab.moveCursor(self.duplicate_tab.textCursor().MoveOperation.Start)

    # -------------------------

    def load_steam(self):

        self.steam_tab.clear()

        games = self.file_manager.get_steam_games_usage()

        if games:
            for game, size in games:
                size_gb = round(size/(1024**3),2)
                self.steam_tab.append(f"{size_gb} GB — {game}")
        else:
            self.steam_tab.append("No Steam libraries detected.")

        self.steam_tab.moveCursor(self.steam_tab.textCursor().MoveOperation.Start)

    # -------------------------

    def load_cleanup(self):

        self.cleanup_tab.clear()

        suggestions = self.file_manager.get_cleanup_suggestions()

        if suggestions:
            for s in suggestions[:20]:
                self.cleanup_tab.append(s)
        else:
            self.cleanup_tab.append("No cleanup suggestions.")

        self.cleanup_tab.moveCursor(self.cleanup_tab.textCursor().MoveOperation.Start)