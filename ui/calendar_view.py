"""
ui/calendar_view.py
===================
Month-grid view of everything with a due date. Days that carry items are
highlighted; selecting a day lists that day's items. "Sync Brightspace" pulls
the calendar feed on a worker thread and rebuilds the grid.
"""

from __future__ import annotations

from datetime import date

from PyQt6.QtCore import QDate, QObject, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QDesktopServices, QTextCharFormat
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCalendarWidget,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)


class _SyncWorker(QObject):
    done = pyqtSignal(bool, str)

    def __init__(self, sync) -> None:
        super().__init__()
        self.sync = sync

    def run(self) -> None:
        try:
            ok, message, _details = self.sync.run()
        except Exception as exc:  # noqa: BLE001
            ok, message = False, str(exc)
        self.done.emit(ok, message)


class CalendarView(QWidget):
    tasks_changed = pyqtSignal()

    def __init__(self, task_manager, brightspace_sync) -> None:
        super().__init__()
        self.task_manager = task_manager
        self.brightspace_sync = brightspace_sync
        self._by_date: dict[str, list] = {}
        self._sync_thread: QThread | None = None
        self._sync_worker: _SyncWorker | None = None

        self._build_ui()
        self.reload()

    # ── ui ───────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)
        self.setLayout(root)

        header = QHBoxLayout()
        title = QLabel("Calendar")
        title.setStyleSheet("font-size: 24px; font-weight: 600;")
        header.addWidget(title)
        header.addStretch()

        self.sync_btn = QPushButton("Sync Brightspace")
        self.sync_btn.clicked.connect(self.sync_now)
        header.addWidget(self.sync_btn)
        root.addLayout(header)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #888; font-size: 12px;")
        root.addWidget(self.status)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)

        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(True)
        self.calendar.setVerticalHeaderFormat(
            QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
        )
        self.calendar.selectionChanged.connect(self._show_day)
        self.calendar.currentPageChanged.connect(lambda *_: self._paint_calendar())
        splitter.addWidget(self.calendar)

        right = QWidget()
        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(12, 0, 0, 0)
        right.setLayout(right_layout)

        self.day_label = QLabel("")
        self.day_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        right_layout.addWidget(self.day_label)

        self.agenda = QListWidget()
        self.agenda.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.agenda.itemDoubleClicked.connect(self._open_item)
        right_layout.addWidget(self.agenda, 1)

        hint = QLabel("Double-click an item to open it in Brightspace.")
        hint.setStyleSheet("color: #888; font-size: 11px;")
        right_layout.addWidget(hint)

        splitter.addWidget(right)
        splitter.setSizes([540, 360])

    # ── data ─────────────────────────────────────────────────────────────
    def reload(self) -> None:
        tasks = [t for t in self.task_manager.get_all_tasks(True) if t.get("due_date")]
        self._by_date = {}
        for task in tasks:
            self._by_date.setdefault(task["due_date"], []).append(task)

        snapshot = self.brightspace_sync.client.get_dashboard_snapshot()
        if not snapshot["enabled"]:
            self.status.setText("Brightspace sync is off — enable it in Settings.")
        elif not snapshot["configured"]:
            self.status.setText("Add your Brightspace calendar feed URL in Settings.")
        else:
            self.status.setText(
                f"Brightspace: {snapshot['last_sync_status']}"
                + (f"  ·  last checked {snapshot['last_sync_at']}" if snapshot["last_sync_at"] else "")
            )

        self._paint_calendar()
        self._show_day()

    def _paint_calendar(self) -> None:
        blank = QTextCharFormat()
        self.calendar.setDateTextFormat(QDate(), blank)

        today = date.today()
        has_fmt = QTextCharFormat()
        has_fmt.setFontWeight(75)
        has_fmt.setBackground(QBrush(QColor(58, 90, 140, 60)))

        overdue_fmt = QTextCharFormat()
        overdue_fmt.setFontWeight(75)
        overdue_fmt.setForeground(QBrush(QColor(231, 76, 60)))
        overdue_fmt.setBackground(QBrush(QColor(231, 76, 60, 45)))

        for key, items in self._by_date.items():
            qd = QDate.fromString(key, "yyyy-MM-dd")
            if not qd.isValid():
                continue
            day = qd.toPyDate()
            active = [i for i in items if not i["completed"]]
            fmt = overdue_fmt if (day < today and active) else has_fmt
            self.calendar.setDateTextFormat(qd, fmt)

    def _show_day(self) -> None:
        qd = self.calendar.selectedDate()
        key = qd.toString("yyyy-MM-dd")
        self.day_label.setText(qd.toString("dddd, MMMM d, yyyy"))
        self.agenda.clear()

        items = sorted(
            self._by_date.get(key, []),
            key=lambda t: (bool(t["completed"]), (t.get("title") or "").lower()),
        )
        if not items:
            placeholder = QListWidgetItem("Nothing due.")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.agenda.addItem(placeholder)
            return

        for task in items:
            mark = "✓ " if task["completed"] else "• "
            label = f"{mark}{task['title']}"
            if task.get("category"):
                label += f"   —   {task['category']}"
            entry = QListWidgetItem(label)
            entry.setData(Qt.ItemDataRole.UserRole, task)
            if task["completed"]:
                entry.setForeground(QBrush(QColor(140, 140, 140)))
            self.agenda.addItem(entry)

    def _open_item(self, entry: QListWidgetItem) -> None:
        task = entry.data(Qt.ItemDataRole.UserRole)
        if not isinstance(task, dict):
            return
        description = task.get("description") or ""
        url = ""
        for line in description.splitlines():
            line = line.strip()
            if line.startswith("http://") or line.startswith("https://"):
                url = line
                break
        if url:
            QDesktopServices.openUrl(QUrl(url))
        else:
            QMessageBox.information(self, task.get("title", "Item"), description or "No details.")

    # ── sync ─────────────────────────────────────────────────────────────
    def sync_now(self) -> None:
        if self._sync_thread is not None:
            return
        client = self.brightspace_sync.client
        if not client.is_enabled() or not client.has_feed():
            QMessageBox.information(
                self,
                "Brightspace",
                "Turn on Brightspace and paste your calendar feed URL in Settings first.",
            )
            return

        self.sync_btn.setEnabled(False)
        self.status.setText("Syncing with Brightspace…")

        self._sync_thread = QThread()
        self._sync_worker = _SyncWorker(self.brightspace_sync)
        self._sync_worker.moveToThread(self._sync_thread)
        self._sync_thread.started.connect(self._sync_worker.run)
        self._sync_worker.done.connect(self._on_sync_done)
        self._sync_worker.done.connect(self._sync_thread.quit)
        self._sync_worker.done.connect(self._sync_worker.deleteLater)
        self._sync_thread.finished.connect(self._cleanup_sync_thread)
        self._sync_thread.finished.connect(self._sync_thread.deleteLater)
        self._sync_thread.start()

    def _cleanup_sync_thread(self) -> None:
        self._sync_thread = None
        self._sync_worker = None
        self.sync_btn.setEnabled(True)

    def _on_sync_done(self, ok: bool, message: str) -> None:
        self.reload()
        self.tasks_changed.emit()
        if not ok:
            QMessageBox.warning(self, "Brightspace Sync", message)
