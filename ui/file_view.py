from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit
)


class FilesView(QWidget):
    def __init__(self, file_manager):
        super().__init__()

        self.file_manager = file_manager

        self.main_layout = QVBoxLayout()
        self.main_layout.setContentsMargins(30, 30, 30, 30)
        self.main_layout.setSpacing(20)
        self.setLayout(self.main_layout)

        title = QLabel("File Manager")
        title.setStyleSheet("font-size: 26px; font-weight: 600;")
        self.main_layout.addWidget(title)

        self.scan_button = QPushButton("Scan Drive")
        self.scan_button.clicked.connect(self.run_scan)
        self.main_layout.addWidget(self.scan_button)

        self.results_box = QTextEdit()
        self.results_box.setReadOnly(True)
        self.main_layout.addWidget(self.results_box)

        self.main_layout.addStretch()

    def run_scan(self):
        self.results_box.clear()
        self.results_box.append("Scanning drive...\n")

        files = self.file_manager.full_scan("C:\\Users")

        self.results_box.append("\nScan Complete.")
        self.results_box.append(f"Total files indexed: {len(files)}\n")

        large_files = self.file_manager.get_large_files(500)
        self.results_box.append(f"Large files (>500MB): {len(large_files)}")

        duplicates = self.file_manager.get_duplicates()
        self.results_box.append(f"Potential duplicates: {len(duplicates)}")