from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel


class SettingsView(QWidget):
    def __init__(self):
        super().__init__()

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        self.setLayout(layout)

        label = QLabel("Settings")
        label.setStyleSheet("font-size: 24px;")
        layout.addWidget(label)
