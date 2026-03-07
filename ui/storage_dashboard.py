from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QTextEdit
)


class StorageDashboard(QWidget):

    def __init__(self, file_manager):
        super().__init__()

        self.file_manager = file_manager

        self.main_layout = QVBoxLayout()
        self.setLayout(self.main_layout)

        title = QLabel("Storage Intelligence Dashboard")
        title.setStyleSheet("font-size: 24px; font-weight: bold;")
        self.main_layout.addWidget(title)

        # sections
        self.summary_box = QTextEdit()
        self.types_box = QTextEdit()
        self.folder_box = QTextEdit()
        self.largest_files_box = QTextEdit()
        self.steam_box = QTextEdit()
        self.cleanup_box = QTextEdit()

        for box in [
            self.summary_box,
            self.types_box,
            self.folder_box,
            self.largest_files_box,
            self.steam_box,
            self.cleanup_box
        ]:
            box.setReadOnly(True)
            self.main_layout.addWidget(box)

        self.refresh_dashboard()

    # -------------------------

    def refresh_dashboard(self):

        self.load_summary()
        self.load_file_types()
        self.load_folders()
        self.load_largest_files()
        self.load_steam()
        self.load_cleanup()

    # -------------------------

    def load_summary(self):

        self.summary_box.clear()

        total_files = self.file_manager.get_indexed_file_count()
        total_storage = self.file_manager.get_total_storage_used()

        self.summary_box.append("SYSTEM SUMMARY\n")
        self.summary_box.append(f"Total Files Indexed: {total_files}")
        self.summary_box.append(
            f"Total Storage Indexed: {round(total_storage / (1024**3), 2)} GB"
        )

    # -------------------------

    def load_file_types(self):

        self.types_box.clear()
        self.types_box.append("\nFILE TYPE BREAKDOWN\n")

        types = self.file_manager.get_extension_breakdown()

        for ext, count, size in types:
            size_gb = round(size / (1024**3), 2) if size else 0
            self.types_box.append(
                f"{ext or 'NO EXT'} | {count} files | {size_gb} GB"
            )

    # -------------------------

    def load_folders(self):

        self.folder_box.clear()
        self.folder_box.append("\nLARGEST FOLDERS\n")

        folders = self.file_manager.get_storage_by_folder(10)

        for folder, size in folders:
            size_gb = round(size / (1024**3), 2)
            self.folder_box.append(f"{size_gb} GB — {folder}")

    # -------------------------

    def load_largest_files(self):

        self.largest_files_box.clear()
        self.largest_files_box.append("\nLARGEST FILES\n")

        files = self.file_manager.get_largest_files(10)

        for path, size in files:
            size_gb = round(size / (1024**3), 2)
            self.largest_files_box.append(f"{size_gb} GB — {path}")

    # -------------------------

    def load_steam(self):

        self.steam_box.clear()
        self.steam_box.append("\nSTEAM / GAME STORAGE\n")

        games = self.file_manager.get_steam_games_usage()

        if not games:
            self.steam_box.append("No Steam libraries detected.")
            return

        for game, size in games:
            size_gb = round(size / (1024**3), 2)
            self.steam_box.append(f"{size_gb} GB — {game}")

    # -------------------------

    def load_cleanup(self):

        self.cleanup_box.clear()
        self.cleanup_box.append("\nSMART CLEANUP SUGGESTIONS\n")

        suggestions = self.file_manager.get_cleanup_suggestions()

        if not suggestions:
            self.cleanup_box.append("No cleanup suggestions available.")
            return

        for s in suggestions[:20]:
            self.cleanup_box.append(s)