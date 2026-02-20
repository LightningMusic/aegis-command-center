from PyQt6.QtWidgets import QWidget, QVBoxLayout, QPushButton
from PyQt6.QtCore import pyqtSignal


class Sidebar(QWidget):
    page_changed = pyqtSignal(int)

    def __init__(self):
        super().__init__()

        self.setFixedWidth(220)

        self.main_layout = QVBoxLayout()
        self.main_layout.setContentsMargins(10, 20, 10, 20)
        self.main_layout.setSpacing(6)
        self.setLayout(self.main_layout)

        self.buttons = []
        self.current_index = 0

        pages = [
            "Dashboard",
            "Tasks",
            "File Organizer",
            "Analytics",
            "Settings"
        ]

        for index, name in enumerate(pages):
            btn = QPushButton(name)
            btn.setMinimumHeight(40)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _, i=index: self.select_page(i))

            self.main_layout.addWidget(btn)
            self.buttons.append(btn)

        self.main_layout.addStretch()

        # Default selection
        self.select_page(0)

    def select_page(self, index: int):
        self.current_index = index

        for i, btn in enumerate(self.buttons):
            btn.setChecked(i == index)

        self.page_changed.emit(index)
