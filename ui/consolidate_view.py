"""
ui/consolidate_view.py
======================
"Consolidate Drives": compare a SOURCE drive/folder against a TARGET, review
what the planner wants to do with every file, then apply it. Nothing is moved
or deleted until you press Apply.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.drive_consolidator import ConsolidationPlanner, DbHashCache, Plan, PlanEntry
from ui.preview_panel import PreviewPanel
from ui.thread_utils import stop_thread


from core.util import connected_drives, human_bytes


def _h(n: int) -> str:
    return human_bytes(n)


_ACTION_LABELS = {
    "MOVE": "Move to target",
    "ALREADY_THERE": "Delete from source — identical copy already on target",
    "DUPLICATE_ELSEWHERE": "Delete from source — duplicate content on target",
    "CONFLICT": "Conflict — same path, different content",
    "CORRUPT": "Corrupt / unreadable — left untouched",
    "JUNK": "Delete — junk / regenerable",
    "SKIP": "Skipped — protected system location",
}
_CONFLICT_CHOICES = [
    ("", "(leave for now)"),
    ("keep_source", "keep source (back up target copy)"),
    ("keep_target", "keep target (delete source)"),
    ("keep_both", "keep both (copy source alongside)"),
]


class _PlanWorker(QObject):
    progress = pyqtSignal(str, int, int, str)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, planner: ConsolidationPlanner) -> None:
        super().__init__()
        self.planner = planner
        self._stop = False

    def run(self) -> None:
        try:
            plan = self.planner.build_plan(
                progress=lambda ph, d, t, m: self.progress.emit(ph, d, t, m),
                should_stop=lambda: self._stop,
            )
            self.done.emit(plan)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))

    def stop(self) -> None:
        self._stop = True


class _ExecWorker(QObject):
    progress = pyqtSignal(str, int, int, str)
    done = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, planner: ConsolidationPlanner, plan: Plan) -> None:
        super().__init__()
        self.planner = planner
        self.plan = plan
        self._stop = False

    def run(self) -> None:
        try:
            result = self.planner.execute(
                self.plan,
                progress=lambda ph, d, t, m: self.progress.emit(ph, d, t, m),
                should_stop=lambda: self._stop,
            )
            self.done.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))

    def stop(self) -> None:
        self._stop = True


class ConsolidateView(QWidget):
    tasks_changed = pyqtSignal()  # kept for symmetry with other views

    def __init__(self, file_manager) -> None:
        super().__init__()
        self.file_manager = file_manager
        self.planner: Optional[ConsolidationPlanner] = None
        self.plan: Optional[Plan] = None
        self._thread: Optional[QThread] = None
        self._worker = None
        self._conflict_combos: Dict[int, QComboBox] = {}
        self._item_by_path: Dict[str, QTreeWidgetItem] = {}

        self._build_ui()
        self.refresh_drives()

    # ── UI ─────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        title = QLabel("Consolidate Drives")
        title.setStyleSheet("font-size: 22px; font-weight: 600;")
        root.addWidget(title)

        self.blurb = QLabel()
        self.blurb.setWordWrap(True)
        self.blurb.setStyleSheet("color: #888;")
        root.addWidget(self.blurb)

        pick = QGroupBox("Source and target")
        form = QFormLayout(pick)

        self.source_combo = QComboBox()
        self.source_combo.setEditable(True)
        src_row = QHBoxLayout()
        src_row.addWidget(self.source_combo, 1)
        src_browse = QPushButton("Browse…")
        src_browse.clicked.connect(lambda: self._browse(self.source_combo))
        src_row.addWidget(src_browse)
        form.addRow("Source (free this up)", self._wrap(src_row))

        self.target_combo = QComboBox()
        self.target_combo.setEditable(True)
        tgt_row = QHBoxLayout()
        tgt_row.addWidget(self.target_combo, 1)
        tgt_browse = QPushButton("Browse…")
        tgt_browse.clicked.connect(lambda: self._browse(self.target_combo))
        tgt_row.addWidget(tgt_browse)
        form.addRow("Target (keep everything)", self._wrap(tgt_row))

        mode_row = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItem(
            "Consolidate — copy missing files to the target, then remove the originals",
            "consolidate",
        )
        self.mode_combo.addItem(
            "Reclaim — only remove files that are already backed up on the target",
            "reclaim",
        )
        self.mode_combo.setToolTip(
            "Consolidate moves everything onto the target drive.\n"
            "Reclaim never copies — it just deletes, from the source, files that "
            "are already byte-identical on the target (your backup drive). "
            "Anything not on the target is left alone."
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(QLabel("Mode:"))
        mode_row.addWidget(self.mode_combo, 1)
        form.addRow("Mode", self._wrap(mode_row))

        opts = QHBoxLayout()
        self.verify_combo = QComboBox()
        self.verify_combo.addItem("Hash only (fast)", "none")
        self.verify_combo.addItem("Structural check (zip CRC, image/pdf/sqlite headers)", "structural")
        self.verify_combo.addItem("Full decode / play-through (slow — every frame & page)", "playthrough")
        self.verify_combo.setCurrentIndex(1)
        opts.addWidget(QLabel("Integrity:"))
        opts.addWidget(self.verify_combo, 1)
        form.addRow("Verify", self._wrap(opts))

        opts_j = QHBoxLayout()
        self.aggressive_box = QCheckBox("Treat node_modules / windows.old / build caches as junk")
        opts_j.addWidget(self.aggressive_box)
        opts_j.addStretch()
        form.addRow("Options", self._wrap(opts_j))

        opts2 = QHBoxLayout()
        opts2.addWidget(QLabel("Removed originals:"))
        self.delete_combo = QComboBox()
        self.delete_combo.addItem("Recycle Bin (recoverable — space frees when emptied)", "recycle")
        self.delete_combo.addItem("Permanent delete (frees space immediately, no undo)", "permanent")
        self.delete_combo.addItem("Quarantine folder on source (review, then delete)", "quarantine")
        self.delete_combo.setToolTip(
            "A source file is only removed after its copy on the target is "
            "double-verified. Recycle Bin and Quarantine keep the original on the "
            "source drive until you clear them, so the drive won't show the freed "
            "space right away."
        )
        opts2.addWidget(self.delete_combo, 1)
        self.ignore_cache_box = QCheckBox("Ignore saved hashes (re-read every file)")
        self.ignore_cache_box.setToolTip(
            "Normally a file whose size and modified-time haven't changed since a "
            "previous run is trusted from the saved hash and not re-read. Tick this "
            "to force a full re-read of everything."
        )
        opts2.addWidget(self.ignore_cache_box)
        opts2.addStretch()
        form.addRow("", self._wrap(opts2))

        root.addWidget(pick)

        self.pair_warn = QLabel()
        self.pair_warn.setStyleSheet("color: #c62828; font-weight: 600;")
        self.pair_warn.setVisible(False)
        root.addWidget(self.pair_warn)

        buttons = QHBoxLayout()
        self.analyze_btn = QPushButton("Analyze")
        self.analyze_btn.clicked.connect(self.analyze)
        buttons.addWidget(self.analyze_btn)

        self.refresh_btn = QPushButton("Refresh Drives")
        self.refresh_btn.clicked.connect(self.refresh_drives)
        buttons.addWidget(self.refresh_btn)

        self.apply_btn = QPushButton("Apply Selected")
        self.apply_btn.setEnabled(False)
        self.apply_btn.setStyleSheet("font-weight: 600;")
        self.apply_btn.clicked.connect(self.apply_selected)
        buttons.addWidget(self.apply_btn)

        self.cancel_btn = QPushButton("Stop")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._stop)
        buttons.addWidget(self.cancel_btn)
        buttons.addStretch()
        root.addLayout(buttons)

        self.progress = QProgressBar()
        self.progress.setFormat("Idle")
        root.addWidget(self.progress)

        self.summary_label = QLabel("Pick a source and target, then Analyze.")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("font-size: 13px;")
        root.addWidget(self.summary_label)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["File", "Size", "Action / choice", "Details", "Integrity"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.currentItemChanged.connect(self._on_current_changed)
        header = self.tree.header()
        if header is not None:
            header.setStretchLastSection(False)
            header.resizeSection(0, 360)
            header.resizeSection(1, 80)
            header.resizeSection(2, 200)
            header.resizeSection(3, 300)

        self.preview = PreviewPanel()
        self.preview.verified.connect(self._on_preview_verified)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.tree)
        split.addWidget(self.preview)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([680, 460])
        root.addWidget(split, 1)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(140)
        self.log.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")
        root.addWidget(self.log)

        self._reclaim_row = QWidget()
        rr = QHBoxLayout(self._reclaim_row)
        rr.setContentsMargins(0, 0, 0, 0)
        rr.addWidget(QLabel("Removed originals are in the Recycle Bin —"))
        self.empty_bin_btn = QPushButton("Empty Recycle Bin on source drive")
        self.empty_bin_btn.clicked.connect(self._empty_recycle_bin)
        rr.addWidget(self.empty_bin_btn)
        rr.addStretch()
        self._reclaim_row.setVisible(False)
        root.addWidget(self._reclaim_row)

        self.source_combo.editTextChanged.connect(self._validate_pair)
        self.target_combo.editTextChanged.connect(self._validate_pair)
        self.source_combo.currentIndexChanged.connect(self._validate_pair)
        self.target_combo.currentIndexChanged.connect(self._validate_pair)
        self._on_mode_changed()

    def _on_mode_changed(self, *_a):
        if self.mode_combo.currentData() == "reclaim":
            self.blurb.setText(
                "Reclaim: for every file on the source, check whether a "
                "byte-identical copy already exists on the target (your backup "
                "drive). If it does, the source copy is removed. Nothing is ever "
                "copied to the target, and files that aren't backed up are left "
                "alone. Removals only happen after a fresh full-read hash match."
            )
        else:
            self.blurb.setText(
                "Consolidate: compare every file on the source against the "
                "target, copy across what's missing, delete verified duplicates "
                "and junk, flag anything corrupt. A source file is only removed "
                "after an identical copy is confirmed on the target."
            )

    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    def _drive_of(self, combo: QComboBox) -> str:
        p = self._combo_path(combo)
        if not p:
            return ""
        return os.path.splitdrive(os.path.abspath(p))[0].upper()

    def _validate_pair(self, *_a) -> None:
        """Block picking the same physical drive for source and target."""
        sd, td = self._drive_of(self.source_combo), self._drive_of(self.target_combo)
        same = bool(sd and td and sd == td)
        self.pair_warn.setVisible(same)
        if same:
            self.pair_warn.setText(
                f"Source and target are both on {sd} — consolidation moves data "
                f"between two different drives. Pick another drive."
            )
        self.analyze_btn.setEnabled(not same and self._thread is None)

    # ── drive pickers ─────────────────────────────────────────────────
    def refresh_drives(self) -> None:
        keep_src = self.source_combo.currentText()
        keep_tgt = self.target_combo.currentText()
        self.source_combo.clear()
        self.target_combo.clear()
        for d in connected_drives(force=True):
            if not d["is_scan_eligible"]:
                continue
            label = (
                f"{d['root']}  ({d['type_name']}, {_h(d['free_bytes'])} free "
                f"of {_h(d['total_bytes'])})"
            )
            self.source_combo.addItem(label, d["root"])
            self.target_combo.addItem(label, d["root"])
        if keep_src:
            self.source_combo.setCurrentText(keep_src)
        if keep_tgt:
            self.target_combo.setCurrentText(keep_tgt)

    def _browse(self, combo: QComboBox) -> None:
        start = self._combo_path(combo) or ""
        chosen = QFileDialog.getExistingDirectory(self, "Choose folder", start)
        if chosen:
            combo.setEditText(chosen)

    @staticmethod
    def _combo_path(combo: QComboBox) -> str:
        text = combo.currentText().strip()
        idx = combo.currentIndex()
        # Only trust the item's stored root when the field still shows that item;
        # once the user types or browses, the edit text wins.
        if idx >= 0 and text == combo.itemText(idx).strip():
            data = combo.currentData()
            if data:
                return str(data)
        # allow "E:\  (Removable, …)" style entries typed/selected
        if len(text) >= 2 and text[1] == ":":
            return text.split("  ")[0].strip()
        return text

    # ── analyze ──────────────────────────────────────────────────────
    def analyze(self) -> None:
        if self._thread is not None:
            return
        source = self._combo_path(self.source_combo)
        target = self._combo_path(self.target_combo)
        if not source or not target:
            QMessageBox.warning(self, "Consolidate", "Pick both a source and a target.")
            return
        if self._drive_of(self.source_combo) and \
                self._drive_of(self.source_combo) == self._drive_of(self.target_combo):
            QMessageBox.warning(
                self, "Consolidate",
                "Source and target are on the same drive. Pick two different drives.",
            )
            return

        level = self.verify_combo.currentData()
        try:
            self.planner = ConsolidationPlanner(
                source, target,
                hash_cache=DbHashCache(
                    getattr(self.file_manager, "db", None),
                    ignore=self.ignore_cache_box.isChecked(),
                ),
                deep_verify=level in ("structural", "playthrough"),
                playthrough=level == "playthrough",
                aggressive_junk=self.aggressive_box.isChecked(),
                deletion_mode=self.delete_combo.currentData(),
                copy_missing=self.mode_combo.currentData() == "consolidate",
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Consolidate", str(exc))
            return

        self.tree.clear()
        self._conflict_combos.clear()
        self.plan = None
        self.apply_btn.setEnabled(False)
        self._set_running(True)
        self.summary_label.setText("Analyzing… this reads every file to hash and verify it.")
        self.log.clear()

        self._thread = QThread()
        self._worker = _PlanWorker(self.planner)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_plan_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.done.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._worker.done.connect(self._worker.deleteLater)
        self._worker.failed.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_plan_done(self, plan: Plan) -> None:
        self.plan = plan
        self._populate_tree(plan)
        self._refresh_summary()
        self.apply_btn.setEnabled(bool(plan.entries))
        self._set_running(False)
        self.progress.setFormat("Analysis complete")
        self.progress.setValue(100)
        cache = getattr(self.planner, "hash_cache", None)
        if cache is not None:
            self.log.append(
                f"\nHash cache: reused {cache.hits:,} file hash(es) from previous "
                f"runs / the scanner, read {cache.misses:,} fresh."
            )

    @staticmethod
    def _integrity_text(e: PlanEntry) -> str:
        word = e.integrity or "—"
        detail = e.integrity_detail or ""
        return f"{word} — {detail}" if detail else word

    def _populate_tree(self, plan: Plan) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        self._conflict_combos.clear()
        self._item_by_path.clear()

        by_action: Dict[str, List[PlanEntry]] = {}
        for e in plan.entries:
            by_action.setdefault(e.action, []).append(e)

        order = ["CONFLICT", "CORRUPT", "MOVE", "DUPLICATE_ELSEWHERE",
                 "ALREADY_THERE", "JUNK", "SKIP"]
        for action in order:
            entries = by_action.get(action)
            if not entries:
                continue
            total_bytes = sum(e.size for e in entries)
            parent = QTreeWidgetItem(self.tree)
            parent.setText(0, f"{_ACTION_LABELS.get(action, action)}  —  {len(entries):,} files")
            parent.setText(1, _h(total_bytes))
            parent.setText(2, action)
            parent.setFirstColumnSpanned(False)
            parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
            parent.setExpanded(action in ("CONFLICT", "CORRUPT"))

            # cap very large lists in the UI; the plan still holds them all
            shown = entries[:4000]
            for e in shown:
                child = QTreeWidgetItem(parent)
                child.setText(0, e.rel_path)
                child.setText(1, _h(e.size))
                child.setText(3, e.reason)
                child.setText(4, self._integrity_text(e))
                child.setData(0, Qt.ItemDataRole.UserRole, e)
                self._item_by_path[os.path.normcase(e.source_path)] = child
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked if e.selected else Qt.CheckState.Unchecked)
                if action == "CONFLICT":
                    combo = QComboBox()
                    for value, text in _CONFLICT_CHOICES:
                        combo.addItem(text, value)
                    idx = max(0, [v for v, _ in _CONFLICT_CHOICES].index(e.resolution)
                              if e.resolution in [v for v, _ in _CONFLICT_CHOICES] else 0)
                    combo.setCurrentIndex(idx)
                    combo.currentIndexChanged.connect(
                        lambda _i, ent=e, cb=combo: self._set_resolution(ent, cb)
                    )
                    self.tree.setItemWidget(child, 2, combo)
                else:
                    child.setText(2, "")
            if len(entries) > len(shown):
                more = QTreeWidgetItem(parent)
                more.setText(0, f"… and {len(entries) - len(shown):,} more (included in Apply)")
                more.setFlags(Qt.ItemFlag.ItemIsEnabled)

        self.tree.blockSignals(False)

    def _set_resolution(self, entry: PlanEntry, combo: QComboBox) -> None:
        entry.resolution = combo.currentData() or ""
        entry.selected = bool(entry.resolution)
        self._refresh_summary()

    def _on_current_changed(self, current: Optional[QTreeWidgetItem], _prev) -> None:
        entry = current.data(0, Qt.ItemDataRole.UserRole) if current else None
        if isinstance(entry, PlanEntry):
            self.preview.show_path(entry.source_path, entry.integrity, entry.integrity_detail)
        else:
            self.preview.show_path("")

    def _on_preview_verified(self, path: str, status: str, detail: str) -> None:
        item = self._item_by_path.get(os.path.normcase(path))
        if item is None:
            return
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(entry, PlanEntry):
            return
        entry.integrity = status
        entry.integrity_detail = detail
        self.tree.blockSignals(True)
        item.setText(4, self._integrity_text(entry))
        if status in ("corrupt", "unreadable"):
            entry.selected = False
            item.setCheckState(0, Qt.CheckState.Unchecked)
            entry.reason = f"failed manual verify: {detail}"
            item.setText(3, entry.reason)
        self.tree.blockSignals(False)
        self._refresh_summary()

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(entry, PlanEntry):
            entry.selected = item.checkState(0) == Qt.CheckState.Checked
            self._refresh_summary()

    def _refresh_summary(self) -> None:
        if not self.plan:
            return
        s = self.plan.summary()
        freed = self.plan.reclaimable_bytes()
        need = self.plan.target_needed_bytes()
        parts = []
        for action in ("MOVE", "ALREADY_THERE", "DUPLICATE_ELSEWHERE", "CONFLICT", "JUNK", "CORRUPT"):
            if action in s and s[action]["selected"]:
                parts.append(f"{action.replace('_', ' ').title()}: {s[action]['selected']:,}")
        tgt_free = self._target_free_bytes()
        room = "" if tgt_free is None else (
            f"  ·  target free: {_h(tgt_free)}"
            + ("  ⚠ not enough room" if need > tgt_free else "")
        )
        self.summary_label.setText(
            f"Selected — {', '.join(parts) or 'nothing'}.  "
            f"Frees ~{_h(freed)} on source, writes ~{_h(need)} to target.{room}"
        )

    def _target_free_bytes(self) -> Optional[int]:
        try:
            import shutil as _sh
            return _sh.disk_usage(self._combo_path(self.target_combo)).free
        except Exception:  # noqa: BLE001
            return None

    # ── apply ────────────────────────────────────────────────────────
    def apply_selected(self) -> None:
        if self._thread is not None or not self.plan or not self.planner:
            return
        selected = [e for e in self.plan.entries if e.selected and e.action != "SKIP"]
        if not selected:
            QMessageBox.information(self, "Consolidate", "Nothing is selected.")
            return
        need = self.plan.target_needed_bytes()
        free = self._target_free_bytes()
        if free is not None and need > free:
            QMessageBox.warning(
                self, "Not enough room",
                f"The selected plan writes {_h(need)} to the target but only "
                f"{_h(free)} is free.",
            )
            return

        moves = sum(1 for e in selected if e.action in ("MOVE", "CONFLICT"))
        dels = sum(1 for e in selected if e.action in
                   ("ALREADY_THERE", "DUPLICATE_ELSEWHERE", "JUNK"))
        mode = self.delete_combo.currentData()
        dest_note = {
            "recycle": "the Recycle Bin (space frees only once you empty it)",
            "permanent": "permanently deleted (frees space now, no undo)",
            "quarantine": "an __Aegis_Quarantine__ folder on the source drive",
        }[mode]
        confirm = QMessageBox.question(
            self, "Apply consolidation",
            f"This will copy ~{moves:,} file(s) to the target and remove "
            f"~{dels:,} file(s) from the source.\n\n"
            f"Removed originals go to: {dest_note}.\n"
            "Every removal happens only after the target copy is double-verified. "
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.planner.deletion_mode = mode
        self.planner.use_recycle_bin = mode == "recycle"
        self._set_running(True, analyzing=False)
        self.log.append("Applying…")

        self._thread = QThread()
        self._worker = _ExecWorker(self.planner, self.plan)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_exec_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.done.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._worker.done.connect(self._worker.deleteLater)
        self._worker.failed.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_exec_done(self, result: dict) -> None:
        self._set_running(False)
        self.progress.setFormat("Apply complete")
        self.progress.setValue(100)
        msg = (
            f"Moved {result['moved']:,} ({_h(result['moved_bytes'])}), "
            f"deleted {result['deleted']:,} ({_h(result['deleted_bytes'])}), "
            f"conflicts resolved {result['conflicts_resolved']:,}, "
            f"skipped {result['skipped']:,}."
        )
        self.log.append(msg)

        freed = result.get("source_freed", 0)
        space_note = result.get("space_note", "")
        self.log.append(
            f"Source free space: {_h(result.get('source_free_before', 0))} -> "
            f"{_h(result.get('source_free_after', 0))}  (+{_h(freed)})"
        )
        if space_note:
            self.log.append("NOTE: " + space_note)
            self._reclaim_row.setVisible(result.get("deletion_mode") == "recycle")

        if result["errors"]:
            self.log.append(f"\n{len(result['errors'])} problem(s):")
            for err in result["errors"][:40]:
                self.log.append(f"  {err}")
        self.log.append(f"\nJournal: {result['journal']}")
        self.summary_label.setText(msg + "  Re-run Analyze to see what's left.")
        self.tasks_changed.emit()
        extra = ""
        if result["errors"]:
            extra = f"\n\n{len(result['errors'])} problem(s) — see the log."
        elif space_note:
            extra = "\n\n" + space_note
        box = QMessageBox.warning if result["errors"] else QMessageBox.information
        box(self, "Consolidate", msg + extra)

    def _empty_recycle_bin(self) -> None:
        drive = self._drive_of(self.source_combo) or ""
        root = f"{drive}\\" if drive else None
        if QMessageBox.question(
            self, "Empty Recycle Bin",
            f"Permanently delete everything in the Recycle Bin"
            + (f" on {drive}" if drive else "")
            + "?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            import ctypes

            # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
            rc = ctypes.windll.shell32.SHEmptyRecycleBinW(None, root, 0x00000001 | 0x00000002 | 0x00000004)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Empty Recycle Bin", f"Could not empty it: {exc}")
            return
        if rc == 0:
            self.log.append("Recycle Bin emptied.")
            self._reclaim_row.setVisible(False)
            free = self._source_free_bytes()
            if free is not None:
                self.log.append(f"Source free space now: {_h(free)}")
        else:
            QMessageBox.warning(
                self, "Empty Recycle Bin",
                f"Windows returned code 0x{rc & 0xFFFFFFFF:08X}. "
                "It may already be empty, or another app is using it.",
            )

    def _source_free_bytes(self):
        try:
            import shutil as _sh
            return _sh.disk_usage(self._combo_path(self.source_combo)).free
        except Exception:  # noqa: BLE001
            return None

    # ── shared plumbing ─────────────────────────────────────────────
    def _on_progress(self, phase: str, done: int, total: int, message: str) -> None:
        if total > 0:
            self.progress.setMaximum(total)
            self.progress.setValue(min(done, total))
            self.progress.setFormat(f"{phase}: {done:,}/{total:,}")
        else:
            self.progress.setMaximum(0)  # busy indicator
            self.progress.setFormat(f"{phase}: {done:,}")
        if message and done % 2000 < 250:
            self.log.append(f"[{phase}] {message}")

    def _on_failed(self, message: str) -> None:
        self._set_running(False)
        self.progress.setFormat("Failed")
        self.progress.setMaximum(100)
        self.log.append(f"ERROR: {message}")
        QMessageBox.warning(self, "Consolidate", message)

    def _cleanup_thread(self) -> None:
        self._thread = None
        self._worker = None
        self.cancel_btn.setEnabled(False)

    def _stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self.progress.setFormat("Stopping…")
            self.log.append("Stop requested — finishing the current file…")

    def shutdown(self) -> None:
        stop_thread(getattr(self, "_thread", None), getattr(self, "_worker", None),
                    wait_ms=8000)
        preview = getattr(self, "preview", None)
        if preview is not None:
            if hasattr(preview, "stop"):
                preview.stop()
            stop_thread(getattr(preview, "_verify_thread", None),
                        getattr(preview, "_verify_worker", None))

    def _set_running(self, running: bool, analyzing: bool = True) -> None:
        self.analyze_btn.setEnabled(not running)
        self.refresh_btn.setEnabled(not running)
        self.apply_btn.setEnabled(not running and bool(self.plan))
        self.cancel_btn.setEnabled(running)
        self.source_combo.setEnabled(not running)
        self.target_combo.setEnabled(not running)
        if running:
            self.progress.setValue(0)
            self.progress.setFormat("Analyzing…" if analyzing else "Applying…")
        else:
            self._validate_pair()  # keep Analyze disabled if same-drive
