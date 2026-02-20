from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QStackedWidget,
    QStatusBar
)


from ui.sidebar import Sidebar
from ui.dashboard_view import DashboardView
from ui.tasks_view import TasksView
from ui.files_view import FilesView
from ui.analytics_view import AnalyticsView
from ui.settings_view import SettingsView


class MainWindow(QMainWindow):
    def __init__(self, task_manager):
        super().__init__()
        self.task_manager = task_manager
        self.setWindowTitle("Aegis")
        self.resize(1200, 800)

        self._init_ui()

    def _init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        central_widget.setLayout(main_layout)

        # Sidebar
        self.sidebar = Sidebar()
        main_layout.addWidget(self.sidebar)

        # Page Stack
        self.stack = QStackedWidget()
        main_layout.addWidget(self.stack)

        # Pages
        self.dashboard_view = DashboardView()
        self.tasks_view = TasksView(self.task_manager)
        self.files_view = FilesView()
        self.analytics_view = AnalyticsView()
        self.settings_view = SettingsView()

        self.stack.addWidget(self.dashboard_view)
        self.stack.addWidget(self.tasks_view)
        self.stack.addWidget(self.files_view)
        self.stack.addWidget(self.analytics_view)
        self.stack.addWidget(self.settings_view)

        # Connect sidebar
        self.sidebar.page_changed.connect(self.switch_page)

        # Status Bar
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Ready")

    def switch_page(self, index: int):
        self.stack.setCurrentIndex(index)

        page_names = [
            "Dashboard",
            "Tasks",
            "File Organizer",
            "Analytics",
            "Settings"
        ]

        self.status.showMessage(f"{page_names[index]} loaded")
