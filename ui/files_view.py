from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel


class FilesView(QWidget):
    def __init__(self):
        super().__init__()

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        self.setLayout(layout)

        label = QLabel("Files")
        label.setStyleSheet("font-size: 24px;")
        layout.addWidget(label)
