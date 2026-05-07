from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QStackedWidget,
    QStatusBar
)

from core.analytics import AnalyticsEngine
from core.brightspace import BrightspaceClient
from core.config import ConfigManager
from core.file_manager import FileManager

from ui.sidebar import Sidebar
from ui.dashboard_view import DashboardView
from ui.tasks_view import TasksView
from ui.file_view import FilesView
from ui.analytics_view import AnalyticsView
from ui.settings_view import SettingsView


class MainWindow(QMainWindow):
    def __init__(self, task_manager):
        super().__init__()

        self.task_manager = task_manager
        self.config_manager = ConfigManager()
        self.file_manager = FileManager(self.config_manager)
        self.brightspace_client = BrightspaceClient(self.config_manager)

        self.analytics = AnalyticsEngine(self.task_manager)

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

        self.sidebar = Sidebar()
        main_layout.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        main_layout.addWidget(self.stack)

        self.dashboard_view = DashboardView(
            self.task_manager,
            self.analytics,
            self.brightspace_client,
        )
        self.tasks_view = TasksView(self.task_manager)
        self.file_view = FilesView(self.file_manager, self.config_manager)
        self.analytics_view = AnalyticsView(self.analytics)
        self.settings_view = SettingsView(
            self.config_manager,
            self.brightspace_client,
        )
        self.settings_view.settings_saved.connect(self.dashboard_view.refresh)

        self.stack.addWidget(self.dashboard_view)
        self.stack.addWidget(self.tasks_view)
        self.stack.addWidget(self.file_view)
        self.stack.addWidget(self.analytics_view)
        self.stack.addWidget(self.settings_view)

        self.sidebar.page_changed.connect(self.switch_page)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Ready")

    def switch_page(self, index: int):
        self.stack.setCurrentIndex(index)
        if index == 0:
            self.dashboard_view.refresh()

        page_names = [
            "Dashboard",
            "Tasks",
            "File Organizer",
            "Analytics",
            "Settings"
        ]

        if 0 <= index < len(page_names):
            self.status.showMessage(f"{page_names[index]} loaded")
