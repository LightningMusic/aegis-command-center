"""
ui/phone_backup_view.py
=======================
QWidget that lives as a tab inside FilesView's tools_tabs panel.

Layout
──────
┌─────────────────────────────────┬──────────────────────────────┐
│  Connected Devices              │  Previously Backed-Up Phones │
│  [Detect Phones]                │  ─────────────────────────── │
│  ┌──────────────────────────┐   │  Galaxy S24   (3 snapshots)  │
│  │ Galaxy S24  [MTP]        │   │  Pixel 8 Pro  (1 snapshot)   │
│  │ Pixel 8 Pro [MTP]        │   │                              │
│  └──────────────────────────┘   │  ──── Device detail ──────── │
│  (multi-select: ctrl/shift-click│  Name:      Galaxy S24       │
│   to back up several phones     │  Folder:    …/Phones/…       │
│   connected at once)            │  Snapshots: 3                │
│                                 │  Last:      2026-05-30…      │
│  ─── Backup ───────────────     │  Size:      4.2 GB           │
│  [Start Backup]                 │                              │
│  ┌ Galaxy S24 ──────────────┐   │                              │
│  │ ████████░░░░  42 %  [X] │   │                              │
│  │ log…                     │   │                              │
│  └──────────────────────────┘   │                              │
│  ┌ Pixel 8 Pro ─────────────┐   │                              │
│  │ ███░░░░░░░░░  18 %  [X] │   │                              │
│  │ log…                     │   │                              │
│  └──────────────────────────┘   │                              │
└─────────────────────────────────┴──────────────────────────────┘

Each connected device gets its own independent progress panel
(DeviceBackupPanel) with its own worker thread, so two (or more) phones
connected at once can back up concurrently without one blocking the other —
each is a wholly separate MTP session against a different USB device.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional
import time
from PyQt6.QtCore import QObject, QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


from core.phone_backup_manager import PhoneBackupManager, PhoneDevice
from core.device_manager import DeviceManager
from core.util import human_bytes
from ui.thread_utils import stop_thread

# ─────────────────────────────────────────────────────────────────────────────
# Background worker
# ─────────────────────────────────────────────────────────────────────────────

class PhoneBackupWorker(QObject):
    """Runs the backup in a QThread and emits progress / completion signals."""

    progress  = pyqtSignal(int, str)
    finished  = pyqtSignal(dict)
    failed    = pyqtSignal(str)

    def __init__(self, manager: PhoneBackupManager, device: PhoneDevice, force_verify: bool = False) -> None:
        super().__init__()
        self.manager = manager
        self.device  = device
        self.force_verify = force_verify
        self._stop   = False

    def run(self) -> None:
        try:
            result = self.manager.backup_phone_until_complete(
                self.device,
                progress_callback=self._emit_progress,
                should_stop=lambda: self._stop,
                force_verify=self.force_verify,
            )
            self.finished.emit(result)
        except Exception as exc:
            # A bare str(exc) is useless (or actively misleading) for some
            # exception types — e.g. str(KeyError('x')) is just "'x'", which
            # reads like a real status rather than a bug. Always include the
            # exception type so a message like this is diagnosable instead
            # of a cryptic fragment.
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    def _emit_progress(self, percent: int, message: str) -> None:
        self.progress.emit(percent, message)

    def stop(self) -> None:
        self._stop = True


class PhoneDetectWorker(QObject):
    """Runs phone detection (PowerShell/MTP enumeration) off the GUI thread."""

    finished = pyqtSignal(list)

    def __init__(self, manager: PhoneBackupManager) -> None:
        super().__init__()
        self.manager = manager

    def run(self) -> None:
        try:
            devices = self.manager.detect_phones()
        except Exception:
            devices = []
        self.finished.emit(devices)


# ─────────────────────────────────────────────────────────────────────────────
# Per-device backup panel — owns its own thread/worker, progress bar and log,
# so N of these can run at once without sharing any UI state.
# ─────────────────────────────────────────────────────────────────────────────

class DeviceBackupPanel(QFrame):
    """
    Self-contained backup run for one device: progress bar, status, activity
    line, its own log, and a Stop button. Owns its own QThread/worker pair
    following the same lifecycle rule used everywhere else in this app —
    thread refs are only ever cleared from a thread.finished-connected slot,
    never from worker.finished, so a still-running QThread never gets
    garbage-collected out from under itself.
    """

    request_remove = pyqtSignal(object)      # self — parent should remove/discard this panel
    run_finished    = pyqtSignal(object, dict)  # self, result — for history/DB bookkeeping
    run_failed      = pyqtSignal(object, str)   # self, error message
    activity_state_changed = pyqtSignal()     # any start/stop — parent re-evaluates button states

    def __init__(self, manager: PhoneBackupManager, device: PhoneDevice) -> None:
        super().__init__()
        self.manager = manager
        self.device = device
        self.thread: Optional[QThread] = None
        self.worker: Optional[PhoneBackupWorker] = None
        self._started_at: Optional[float] = None
        self._last_activity_at: Optional[float] = None
        self.finished_ok = False

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("DeviceBackupPanel { border: 1px solid #3a3a3a; border-radius: 4px; }")

        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(1000)
        self._activity_timer.timeout.connect(self._tick_activity)

        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        self.setLayout(layout)

        header = QHBoxLayout()
        title = QLabel(self.device.display_name)
        title.setStyleSheet("font-size: 13px; font-weight: 600;")
        header.addWidget(title)
        header.addStretch()

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setFixedWidth(60)
        self.stop_btn.clicked.connect(self._on_stop_clicked)
        header.addWidget(self.stop_btn)

        self.dismiss_btn = QPushButton("✕")
        self.dismiss_btn.setFixedWidth(28)
        self.dismiss_btn.setToolTip("Remove this panel")
        self.dismiss_btn.setVisible(False)
        self.dismiss_btn.clicked.connect(lambda: self.request_remove.emit(self))
        header.addWidget(self.dismiss_btn)
        layout.addLayout(header)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Starting…")
        layout.addWidget(self.progress_bar)

        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("font-size: 12px; color: #AAA;")
        layout.addWidget(self.status_lbl)

        self.activity_lbl = QLabel("")
        self.activity_lbl.setStyleSheet("font-size: 11px; color: #777;")
        layout.addWidget(self.activity_lbl)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(90)
        self.log_box.setStyleSheet("font-size: 11px; font-family: Consolas, monospace;")
        layout.addWidget(self.log_box)

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self, force_verify: bool) -> None:
        verify_note = " (verifying everything — this will be slower)" if force_verify else ""
        self.status_lbl.setText(f"Backing up…{verify_note}")
        self.log_box.clear()

        self._started_at = time.monotonic()
        self._last_activity_at = self._started_at
        self._activity_timer.start()

        thread = QThread()
        worker = PhoneBackupWorker(self.manager, self.device, force_verify=force_verify)
        self.thread = thread
        self.worker = worker
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)

        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._cleanup_thread)
        thread.finished.connect(thread.deleteLater)

        thread.start()
        self.activity_state_changed.emit()

    def is_running(self) -> bool:
        return self.thread is not None

    def _on_stop_clicked(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.stop_btn.setEnabled(False)
            self.progress_bar.setFormat("Stopping…")
            self.status_lbl.setText("Stopping — finishing the current file first…")

    def shutdown(self) -> None:
        self._activity_timer.stop()
        stop_thread(self.thread, self.worker, wait_ms=6000)

    def _cleanup_thread(self) -> None:
        self.thread = None
        self.worker = None
        self._activity_timer.stop()
        self.stop_btn.setVisible(False)
        self.dismiss_btn.setVisible(True)
        self.activity_state_changed.emit()

    def _tick_activity(self) -> None:
        if self._started_at is None:
            return
        now = time.monotonic()
        elapsed = int(now - self._started_at)
        since_activity = int(now - (self._last_activity_at or self._started_at))

        elapsed_txt = f"{elapsed // 60}m {elapsed % 60:02d}s"
        activity_txt = "active now" if since_activity < 10 else f"last update {since_activity}s ago"
        self.activity_lbl.setText(f"Elapsed: {elapsed_txt} · {activity_txt}")

    # ── worker signal handlers ──────────────────────────────────────────

    def _on_progress(self, percent: int, message: str) -> None:
        self._last_activity_at = time.monotonic()

        if percent == -2:
            self.status_lbl.setText(message)
            return

        if percent == -3:
            self.status_lbl.setText(message)
            self.log_box.append(f"\n— {message} —")
            self._scroll_log_to_end()
            return

        if percent >= 0:
            self.progress_bar.setValue(percent)
            self.progress_bar.setFormat(f"{percent}%")
        self.log_box.append(message)
        self._scroll_log_to_end()

    def _on_finished(self, result: dict) -> None:
        cancelled = result.get("cancelled", False)
        stalled = result.get("stalled", False)
        connection_failed = result.get("connection_failed", False)
        unreachable = stalled or connection_failed

        self.progress_bar.setValue(100)
        if unreachable:
            self.progress_bar.setFormat("Paused — phone unreachable")
        elif cancelled:
            self.progress_bar.setFormat("Stopped")
        else:
            self.progress_bar.setFormat("Complete ✓")
            self.finished_ok = True

        copy_r = result.get("copy_result") or {}
        manifest_summary = result.get("manifest_summary") or {}

        confirmed_files = manifest_summary.get("confirmed_files")
        confirmed_bytes = manifest_summary.get("confirmed_bytes")
        lines = ["Preserved folder structure in latest/"]

        copied_now = copy_r.get("copied")
        skipped_now = copy_r.get("skipped")
        skipped_manifest_now = copy_r.get("skipped_manifest") or 0
        skipped_elsewhere_now = copy_r.get("skipped_elsewhere") or 0
        if copied_now is not None:
            extra_bits = []
            if skipped_manifest_now:
                extra_bits.append(f"{skipped_manifest_now} already backed up, unchanged")
            if skipped_elsewhere_now:
                extra_bits.append(f"{skipped_elsewhere_now} found already backed up under a different folder")
            lines.append(
                f"This pass: {copied_now} copied, {skipped_now} skipped"
                + (f" ({', '.join(extra_bits)})" if extra_bits else "")
            )
            if copied_now == 0 and skipped_manifest_now == (skipped_now or 0) and skipped_manifest_now > 0:
                lines.append(
                    "  (nothing copied because everything matched what's "
                    "already saved — check “Verify everything” above "
                    "to re-check those files against the phone instead of "
                    "trusting the record.)"
                )

        if confirmed_files is not None:
            lines.append(
                f"Confirmed on disk (all-time total): {confirmed_files} file(s)"
                + (f", {human_bytes(confirmed_bytes)}" if confirmed_bytes else "")
            )

        errors = result.get("errors") or []
        if errors:
            lines += ["", "── Errors (first 10) ───────────────────"]
            for e in errors[:10]:
                lines.append(f"  {e}")

        lines += [
            "",
            f"Folder : {result.get('device_folder', '?')}",
            f"Latest : {result.get('latest_dir',  '?')}",
            "─" * 40,
        ]

        if unreachable:
            lines.append(
                "The phone stopped responding or lost its USB/MTP connection, and "
                "automatic retries were exhausted. Reconnect it and click Start "
                "Backup to keep going — already-copied files won't be re-transferred."
            )

        self.log_box.append("\n".join(lines))
        self._scroll_log_to_end()

        if unreachable:
            self.status_lbl.setText("Paused — phone unreachable after several retries. Reconnect and run again.")
        elif cancelled:
            self.status_lbl.setText("Backup stopped — progress saved.")
        else:
            self.status_lbl.setText("Backup complete.")

        self.run_finished.emit(self, result)

    def _on_failed(self, error_message: str) -> None:
        self.progress_bar.setFormat("Failed")
        self.status_lbl.setText(f"Error: {error_message}")
        self.log_box.append(f"\nERROR: {error_message}")
        self._scroll_log_to_end()
        self.run_failed.emit(self, error_message)

    def _scroll_log_to_end(self) -> None:
        sb = self.log_box.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())


# ─────────────────────────────────────────────────────────────────────────────
# Main view
# ─────────────────────────────────────────────────────────────────────────────

class PhoneBackupView(QWidget):
    """
    Tab widget for phone detection and backup.

    Parameters
    ----------
    phone_backup_manager : PhoneBackupManager
        Shared manager instance (created by FilesView and passed in).
    """

    def __init__(self, phone_backup_manager: PhoneBackupManager) -> None:
        super().__init__()
        self.manager: PhoneBackupManager = phone_backup_manager
        self.device_manager: Optional[DeviceManager] = None
        self.detected: list[PhoneDevice] = []
        self._panels: list[DeviceBackupPanel] = []
        self._detect_thread: Optional[QThread] = None
        self._detect_worker: Optional[PhoneDetectWorker] = None
        self._detect_callbacks: list[Callable[[], None]] = []

        self._build_ui()
        # History is populated by the owner (DevicesView) from a worker thread
        # so manifest parsing never blocks construction / the page switch.
        self.populate_history([])

    # ─────────────────────────────────────────────────────────────────────
    # UI construction
    # ─────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout()
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)
        self.setLayout(root)

        # ── Header ────────────────────────────────────────────────────────
        hdr = QLabel("Phone Backup")
        hdr.setStyleSheet("font-size: 20px; font-weight: 600;")
        root.addWidget(hdr)

        hint = QLabel(
            "Connect one or more phones via USB, switch each to File Transfer "
            "(MTP) mode, then click Detect. Select several phones (ctrl/shift-"
            "click) to back them up at the same time."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size: 13px; color: #888;")
        root.addWidget(hint)

        # ── Splitter: left = detect+backup  │  right = history ────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        splitter.setChildrenCollapsible(True)
        root.addWidget(splitter)

        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([560, 320])

    # ── Left panel ────────────────────────────────────────────────────────

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(0)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(10)
        panel.setLayout(layout)

        detect_group = QGroupBox("Connected Devices")
        detect_layout = QVBoxLayout()
        detect_group.setLayout(detect_layout)

        btn_row = QHBoxLayout()
        self.detect_btn = QPushButton("Detect Phones")
        self.detect_btn.setMinimumHeight(34)
        self.detect_btn.clicked.connect(self.detect_phones)
        btn_row.addWidget(self.detect_btn)
        btn_row.addStretch()
        detect_layout.addLayout(btn_row)

        self.phone_list = QListWidget()
        self.phone_list.setMaximumHeight(150)
        self.phone_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.phone_list.itemSelectionChanged.connect(self._on_phone_selection_changed)
        detect_layout.addWidget(self.phone_list)

        self.no_phone_label = QLabel(
            "No phones detected yet.  Make sure File Transfer mode is enabled on your phone."
        )
        self.no_phone_label.setWordWrap(True)
        self.no_phone_label.setStyleSheet("color: #888; font-size: 12px;")
        self.no_phone_label.setVisible(False)
        detect_layout.addWidget(self.no_phone_label)

        layout.addWidget(detect_group)

        backup_group = QGroupBox("Backup")
        backup_layout = QVBoxLayout()
        backup_group.setLayout(backup_layout)

        backup_btns = QHBoxLayout()
        self.backup_btn = QPushButton("Start Backup")
        self.backup_btn.setMinimumHeight(34)
        self.backup_btn.setEnabled(False)
        self.backup_btn.clicked.connect(self._start_selected_backups)
        backup_btns.addWidget(self.backup_btn)

        self.cleanup_btn = QPushButton("Clean Up Duplicates")
        self.cleanup_btn.setMinimumHeight(34)
        self.cleanup_btn.clicked.connect(self._cleanup_duplicates)
        backup_btns.addWidget(self.cleanup_btn)

        backup_btns.addStretch()
        backup_layout.addLayout(backup_btns)

        self.verify_everything_chk = QCheckBox("Verify everything (ignore fast-skip — slower, but nothing is taken on trust)")
        self.verify_everything_chk.setToolTip(
            "Normally, if a file can't be sized or dated by the phone at all, it's "
            "skipped on the assumption it's unchanged since the last backup. Check "
            "this to re-check every such file against the phone instead — much "
            "slower on a big phone, but nothing gets skipped just because it "
            "couldn't be verified. Applies to backups started after you check it."
        )
        backup_layout.addWidget(self.verify_everything_chk)

        self.global_status_lbl = QLabel("Select one or more phones above, then click Start Backup.")
        self.global_status_lbl.setWordWrap(True)
        self.global_status_lbl.setStyleSheet("font-size: 13px; color: #AAA;")
        backup_layout.addWidget(self.global_status_lbl)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #333;")
        backup_layout.addWidget(line)

        # Scrollable stack of per-device panels — each active (or just-
        # finished) backup gets its own independent progress bar and log.
        self.panels_scroll = QScrollArea()
        self.panels_scroll.setWidgetResizable(True)
        self.panels_scroll.setMinimumHeight(200)
        self.panels_scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.panels_container = QWidget()
        self.panels_layout = QVBoxLayout()
        self.panels_layout.setContentsMargins(0, 0, 0, 0)
        self.panels_layout.setSpacing(8)
        self.panels_layout.addStretch()
        self.panels_container.setLayout(self.panels_layout)
        self.panels_scroll.setWidget(self.panels_container)

        self.no_active_lbl = QLabel("No active or recent backups.")
        self.no_active_lbl.setStyleSheet("color: #777; font-size: 12px;")
        self.panels_layout.insertWidget(0, self.no_active_lbl)

        backup_layout.addWidget(self.panels_scroll, 1)

        layout.addWidget(backup_group, 1)
        return panel

    # ── Right panel ───────────────────────────────────────────────────────

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(0)
        layout = QVBoxLayout()
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(10)
        panel.setLayout(layout)

        hist_group = QGroupBox("Previously Backed-Up Phones")
        hist_layout = QVBoxLayout()
        hist_group.setLayout(hist_layout)

        refresh_row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh_history)
        refresh_row.addWidget(self.refresh_btn)
        refresh_row.addStretch()
        hist_layout.addLayout(refresh_row)

        self.history_list = QListWidget()
        self.history_list.currentItemChanged.connect(self._on_history_selected)
        hist_layout.addWidget(self.history_list)

        detail_lbl = QLabel("Device Details")
        detail_lbl.setStyleSheet("font-size: 13px; font-weight: 600; margin-top: 4px;")
        hist_layout.addWidget(detail_lbl)

        self.detail_box = QTextEdit()
        self.detail_box.setReadOnly(True)
        self.detail_box.setMaximumHeight(200)
        self.detail_box.setStyleSheet("font-size: 12px; font-family: Consolas, monospace;")
        hist_layout.addWidget(self.detail_box)

        layout.addWidget(hist_group)
        return panel

    # ─────────────────────────────────────────────────────────────────────
    # Detection
    # ─────────────────────────────────────────────────────────────────────

    def detect_phones(self, _checked: bool = False,
                      on_complete: Optional[Callable[[], None]] = None) -> None:
        """
        Kick off phone detection on a background thread. MTP enumeration shells
        out to PowerShell and can take tens of seconds, so it must never run on
        the GUI thread. ``on_complete`` (if given) fires once the list has been
        populated, whether or not the current call started a new scan.
        """
        if on_complete is not None:
            self._detect_callbacks.append(on_complete)

        if self._detect_thread is not None:
            return  # a scan is already running; callback will fire when it ends

        self.detect_btn.setEnabled(False)
        self.detect_btn.setText("Detecting…")
        self.phone_list.clear()
        self.no_phone_label.setVisible(False)

        self._detect_thread = QThread()
        self._detect_worker = PhoneDetectWorker(self.manager)
        self._detect_worker.moveToThread(self._detect_thread)

        self._detect_thread.started.connect(self._detect_worker.run)
        self._detect_worker.finished.connect(self._on_detect_finished)
        self._detect_worker.finished.connect(self._detect_thread.quit)
        self._detect_worker.finished.connect(self._detect_worker.deleteLater)
        self._detect_thread.finished.connect(self._cleanup_detect_thread)
        self._detect_thread.finished.connect(self._detect_thread.deleteLater)

        self._detect_thread.start()

    def _on_detect_finished(self, devices: list) -> None:
        self.detected = devices

        self.detect_btn.setEnabled(True)
        self.detect_btn.setText("Detect Phones")

        if not self.detected:
            self.no_phone_label.setVisible(True)
            self.backup_btn.setEnabled(False)
        else:
            for device in self.detected:
                mode = "MTP" if device.access_type == "mtp" else "Drive"
                label = f"{device.display_name}  [{mode}]"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, device)
                self.phone_list.addItem(item)
            self.phone_list.setCurrentRow(0)  # backup_btn enabled by _on_phone_selection_changed

        callbacks, self._detect_callbacks = self._detect_callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass

    def _cleanup_detect_thread(self) -> None:
        self._detect_thread = None
        self._detect_worker = None

    def shutdown(self) -> None:
        stop_thread(getattr(self, "_detect_thread", None), getattr(self, "_detect_worker", None))
        # Running phone backups can take a long time to reach a stop point;
        # ask each to stop and give it a bounded wait, then terminate at exit.
        for panel in list(self._panels):
            panel.shutdown()

    def _on_phone_selection_changed(self) -> None:
        self._update_backup_btn_state()

    def _update_backup_btn_state(self) -> None:
        running_devices = {p.device.shell_path or p.device.drive_root or p.device.name
                            for p in self._panels if p.is_running()}
        selected = self.phone_list.selectedItems()
        startable = [
            item for item in selected
            if isinstance(item.data(Qt.ItemDataRole.UserRole), PhoneDevice)
            and (item.data(Qt.ItemDataRole.UserRole).shell_path
                 or item.data(Qt.ItemDataRole.UserRole).drive_root
                 or item.data(Qt.ItemDataRole.UserRole).name) not in running_devices
        ]
        self.backup_btn.setEnabled(bool(startable))

    # ─────────────────────────────────────────────────────────────────────
    # Backup start
    # ─────────────────────────────────────────────────────────────────────

    def _start_selected_backups(self) -> None:
        items = self.phone_list.selectedItems()
        if not items:
            return

        running_devices = {p.device.shell_path or p.device.drive_root or p.device.name
                            for p in self._panels if p.is_running()}

        devices: list[PhoneDevice] = []
        for item in items:
            device = item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(device, PhoneDevice):
                continue
            key = device.shell_path or device.drive_root or device.name
            if key in running_devices:
                continue
            devices.append(device)

        if not devices:
            return

        force_verify = self.verify_everything_chk.isChecked()

        for device in devices:
            self._start_backup_panel(device, force_verify)

        count = len(devices)
        self.global_status_lbl.setText(
            f"Backing up {count} phone{'s' if count != 1 else ''}…"
        )
        self.detect_btn.setEnabled(False)
        self._update_backup_btn_state()

    def start_backup_for_specific_device(self, device: PhoneDevice) -> bool:
        """
        Start a backup for exactly this device, bypassing the list selection —
        used by the "Backup Now" entry point on the Devices page. Returns
        False (and does nothing) if this device is already backing up.
        """
        key = device.shell_path or device.drive_root or device.name
        running_devices = {p.device.shell_path or p.device.drive_root or p.device.name
                            for p in self._panels if p.is_running()}
        if key in running_devices:
            return False

        self._start_backup_panel(device, self.verify_everything_chk.isChecked())
        self.global_status_lbl.setText(f"Backing up {device.display_name}…")
        self.detect_btn.setEnabled(False)
        self._update_backup_btn_state()
        return True

    def _start_backup_panel(self, device: PhoneDevice, force_verify: bool) -> None:
        self.no_active_lbl.setVisible(False)

        panel = DeviceBackupPanel(self.manager, device)
        panel.request_remove.connect(self._remove_panel)
        panel.run_finished.connect(self._on_panel_finished)
        panel.run_failed.connect(self._on_panel_failed)
        panel.activity_state_changed.connect(self._on_panel_activity_changed)

        # Newest panel at the top, above the trailing stretch.
        self.panels_layout.insertWidget(0, panel)
        self._panels.append(panel)

        panel.start(force_verify)

    def _remove_panel(self, panel: DeviceBackupPanel) -> None:
        if panel.is_running():
            return
        panel.shutdown()
        if panel in self._panels:
            self._panels.remove(panel)
        self.panels_layout.removeWidget(panel)
        panel.setParent(None)
        panel.deleteLater()
        self.no_active_lbl.setVisible(not self._panels)

    def _on_panel_activity_changed(self) -> None:
        self._update_backup_btn_state()
        if not any(p.is_running() for p in self._panels):
            self.detect_btn.setEnabled(True)
            if self._panels:
                done = sum(1 for p in self._panels if p.finished_ok)
                self.global_status_lbl.setText(
                    f"{done} of {len(self._panels)} backup(s) complete."
                )

    # ─────────────────────────────────────────────────────────────────────
    # Signals from panels — cross-cutting bookkeeping (history, DB)
    # ─────────────────────────────────────────────────────────────────────

    def _on_panel_finished(self, panel: DeviceBackupPanel, result: dict) -> None:
        cancelled = result.get("cancelled", False)
        self._refresh_history()

        if cancelled or not hasattr(self, "device_manager") or not self.device_manager:
            return

        copy_r = result.get("copy_result") or {}
        manifest_summary = result.get("manifest_summary") or {}
        try:
            device_name = result.get("device")
            if not device_name:
                return
            device = self.device_manager.get_device_by_name(device_name)
            if not device:
                return
            device_id = device.get("device_id")
            if not device_id:
                return
            latest_dir_str = result.get("latest_dir")
            if not latest_dir_str:
                return

            copied_files = copy_r.get("copied", 0)
            total_bytes = int(manifest_summary.get("confirmed_bytes") or 0)
            self.device_manager.update_last_backup(
                device_id, latest_dir_str, copied_files, total_bytes
            )
        except Exception as e:
            print(f"Error updating device backup in database: {e}")

    def _on_panel_failed(self, panel: DeviceBackupPanel, error_message: str) -> None:
        confirmed_note = None
        try:
            for dev in self.manager.list_all_backed_up_devices():
                if Path(dev.get("folder", "")).name == panel.device.safe_folder_name:
                    confirmed = dev.get("confirmed_files", 0)
                    confirmed_note = (
                        f"{confirmed} file(s) confirmed backed up so far in "
                        f"{dev.get('folder', '?')}. Nothing already copied is "
                        f"lost — click Start Backup again to pick up where "
                        f"this left off."
                    )
                    break
        except Exception:
            pass

        if confirmed_note:
            panel.log_box.append(f"\n{confirmed_note}")
            panel._scroll_log_to_end()

        QMessageBox.warning(
            self, f"Backup Failed — {panel.device.display_name}",
            f"{error_message}\n\n{confirmed_note}" if confirmed_note else error_message,
        )
        self._refresh_history()

    def _cleanup_duplicates(self) -> None:
        device_dir = None
        display_name = None

        item = self.phone_list.currentItem()
        device = item.data(Qt.ItemDataRole.UserRole) if item else None
        if isinstance(device, PhoneDevice):
            device_dir = self.manager._device_dir(device)
            display_name = device.display_name
        else:
            hist_item = self.history_list.currentItem()
            dev_info = hist_item.data(Qt.ItemDataRole.UserRole) if hist_item else None
            if isinstance(dev_info, dict) and dev_info.get("folder"):
                device_dir = Path(dev_info["folder"])
                display_name = dev_info.get("name")

        if not device_dir:
            QMessageBox.warning(
                self, "Clean Up Duplicates",
                "Select a phone above, or a device in the history list on the right, first."
            )
            return

        result = self.manager.merge_duplicate_groups_in_latest_dir(device_dir)
        QMessageBox.information(
            self,
            "Clean Up Duplicates",
            f"{display_name or device_dir.name}: removed {result['removed_files']} duplicate "
            f"file(s) across {result['groups_merged']} group(s), reclaiming "
            f"{_fmt_bytes(result['reclaimed_bytes'])}.",
        )
        self._refresh_history()
    # ─────────────────────────────────────────────────────────────────────
    # History panel
    # ─────────────────────────────────────────────────────────────────────

    def _refresh_history(self) -> None:
        # Synchronous path, kept for the in-view Refresh button and post-backup
        # refresh. The Devices page feeds populate_history() from a worker so it
        # never blocks on manifest parsing during a page switch.
        self.populate_history(self.manager.list_all_backed_up_devices())

    def populate_history(self, devices: list) -> None:
        self.history_list.clear()
        self.detail_box.clear()

        if not devices:
            item = QListWidgetItem("No backups yet.")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.history_list.addItem(item)
            return

        for dev in devices:
            runs = dev["snapshot_count"]
            label = f"{dev['name']}  ({runs} run{'s' if runs != 1 else ''})"

            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, dev)

            self.history_list.addItem(item)


    def _on_history_selected(
        self,
        current: Optional[QListWidgetItem],
        _previous: Optional[QListWidgetItem],
    ) -> None:
        self.detail_box.clear()

        if not current:
            return

        dev = current.data(Qt.ItemDataRole.UserRole)

        if not isinstance(dev, dict):
            return

        total_size = dev.get("total_size")

        lines = [
            f"Name       : {dev.get('name', '?')}",
            f"Folder     : {dev.get('folder', '?')}",
            f"Runs       : {dev.get('snapshot_count', 0)}",
            f"Last backup: {dev.get('last_backup') or 'Unknown'}",
            f"Total size : {_fmt_bytes(total_size) if total_size is not None else 'Not calculated'}",
            f"Latest OK  : {'Yes' if dev.get('has_latest') else 'No'}",
            f"Confirmed files     : {dev.get('confirmed_files', '?')}",
            f"Permanently skipped : {dev.get('permanently_skipped_files', 0)}",
        ]

        if dev.get("pending_files"):
            lines.append(
                f"Legacy staging files: {dev['pending_files']}"
            )

        if dev.get("latest_path"):
            lines.append(f"Latest at : {dev['latest_path']}")

        self.detail_box.setPlainText("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_bytes(n: int | float) -> str:
    return human_bytes(n)
