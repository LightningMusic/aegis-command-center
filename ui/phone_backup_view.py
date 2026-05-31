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


# ─────────────────────────────────────────────────────────────────────────────
# Background worker
# ─────────────────────────────────────────────────────────────────────────────

class PhoneBackupWorker(QObject):
    """Runs the backup in a QThread and emits progress / completion signals."""

    progress  = pyqtSignal(int, str)    # (percent, message)  – percent=-1 → append only
    finished  = pyqtSignal(dict)
    failed    = pyqtSignal(str)

    def __init__(self, manager: PhoneBackupManager, device: PhoneDevice) -> None:
        super().__init__()
        self.manager = manager
        self.device  = device
        self._stop   = False

    def run(self) -> None:
        try:
            result = self.manager.backup_phone(
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
        self.detected: list[PhoneDevice] = []
        self.backup_thread:  Optional[QThread]  = None
        self.backup_worker:  Optional[PhoneBackupWorker] = None

        self._build_ui()
        self._refresh_history()

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
        root.addWidget(splitter)

        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([580, 380])

    # ── Left panel ────────────────────────────────────────────────────────

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(10)
        panel.setLayout(layout)

        # Connected devices group
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

        # Backup group
        backup_group = QGroupBox("Backup")
        backup_layout = QVBoxLayout()
        backup_group.setLayout(backup_layout)

        backup_btns = QHBoxLayout()
        self.backup_btn = QPushButton("Start Backup")
        self.backup_btn.setMinimumHeight(34)
        self.backup_btn.setEnabled(False)
        self.backup_btn.clicked.connect(self._start_or_stop)
        backup_btns.addWidget(self.backup_btn)
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

        # Divider
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #333;")
        backup_layout.addWidget(line)

        log_lbl = QLabel("Backup Log")
        log_lbl.setStyleSheet("font-size: 13px; font-weight: 600;")
        backup_layout.addWidget(log_lbl)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(200)
        self.log_box.setStyleSheet("font-size: 12px; font-family: Consolas, monospace;")
        backup_layout.addWidget(self.log_box)

        layout.addWidget(backup_group)
        return panel

    # ── Right panel ───────────────────────────────────────────────────────

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
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
        self.backup_btn.setText("Stop Backup")
        self.detect_btn.setEnabled(False)

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
        if percent >= 0:
            self.progress_bar.setValue(percent)
            # Keep format in sync with percent
            self.progress_bar.setFormat(f"{percent}%")
        self.log_box.append(message)
        # Auto-scroll
        sb = self.log_box.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

    def _on_finished(self, result: dict) -> None:
        cancelled = result.get("cancelled", False)
        self.progress_bar.setValue(100 if not cancelled else self.progress_bar.value())
        self.progress_bar.setFormat("Cancelled" if cancelled else "Complete ✓")

        copy_r  = result.get("copy_result")   or {}
        org_r   = result.get("organize_result") or {}
        cats    = org_r.get("categories")     or {}

        lines = [
            "─" * 48,
            f"Device  : {result.get('device', '?')}",
            f"Status  : {'Cancelled' if cancelled else 'Completed'}",
            f"Started : {result.get('started_at', '?')}",
            f"Finished: {result.get('completed_at', '?')}",
            "",
            f"Files copied    : {copy_r.get('copied', 0)}",
            f"Files skipped   : {copy_r.get('skipped', 0)}",
            f"Files organised : {org_r.get('files_organized', 0)}",
            "",
            "── Categories ──────────────────────────",
        ]
        for cat, count in sorted(cats.items()):
            lines.append(f"  {cat:<20} {count:>5} file(s)")

        errors = result.get("errors") or []
        if errors:
            lines += ["", "── Errors (first 10) ───────────────────"]
            for e in errors[:10]:
                lines.append(f"  {e}")

        lines += [
            "",
            f"Snapshot : {result.get('snapshot_dir', '?')}",
            f"Latest   : {result.get('latest_dir',  '?')}",
            "─" * 48,
        ]

        self.log_box.append("\n".join(lines))
        sb = self.log_box.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

        self.status_lbl.setText(
            "Backup complete." if not cancelled else "Backup stopped."
        )
        self._refresh_history()

    def _on_failed(self, error_message: str) -> None:
        self.progress_bar.setFormat("Failed")
        self.status_lbl.setText(f"Error: {error_message}")
        self.log_box.append(f"\nERROR: {error_message}")
        QMessageBox.warning(self, "Backup Failed", error_message)

    def _cleanup_backup_thread(self) -> None:
        self.backup_thread = None
        self.backup_worker = None

        self.backup_btn.setText("Start Backup")

        row = self.phone_list.currentRow()
        self.backup_btn.setEnabled(row >= 0 and bool(self.detected))
        self.detect_btn.setEnabled(True)


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
            snaps = dev["snapshot_count"]
            label = f"{dev['name']}  ({snaps} snapshot{'s' if snaps != 1 else ''})"
            item  = QListWidgetItem(label)
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

        lines = [
            f"Name      : {dev.get('name', '?')}",
            f"Folder    : {dev.get('folder', '?')}",
            f"Snapshots : {dev.get('snapshot_count', 0)}",
            f"Last backup: {dev.get('last_backup') or 'Unknown'}",
            f"Total size: {_fmt_bytes(dev.get('total_size', 0))}",
            f"Latest OK : {'Yes' if dev.get('has_latest') else 'No'}",
        ]
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
