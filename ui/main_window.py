from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QMainWindow,
    QScrollArea,
    QSizePolicy,
    QWidget,
    QHBoxLayout,
    QStackedWidget,
    QStatusBar
)

from core.analytics import AnalyticsEngine
from core.brightspace import BrightspaceClient
from core.brightspace_oauth import BrightspaceOAuth
from core.brightspace_session import BrightspaceSession
from core.brightspace_sync import BrightspaceSync
from core.config import ConfigManager
from core.device_manager import DeviceManager
from core.file_manager import FileManager
from core.google_calendar import GoogleCalendarSync
from core.google_oauth import GoogleOAuth
from core.secure_store import SecureStore

from ui.sidebar import Sidebar
from ui.dashboard_view import DashboardView
from ui.tasks_view import TasksView
from ui.file_view import FilesView
from ui.devices_view import DevicesView
from ui.analytics_view import AnalyticsView
from ui.settings_view import SettingsView
from ui.calendar_view import CalendarView
from ui.thread_utils import stop_thread


BASE_DIR = Path(__file__).resolve().parent.parent
SECRETS_PATH = BASE_DIR / "brightspace_secrets.xml"
WEBCACHE_DIR = BASE_DIR / ".webcache" / "brightspace"

# Background Brightspace refresh interval.
SYNC_INTERVAL_MS = 3 * 60 * 60 * 1000
SYNC_STARTUP_DELAY_MS = 8 * 1000


