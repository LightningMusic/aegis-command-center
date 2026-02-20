from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QFrame
)
from PyQt6.QtCore import Qt


class DashboardView(QWidget):
    def __init__(self):
        super().__init__()

        self.main_layout = QVBoxLayout()
        self.main_layout.setContentsMargins(30, 30, 30, 30)
        self.main_layout.setSpacing(20)
        self.setLayout(self.main_layout)

        title = QLabel("Dashboard")
        title.setStyleSheet("font-size: 26px; font-weight: 600;")
        self.main_layout.addWidget(title)

        # Placeholder card area
        self.card_area = QFrame()
        self.card_area.setMinimumHeight(200)
        self.card_area.setStyleSheet("""
            QFrame {
                background-color: #252525;
                border-radius: 8px;
            }
        """)

        self.main_layout.addWidget(self.card_area)
        self.main_layout.addStretch()
