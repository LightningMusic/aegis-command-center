from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel


class AnalyticsView(QWidget):
    def __init__(self, analytics_engine):
        super().__init__()
        self.analytics_engine = analytics_engine

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        self.setLayout(layout)

        label = QLabel("Analytics")
        label.setStyleSheet("font-size: 24px;")
        layout.addWidget(label)