def _scrollable(widget: QWidget) -> QScrollArea:
    """Wrap a page so it scrolls instead of clipping when the window is small."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setWidget(widget)
    return area


class _SyncWorker(QObject):
    done = pyqtSignal(bool, str)

    def __init__(self, sync: BrightspaceSync) -> None:
        super().__init__()
        self.sync = sync

    def run(self) -> None:
        try:
            ok, message, _ = self.sync.run()
        except Exception as exc:  # noqa: BLE001
            ok, message = False, str(exc)
        self.done.emit(ok, message)


class MainWindow(QMainWindow):
    def __init__(self, task_manager):
        super().__init__()

        self.task_manager = task_manager
        self.config_manager = ConfigManager()
        self.file_manager = FileManager(self.config_manager)
        self.device_manager = DeviceManager(self.task_manager.db)

        self.secure_store = SecureStore(SECRETS_PATH)
        self.brightspace_oauth = BrightspaceOAuth(self.config_manager, self.secure_store)
        self.brightspace_session = BrightspaceSession(self.config_manager, self.secure_store)
        self.brightspace_client = BrightspaceClient(
            self.config_manager,
            self.secure_store,
            self.brightspace_oauth,
            self.brightspace_session,
        )
        self.google_oauth = GoogleOAuth(self.config_manager, self.secure_store)
        self.google_calendar = GoogleCalendarSync(
            self.config_manager, self.secure_store, self.google_oauth
        )
        self.brightspace_sync = BrightspaceSync(
            self.brightspace_client, self.task_manager, self.google_calendar
        )

        self.analytics = AnalyticsEngine(self.task_manager)

        self._auto_sync_thread = None
        self._auto_sync_worker = None

        self.setWindowTitle("Aegis")
        # Adaptive: an initial size, but the window can be shrunk freely and
        # every page scrolls rather than clipping.
        self.setMinimumSize(720, 480)
        self.resize(1120, 760)

        self._init_ui()
        self._init_scheduled_sync()

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
        self.devices_view = DevicesView(
            self.task_manager,
            self.device_manager,
            self.file_manager.backup_manager,
        )
        self.devices_view.backup_requested.connect(self.run_device_backup)
        self.analytics_view = AnalyticsView(self.analytics)
        self.settings_view = SettingsView(
            self.config_manager,
            self.brightspace_client,
            self.brightspace_sync,
            str(WEBCACHE_DIR),
            self.google_oauth,
        )
        self.calendar_view = CalendarView(self.task_manager, self.brightspace_sync)
        # "Consolidate Drives" now lives as a tab inside File Organizer.
        self.consolidate_view = self.file_view.consolidate_view

        self.settings_view.settings_saved.connect(self.dashboard_view.refresh)
        self.settings_view.settings_saved.connect(self.calendar_view.reload)
        self.settings_view.brightspace_synced.connect(self._on_brightspace_synced)
        self.calendar_view.tasks_changed.connect(self._on_brightspace_synced)

        # file_view and analytics_view already manage their own scroll area.
        self._pages = [
            self.dashboard_view,
            self.tasks_view,
            self.file_view,
            self.devices_view,
            self.analytics_view,
            self.settings_view,
            self.calendar_view,
        ]
        self_managed = {self.file_view, self.analytics_view}
        for page in self._pages:
            self.stack.addWidget(page if page in self_managed else _scrollable(page))

        self.sidebar.page_changed.connect(self.switch_page)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Ready")

    # ── navigation ───────────────────────────────────────────────────────
    PAGE_NAMES = [
        "Dashboard",
        "Tasks",
        "File Organizer",
        "Devices",
        "Analytics",
        "Settings",
        "Calendar",
    ]

    def switch_page(self, index: int):
        self.stack.setCurrentIndex(index)
        if index == 0:
            self.dashboard_view.refresh()
        elif index == 1:
            self.tasks_view.load_tasks()
        elif index == 2:
            self.file_view.ensure_drive_panels_loaded()
        elif index == 3:
            self.devices_view.refresh_device_list()
        elif index == 6:
            self.calendar_view.reload()

        if 0 <= index < len(self.PAGE_NAMES):
            self.status.showMessage(f"{self.PAGE_NAMES[index]} loaded")

    # ── device backup bridge ─────────────────────────────────────────────
    def run_device_backup(self, device_name, device_type):
        """Find the drive letter for the registered device and start its backup in File Organizer."""
        device = self.device_manager.get_device_by_name(device_name)
        if not device:
            return

        mappings = self.device_manager.db.fetchall(
            "SELECT drive_letter FROM device_mappings WHERE device_id = ?",
            (device.get("device_id"),)
        )
        if mappings and mappings[0][0]:
            drive_letter = mappings[0][0]
            drive_root = f"{drive_letter}:\\"

            self.switch_page(2)
            self.file_view.tools_tabs.setCurrentIndex(1)

            for root, check in self.file_view.drive_checks.items():
                check.setChecked(root == drive_root)

            self.file_view.run_backup()

    # ── scheduled Brightspace sync ───────────────────────────────────────
    def _init_scheduled_sync(self):
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(SYNC_INTERVAL_MS)
        self._sync_timer.timeout.connect(self._run_auto_sync)
        self._sync_timer.start()
        QTimer.singleShot(SYNC_STARTUP_DELAY_MS, self._run_auto_sync)

    def _run_auto_sync(self):
        if self._auto_sync_thread is not None:
            return
        if not self.brightspace_client.is_enabled() or not self.brightspace_client.is_configured():
            return

        self.status.showMessage("Syncing Brightspace…")
        self._auto_sync_thread = QThread()
        self._auto_sync_worker = _SyncWorker(self.brightspace_sync)
        self._auto_sync_worker.moveToThread(self._auto_sync_thread)
        self._auto_sync_thread.started.connect(self._auto_sync_worker.run)
        self._auto_sync_worker.done.connect(self._on_auto_sync_done)
        self._auto_sync_worker.done.connect(self._auto_sync_thread.quit)
        self._auto_sync_worker.done.connect(self._auto_sync_worker.deleteLater)
        self._auto_sync_thread.finished.connect(self._cleanup_auto_sync)
        self._auto_sync_thread.finished.connect(self._auto_sync_thread.deleteLater)
        self._auto_sync_thread.start()

    def _cleanup_auto_sync(self):
        self._auto_sync_thread = None
        self._auto_sync_worker = None

    def _on_auto_sync_done(self, ok: bool, message: str):
        self.status.showMessage(
            f"Brightspace: {message}" if ok else f"Brightspace sync failed: {message}",
            8000,
        )
        self._on_brightspace_synced()

    def _on_brightspace_synced(self):
        self.calendar_view.reload()
        self.tasks_view.load_tasks()
        self.dashboard_view.refresh()

    # ── clean shutdown ───────────────────────────────────────────────────
    def closeEvent(self, event):
        """Stop every background worker before the widgets are destroyed —
        otherwise a still-running QThread aborts the process on exit."""
        try:
            self._sync_timer.stop()
        except Exception:  # noqa: BLE001
            pass
        stop_thread(getattr(self, "_auto_sync_thread", None),
                    getattr(self, "_auto_sync_worker", None))
        for page in getattr(self, "_pages", []):
            fn = getattr(page, "shutdown", None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
        super().closeEvent(event)
