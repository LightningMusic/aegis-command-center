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
│                                 │  Name:      Galaxy S24       │
│  ─── Backup ───────────────     │  Folder:    …/Phones/…       │
│  [Start Backup]                 │  Snapshots: 3                │
│  ████████░░░░  42 %             │  Last:      2026-05-30…      │
│                                 │  Size:      4.2 GB           │
│  ┌──────────────────────────┐   │                              │
│  │ Backup log               │   │                              │
│  │ …                        │   │                              │
│  └──────────────────────────┘   │                              │
└─────────────────────────────────┴──────────────────────────────┘
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
import time
from unittest import result
from PyQt6.QtCore import QObject, QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


from core.phone_backup_manager import PhoneBackupManager, PhoneDevice
from core.device_manager import DeviceManager

# ─────────────────────────────────────────────────────────────────────────────
# Background worker
# ─────────────────────────────────────────────────────────────────────────────

class PhoneBackupWorker(QObject):
    """Runs the backup in a QThread and emits progress / completion signals."""

    progress  = pyqtSignal(int, str)
    finished  = pyqtSignal(dict)
    failed    = pyqtSignal(str)

    def __init__(self, manager: PhoneBackupManager, device: PhoneDevice) -> None:
        super().__init__()
        self.manager = manager
        self.device  = device
        self._stop   = False

    def run(self) -> None:
        try:
            result = self.manager.backup_phone_until_complete(
                self.device,
                progress_callback=self._emit_progress,
                should_stop=lambda: self._stop,
            )
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _emit_progress(self, percent: int, message: str) -> None:
        self.progress.emit(percent, message)

    def stop(self) -> None:
        self._stop = True


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
        self.backup_thread:  Optional[QThread]  = None
        self.backup_worker:  Optional[PhoneBackupWorker] = None
        self._backup_started_at: Optional[float] = None
        self._last_activity_at: Optional[float] = None

        self.activity_timer = QTimer(self)
        self.activity_timer.setInterval(1000)
        self.activity_timer.timeout.connect(self._tick_activity)

        self._build_ui()
        self._refresh_history()


    def _tick_activity(self) -> None:
        if self._backup_started_at is None:
            return
        now = time.monotonic()
        elapsed = int(now - self._backup_started_at)
        since_activity = int(now - (self._last_activity_at or self._backup_started_at))

        elapsed_txt = f"{elapsed // 60}m {elapsed % 60:02d}s"
        if since_activity < 10:
            activity_txt = "active now"
        else:
            activity_txt = f"last update {since_activity}s ago"

        self.activity_lbl.setText(f"Elapsed: {elapsed_txt} · {activity_txt}")


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
            "Connect your phone via USB cable, switch it to File Transfer (MTP) mode, "
            "then click Detect."
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
        splitter.setSizes([520, 320])

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
        self.phone_list.currentRowChanged.connect(self._on_phone_selected)
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
        self.backup_btn.clicked.connect(self._start_or_stop)
        backup_btns.addWidget(self.backup_btn)

        self.cleanup_btn = QPushButton("Clean Up Duplicates")
        self.cleanup_btn.setMinimumHeight(34)
        self.cleanup_btn.clicked.connect(self._cleanup_duplicates)
        backup_btns.addWidget(self.cleanup_btn)

        backup_btns.addStretch()
        backup_layout.addLayout(backup_btns)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Idle")
        backup_layout.addWidget(self.progress_bar)

        self.status_lbl = QLabel("Select a phone above, then click Start Backup.")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("font-size: 13px; color: #AAA;")
        backup_layout.addWidget(self.status_lbl)

        self.activity_lbl = QLabel("")
        self.activity_lbl.setStyleSheet("font-size: 12px; color: #777;")
        backup_layout.addWidget(self.activity_lbl)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #333;")
        backup_layout.addWidget(line)

        log_lbl = QLabel("Backup Log")
        log_lbl.setStyleSheet("font-size: 13px; font-weight: 600;")
        backup_layout.addWidget(log_lbl)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(120)
        self.log_box.setStyleSheet("font-size: 12px; font-family: Consolas, monospace;")
        backup_layout.addWidget(self.log_box)

        layout.addWidget(backup_group)
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

    def detect_phones(self) -> None:
        self.detect_btn.setEnabled(False)
        self.detect_btn.setText("Detecting…")
        self.phone_list.clear()
        self.no_phone_label.setVisible(False)

        # Detection is fast enough (~1-2 s) to run on main thread.
        self.detected = self.manager.detect_phones()

        self.detect_btn.setEnabled(True)
        self.detect_btn.setText("Detect Phones")

        if not self.detected:
            self.no_phone_label.setVisible(True)
            self.backup_btn.setEnabled(False)
            return

        for device in self.detected:
            mode  = "MTP" if device.access_type == "mtp" else "Drive"
            label = f"{device.display_name}  [{mode}]"
            item  = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, device)
            self.phone_list.addItem(item)

        self.phone_list.setCurrentRow(0)
        # backup_btn enabled by _on_phone_selected

    def _on_phone_selected(self, row: int) -> None:
        self.backup_btn.setEnabled(row >= 0 and self.backup_thread is None)

    # ─────────────────────────────────────────────────────────────────────
    # Backup start / stop
    # ─────────────────────────────────────────────────────────────────────

    def _start_or_stop(self) -> None:
        # If a backup is running, request stop.
        if self.backup_thread is not None and self.backup_worker is not None:
            self.backup_worker.stop()
            self.backup_btn.setEnabled(False)
            self.progress_bar.setFormat("Stopping…")
            self.status_lbl.setText("Stopping — finishing the current file first…")
            return

        if self.backup_thread is not None:
            return  # cleanup pending

        item = self.phone_list.currentItem()
        if not item:
            return

        device = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(device, PhoneDevice):
            return

        self._start_backup(device)


    def _start_backup(self, device: PhoneDevice) -> None:
        self.log_box.clear()
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Starting…")
        self.status_lbl.setText(f"Backing up {device.display_name}…")
        self.activity_lbl.setText("")
        self.backup_btn.setText("Stop Backup")
        self.detect_btn.setEnabled(False)

        self._backup_started_at = time.monotonic()
        self._last_activity_at = self._backup_started_at
        self.activity_timer.start()
        thread = QThread()
        worker = PhoneBackupWorker(self.manager, device)

        self.backup_thread = thread
        self.backup_worker = worker

        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)

        # Cleanup chain
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._cleanup_backup_thread)
        thread.finished.connect(thread.deleteLater)

        thread.start()


    # ─────────────────────────────────────────────────────────────────────
    # Signals from worker
    # ─────────────────────────────────────────────────────────────────────

    def _on_progress(self, percent: int, message: str) -> None:
        self._last_activity_at = time.monotonic()

        if percent == -2:
            # Heartbeat-only update — keep the status line fresh, skip the log.
            self.status_lbl.setText(message)
            return

        if percent == -3:
            # A meaningful phase change or retry milestone — worth logging.
            self.status_lbl.setText(message)
            self.log_box.append(f"\n— {message} —")
            sb = self.log_box.verticalScrollBar()
            if sb:
                sb.setValue(sb.maximum())
            return

        if percent >= 0:
            self.progress_bar.setValue(percent)
            self.progress_bar.setFormat(f"{percent}%")
        self.log_box.append(message)
        sb = self.log_box.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

    def _on_finished(self, result: dict) -> None:
        cancelled = result.get("cancelled", False)
        stalled = result.get("stalled", False)
        connection_failed = result.get("connection_failed", False)
        unreachable = stalled or connection_failed
        attempts = result.get("attempts_used", 1)
        interruptions = result.get("interruption_count", 0)

        self.progress_bar.setValue(100)
        if unreachable:
            self.progress_bar.setFormat("Paused — phone unreachable")
        elif cancelled:
            self.progress_bar.setFormat("Stopped")
        else:
            self.progress_bar.setFormat("Complete ✓")

        copy_r  = result.get("copy_result")   or {}
        index_r = result.get("index_result") or {}
        manifest_summary = result.get("manifest_summary") or {}

        lines = [
            "Preserved folder structure in latest/",
        ]

        errors = result.get("errors") or []
        if errors:
            lines += ["", "── Errors (first 10) ───────────────────"]
            for e in errors[:10]:
                lines.append(f"  {e}")

        lines += [
            "",
            f"Folder : {result.get('device_folder', '?')}",
            f"Latest : {result.get('latest_dir',  '?')}",
            "─" * 48,
        ]

        if unreachable:
            lines.append(
                "The phone stopped responding or lost its USB/MTP connection, and "
                "automatic retries were exhausted. Reconnect it and click Start "
                "Backup to keep going — already-copied files won't be re-transferred."
            )

        self.log_box.append("\n".join(lines))
        sb = self.log_box.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

        if unreachable:
            self.status_lbl.setText("Paused — phone unreachable after several retries. Reconnect and run again.")
        elif cancelled:
            self.status_lbl.setText("Backup stopped — progress saved.")
        else:
            self.status_lbl.setText("Backup complete.")

        self._refresh_history()

        # Update last backup in database using device_manager
        if not cancelled and hasattr(self, "device_manager") and self.device_manager:
            try:
                device_name = result.get("device")
                if not device_name:
                    return
                device = self.device_manager.get_device_by_name(device_name)
                if device:
                    device_id = device.get("device_id")
                    if not device_id:
                        return
                    copied_files = copy_r.get("copied", 0)
                    self._last_activity_at = time.monotonic()
                    latest_dir_str = result.get("latest_dir")
                    if not latest_dir_str:
                        return

                    total_bytes = 0
                    backup_path = Path(latest_dir_str)
                    if backup_path.exists():
                        for p in backup_path.rglob("*"):
                            if p.is_file():
                                total_bytes += p.stat().st_size
                    self.device_manager.update_last_backup(
                        device_id,
                        latest_dir_str,
                        copied_files,
                        total_bytes
                    )
            except Exception as e:
                print(f"Error updating device backup in database: {e}")

    def _on_failed(self, error_message: str) -> None:
        self.progress_bar.setFormat("Failed")
        self.status_lbl.setText(f"Error: {error_message}")
        self.log_box.append(f"\nERROR: {error_message}")
        QMessageBox.warning(self, "Backup Failed", error_message)

    def _cleanup_backup_thread(self) -> None:
        self.backup_thread = None
        self.backup_worker = None
        self.activity_timer.stop()
        self._backup_started_at = None

        self.backup_btn.setText("Start Backup")

        row = self.phone_list.currentRow()
        self.backup_btn.setEnabled(row >= 0 and bool(self.detected))
        self.detect_btn.setEnabled(True)

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
        self.history_list.clear()
        self.detail_box.clear()

        devices = self.manager.list_all_backed_up_devices()

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
    size = float(n)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024

    return f"{size:.1f} B"
