"""
ui/review_view.py
=================
"Review & Triage": load a flagged-file CSV, look at each file without leaving
the app, and mark it Delete / Keep / Unsure.

Built for working through an audit list where the filename is not enough to
judge by — a video has to be watched and a document has to be read before you
can say what it is. The preview is the same PreviewPanel the Consolidate tab
uses, so video plays inline with a seek bar, PDFs page, and .docx/.rtf bodies
are extracted and shown as text.

Decisions are keyed by file path, not by row, and are saved to
%LOCALAPPDATA%\\Aegis\\review\\decisions.json after every change. Re-running the
scan and loading a fresh CSV therefore keeps everything already judged. They
are kept out of the repo deliberately: the paths alone are sensitive.

Nothing is moved or deleted by marking. The two buttons that act on files are
explicit, threaded, confirmed, and report exactly what happened.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.util import human_bytes
from ui.preview_panel import PreviewPanel
from ui.thread_utils import stop_thread

STATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Aegis" / "review"
DECISIONS_PATH = STATE_DIR / "decisions.json"
STATE_PATH = STATE_DIR / "state.json"
EXCLUDED_PATH = STATE_DIR / "excluded.json"

DELETE, KEEP, UNSURE, UNDECIDED = "delete", "keep", "unsure", ""

_DECISION_LABEL = {
    DELETE: "Delete",
    KEEP: "Keep",
    UNSURE: "Unsure",
    UNDECIDED: "—",
}
_DECISION_COLOR = {
    DELETE: "#c62828",
    KEEP: "#2e7d32",
    UNSURE: "#b26a00",
    UNDECIDED: "#777777",
}

# Same zones the audit report groups by, so the two line up.
_ZONE_RULES = [
    ("Recycle bin", ("$recycle.bin",)),
    ("Phone backup", ("\\crossdevice\\", "\\phones\\", "iphone backup", "\\phone link\\")),
    ("College laptop", ("\\users\\yy", "yy0133ok_downloads", "\\cold storage\\school\\")),
    ("Other backup", ("\\cold storage\\", "\\c_drive_backups\\", "\\old_backups\\")),
    ("Game / app", ("steamapps", "steamlibrary", "xboxgames", "program files")),
    ("Live system", ("c:\\users\\",)),
]

COL_DECISION, COL_TIER, COL_ZONE, COL_SIZE, COL_MOD, COL_TYPE, COL_TERMS, COL_PATH = range(8)


def _zone_of(path: str) -> str:
    low = path.lower()
    for name, needles in _ZONE_RULES:
        if any(n in low for n in needles):
            return name
    return "Elsewhere"


class _Row(QTreeWidgetItem):
    """Sorts the size column numerically instead of as text."""

    def __lt__(self, other: QTreeWidgetItem) -> bool:  # type: ignore[override]
        col = self.treeWidget().sortColumn() if self.treeWidget() else 0
        if col == COL_SIZE:
            return self.data(COL_SIZE, Qt.ItemDataRole.UserRole + 1) < other.data(
                COL_SIZE, Qt.ItemDataRole.UserRole + 1
            )
        return super().__lt__(other)


class _ActionWorker(QObject):
    """Deletes or moves the marked files on a worker thread."""

    progress = pyqtSignal(int, int, str)
    done = pyqtSignal(dict)

    def __init__(self, paths: List[str], mode: str, dest: str = "") -> None:
        super().__init__()
        self.paths = paths
        self.mode = mode
        self.dest = dest
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _target_for(self, src: str) -> Path:
        """Mirror the original layout under the quarantine root, so a file can
        be put back where it came from if this turns out to be a mistake."""
        drive, rest = os.path.splitdrive(src)
        letter = drive.replace(":", "").strip("\\/") or "UNC"
        return Path(self.dest) / letter / rest.lstrip("\\/")

    def run(self) -> None:
        done_n = 0
        freed = 0
        missing: List[str] = []
        failed: List[str] = []
        acted: List[str] = []

        for i, src in enumerate(self.paths, 1):
            if self._stop:
                break
            self.progress.emit(i, len(self.paths), src)
            try:
                if not os.path.isfile(src):
                    missing.append(src)
                    continue
                size = os.path.getsize(src)
                if self.mode == "move":
                    tgt = self._target_for(src)
                    tgt.parent.mkdir(parents=True, exist_ok=True)
                    if tgt.exists():
                        stem, suf = tgt.stem, tgt.suffix
                        tgt = tgt.with_name(f"{stem}__{int(datetime.now().timestamp())}{suf}")
                    shutil.move(src, str(tgt))
                else:
                    os.remove(src)
                done_n += 1
                freed += size
                acted.append(src)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{src} — {type(exc).__name__}: {exc}")

        self.done.emit({
            "count": done_n, "freed": freed, "missing": missing,
            "failed": failed, "acted": acted, "mode": self.mode,
            "stopped": self._stop,
        })


class ReviewView(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: List[_Row] = []
        self._decisions: Dict[str, str] = {}
        self._excluded: List[str] = []   # normcase folder prefixes, parked out of sight
        self._csv_path = ""
        self._thread: Optional[QThread] = None
        self._worker: Optional[_ActionWorker] = None

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self._save_decisions)

        self._load_decisions()
        self._load_excluded()
        self._build_ui()
        self._restore_last_csv()

    # ── UI ─────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        # Tight margins on purpose: every pixel of chrome here is a pixel the
        # preview pane does not get, and a cramped video is the whole problem
        # this tab exists to avoid.
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(6)

        title = QLabel("Review & Triage")
        title.setStyleSheet("font-size: 22px; font-weight: 600;")
        root.addWidget(title)

        blurb = QLabel(
            "Preview each file, then mark it Delete, Keep or Unsure (D / K / U). "
            "Marking changes nothing on disk."
        )
        blurb.setWordWrap(True)
        blurb.setStyleSheet("color: #888;")
        root.addWidget(blurb)

        # ── source row ────────────────────────────────────────────────
        src = QHBoxLayout()
        load_btn = QPushButton("Load CSV…")
        load_btn.clicked.connect(self._pick_csv)
        src.addWidget(load_btn)

        self.csv_label = QLabel("No list loaded")
        self.csv_label.setStyleSheet("color: #666;")
        self.csv_label.setWordWrap(True)
        src.addWidget(self.csv_label, 1)

        export_btn = QPushButton("Export marked…")
        export_btn.clicked.connect(self._export_marked)
        src.addWidget(export_btn)
        root.addLayout(src)

        # ── filters ───────────────────────────────────────────────────
        filt = QGroupBox("Show")
        fl = QHBoxLayout(filt)

        self.tier_filter = QComboBox()
        self.tier_filter.addItems(["All tiers", "Tier A only", "Tier B only"])
        self.tier_filter.currentIndexChanged.connect(self._apply_filters)
        fl.addWidget(QLabel("Tier"))
        fl.addWidget(self.tier_filter)

        self.state_filter = QComboBox()
        self.state_filter.addItems(
            ["All", "Undecided", "Marked delete", "Marked keep", "Marked unsure"]
        )
        self.state_filter.currentIndexChanged.connect(self._apply_filters)
        fl.addWidget(QLabel("Decision"))
        fl.addWidget(self.state_filter)

        self.zone_filter = QComboBox()
        self.zone_filter.addItem("All zones")
        self.zone_filter.currentIndexChanged.connect(self._apply_filters)
        fl.addWidget(QLabel("Zone"))
        fl.addWidget(self.zone_filter)

        self.type_filter = QComboBox()
        self.type_filter.addItem("All types")
        self.type_filter.currentIndexChanged.connect(self._apply_filters)
        fl.addWidget(QLabel("Type"))
        fl.addWidget(self.type_filter)

        self.search = QLineEdit()
        self.search.setPlaceholderText("filter by path or matched term…")
        self.search.textChanged.connect(self._apply_filters)
        fl.addWidget(self.search, 1)

        self.hide_missing = QCheckBox("Hide files already gone")
        self.hide_missing.stateChanged.connect(self._apply_filters)
        fl.addWidget(self.hide_missing)

        self.show_parked = QCheckBox("Show parked folders")
        self.show_parked.setToolTip(
            "Parked folders are kept out of the list so you can work through "
            "the rest. Nothing about them is deleted or changed."
        )
        self.show_parked.stateChanged.connect(self._apply_filters)
        fl.addWidget(self.show_parked)
        root.addWidget(filt)

        self.counts = QLabel("")
        self.counts.setStyleSheet("font-size: 12px; color: #555;")
        root.addWidget(self.counts)

        # ── table + preview ───────────────────────────────────────────
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            ["Decision", "Tier", "Zone", "Size", "Modified", "Type", "Matched", "Path"]
        )
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.currentItemChanged.connect(self._on_current_changed)
        hdr = self.tree.header()
        for col, width in ((COL_DECISION, 82), (COL_TIER, 46), (COL_ZONE, 110),
                           (COL_SIZE, 80), (COL_MOD, 86), (COL_TYPE, 56),
                           (COL_TERMS, 170)):
            hdr.resizeSection(col, width)
        hdr.setSectionResizeMode(COL_PATH, QHeaderView.ResizeMode.Interactive)
        hdr.resizeSection(COL_PATH, 520)

        self.preview = PreviewPanel()

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.tree)
        split.addWidget(self.preview)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([760, 520])
        root.addWidget(split, 1)

        # ── decision + action row ─────────────────────────────────────
        act = QHBoxLayout()
        for label, decision, key in (
            ("Delete  (D)", DELETE, "D"),
            ("Keep  (K)", KEEP, "K"),
            ("Unsure  (U)", UNSURE, "U"),
            ("Clear  (0)", UNDECIDED, "0"),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, d=decision: self._set_decision(d))
            btn.setStyleSheet(f"color: {_DECISION_COLOR[decision]}; font-weight: 600;")
            act.addWidget(btn)
            QShortcut(QKeySequence(key), self, lambda d=decision: self._set_decision(d))

        self.park_btn = QPushButton("Park folder  (P)")
        self.park_btn.setToolTip(
            "Hide every row under the selected row's folder, so it stops "
            "appearing while you work through the rest. Reversible, and it "
            "changes nothing on disk."
        )
        self.park_btn.clicked.connect(self._park_selected)
        act.addWidget(self.park_btn)
        QShortcut(QKeySequence("P"), self, self._park_selected)

        self.unpark_btn = QPushButton("Unpark folder")
        self.unpark_btn.clicked.connect(self._unpark_selected)
        act.addWidget(self.unpark_btn)
        act.addStretch()

        self.quarantine_btn = QPushButton("Move marked to folder…")
        self.quarantine_btn.clicked.connect(lambda: self._run_action("move"))
        act.addWidget(self.quarantine_btn)

        self.delete_btn = QPushButton("Delete marked permanently…")
        self.delete_btn.setStyleSheet("color: #c62828; font-weight: 600;")
        self.delete_btn.clicked.connect(lambda: self._run_action("delete"))
        act.addWidget(self.delete_btn)
        root.addLayout(act)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-size: 12px; color: #888;")
        root.addWidget(self.status)

    # ── decisions persistence ──────────────────────────────────────────
    def _load_decisions(self) -> None:
        try:
            raw = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
            self._decisions = {k: v for k, v in raw.items() if isinstance(v, str)}
        except (OSError, json.JSONDecodeError, AttributeError):
            self._decisions = {}

    def _save_decisions(self) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = DECISIONS_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._decisions, indent=1), encoding="utf-8")
            tmp.replace(DECISIONS_PATH)
        except OSError as exc:
            self.status.setText(f"Could not save decisions: {exc}")

    # ── parked folders ─────────────────────────────────────────────────
    def _load_excluded(self) -> None:
        try:
            raw = json.loads(EXCLUDED_PATH.read_text(encoding="utf-8"))
            self._excluded = [os.path.normcase(p) for p in raw if isinstance(p, str)]
        except (OSError, json.JSONDecodeError, TypeError):
            self._excluded = []

    def _save_excluded(self) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = EXCLUDED_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(sorted(self._excluded), indent=1), encoding="utf-8")
            tmp.replace(EXCLUDED_PATH)
        except OSError as exc:
            self.status.setText(f"Could not save parked folders: {exc}")

    def _is_parked(self, path: str) -> bool:
        low = os.path.normcase(path)
        return any(low.startswith(pref) for pref in self._excluded)

    def park_folder(self, folder: str) -> None:
        """Park a folder prefix. Public so it can be driven from elsewhere."""
        pref = os.path.normcase(folder.rstrip("\\/")) + os.sep
        if pref not in self._excluded:
            self._excluded.append(pref)
            self._save_excluded()

    def _selected_folders(self) -> List[str]:
        items = [i for i in self.tree.selectedItems() if isinstance(i, _Row)]
        if not items:
            cur = self.tree.currentItem()
            if isinstance(cur, _Row):
                items = [cur]
        return sorted({str(Path(i.data(COL_PATH, Qt.ItemDataRole.UserRole)).parent)
                       for i in items})

    def _park_selected(self) -> None:
        folders = self._selected_folders()
        if not folders:
            self.status.setText("Select a row first — parking works by its folder.")
            return
        for f in folders:
            self.park_folder(f)
        self._apply_filters()
        self.status.setText(
            f"Parked {len(folders)} folder(s). Tick “Show parked folders” to bring "
            "them back; nothing on disk changed."
        )

    def _unpark_selected(self) -> None:
        folders = [os.path.normcase(f.rstrip("\\/")) + os.sep
                   for f in self._selected_folders()]
        if not folders:
            self.status.setText("Select a row in a parked folder to unpark it.")
            return
        before = len(self._excluded)
        # Drop any prefix that covers, or is covered by, a selected folder.
        self._excluded = [
            p for p in self._excluded
            if not any(p.startswith(f) or f.startswith(p) for f in folders)
        ]
        self._save_excluded()
        self._apply_filters()
        self.status.setText(f"Unparked {before - len(self._excluded)} folder prefix(es).")

    def _restore_last_csv(self) -> None:
        try:
            last = json.loads(STATE_PATH.read_text(encoding="utf-8")).get("last_csv", "")
        except (OSError, json.JSONDecodeError, AttributeError):
            last = ""
        if last and os.path.isfile(last):
            self.load_csv(last)

    def _remember_csv(self, path: str) -> None:
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps({"last_csv": path}), encoding="utf-8")
        except OSError:
            pass

    # ── loading ────────────────────────────────────────────────────────
    def _pick_csv(self) -> None:
        start = str(Path(self._csv_path).parent) if self._csv_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open flagged-file list", start, "CSV files (*.csv);;All files (*)"
        )
        if path:
            self.load_csv(path)

    def load_csv(self, path: str) -> None:
        try:
            with open(path, encoding="utf-8-sig", newline="") as fh:
                records = list(csv.DictReader(fh))
        except (OSError, csv.Error, UnicodeDecodeError) as exc:
            QMessageBox.warning(self, "Review & Triage", f"Could not read that CSV:\n{exc}")
            return
        if not records:
            QMessageBox.warning(self, "Review & Triage", "That CSV has no rows.")
            return

        cols = {c.lower() for c in records[0]}
        if "path" not in cols:
            QMessageBox.warning(
                self, "Review & Triage",
                "That CSV has no 'path' column, so there is nothing to preview.\n\n"
                f"Columns found: {', '.join(sorted(cols))}",
            )
            return

        self._csv_path = path
        self.csv_label.setText(f"{path}   ({len(records)} rows)")
        self._remember_csv(path)
        self._populate(records)

    def _populate(self, records: List[dict]) -> None:
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        self._rows = []
        zones, types = set(), set()

        for rec in records:
            get = {k.lower(): (v or "") for k, v in rec.items()}.get
            fpath = get("path", "").strip()
            if not fpath:
                continue
            try:
                size = int(get("size_bytes", "0") or 0)
            except ValueError:
                size = 0
            ext = (get("ext", "") or Path(fpath).suffix).lower()
            terms = "; ".join(t for t in (get("strong", ""), get("weak", "")) if t)
            zone = _zone_of(fpath)
            zones.add(zone)
            types.add(ext)

            item = _Row([
                "", get("tier", ""), zone, human_bytes(size),
                get("modified", ""), ext, terms, fpath,
            ])
            item.setData(COL_SIZE, Qt.ItemDataRole.UserRole + 1, size)
            item.setData(COL_PATH, Qt.ItemDataRole.UserRole, fpath)
            item.setToolTip(COL_PATH, fpath)
            item.setToolTip(COL_TERMS, terms)
            self.tree.addTopLevelItem(item)
            self._rows.append(item)
            self._paint(item)

        self.tree.setSortingEnabled(True)
        self.tree.sortItems(COL_SIZE, Qt.SortOrder.DescendingOrder)

        for combo, values, first in ((self.zone_filter, zones, "All zones"),
                                     (self.type_filter, types, "All types")):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(first)
            combo.addItems(sorted(v for v in values if v))
            combo.blockSignals(False)

        self._apply_filters()
        # Deliberately no auto-selection. Selecting a row starts the preview,
        # and this list is of material the user is trying to get rid of --
        # opening the tab should not put any of it on screen or start playing
        # audio. The first row gets previewed when they ask for it.
        self.preview.show_path("")
        self.status.setText(
            f"{len(self._rows)} rows loaded. Click a row to preview it; "
            "D / K / U to mark it."
        )

    # ── painting / counts ──────────────────────────────────────────────
    def _paint(self, item: _Row) -> None:
        path = item.data(COL_PATH, Qt.ItemDataRole.UserRole)
        decision = self._decisions.get(os.path.normcase(path), UNDECIDED)
        item.setText(COL_DECISION, _DECISION_LABEL[decision])
        item.setForeground(COL_DECISION, QBrush(QColor(_DECISION_COLOR[decision])))
        gone = not os.path.isfile(path)
        item.setForeground(COL_PATH, QBrush(QColor("#aaaaaa" if gone else "#222222")))
        if gone:
            item.setToolTip(COL_PATH, f"{path}\n\n(no longer on disk)")

    def _update_counts(self) -> None:
        total = len(self._rows)
        shown = sum(1 for r in self._rows if not r.isHidden())
        tally = {DELETE: [0, 0], KEEP: [0, 0], UNSURE: [0, 0], UNDECIDED: [0, 0]}
        for r in self._rows:
            d = self._decisions.get(
                os.path.normcase(r.data(COL_PATH, Qt.ItemDataRole.UserRole)), UNDECIDED)
            tally[d][0] += 1
            tally[d][1] += r.data(COL_SIZE, Qt.ItemDataRole.UserRole + 1) or 0
        decided = total - tally[UNDECIDED][0]
        parked = sum(1 for r in self._rows
                     if self._is_parked(r.data(COL_PATH, Qt.ItemDataRole.UserRole)))
        parked_note = (f" &nbsp;·&nbsp; <span style='color:#777'>parked {parked}</span>"
                       if parked else "")
        self.counts.setText(
            f"<b>{decided}</b> of <b>{total}</b> decided &nbsp;·&nbsp; showing {shown}"
            f" &nbsp;·&nbsp; <span style='color:{_DECISION_COLOR[DELETE]}'>delete "
            f"{tally[DELETE][0]} ({human_bytes(tally[DELETE][1])})</span>"
            f" &nbsp;·&nbsp; <span style='color:{_DECISION_COLOR[KEEP]}'>keep "
            f"{tally[KEEP][0]}</span>"
            f" &nbsp;·&nbsp; <span style='color:{_DECISION_COLOR[UNSURE]}'>unsure "
            f"{tally[UNSURE][0]}</span>"
            f" &nbsp;·&nbsp; undecided {tally[UNDECIDED][0]}" + parked_note
        )

    # ── filtering ──────────────────────────────────────────────────────
    def _apply_filters(self) -> None:
        tier_mode = self.tier_filter.currentIndex()
        state_mode = self.state_filter.currentText()
        zone = self.zone_filter.currentText()
        ftype = self.type_filter.currentText()
        needle = self.search.text().strip().lower()
        hide_gone = self.hide_missing.isChecked()
        show_parked = self.show_parked.isChecked()

        want_state = {
            "Undecided": UNDECIDED, "Marked delete": DELETE,
            "Marked keep": KEEP, "Marked unsure": UNSURE,
        }.get(state_mode)

        for r in self._rows:
            path = r.data(COL_PATH, Qt.ItemDataRole.UserRole)
            decision = self._decisions.get(os.path.normcase(path), UNDECIDED)
            ok = True
            if self._is_parked(path) and not show_parked:
                r.setHidden(True)
                continue
            if tier_mode == 1 and r.text(COL_TIER) != "A":
                ok = False
            elif tier_mode == 2 and r.text(COL_TIER) != "B":
                ok = False
            if ok and want_state is not None and decision != want_state:
                ok = False
            if ok and zone not in ("All zones", "") and r.text(COL_ZONE) != zone:
                ok = False
            if ok and ftype not in ("All types", "") and r.text(COL_TYPE) != ftype:
                ok = False
            if ok and needle and needle not in (path + " " + r.text(COL_TERMS)).lower():
                ok = False
            if ok and hide_gone and not os.path.isfile(path):
                ok = False
            r.setHidden(not ok)

        self._update_counts()

    # ── selection / preview ────────────────────────────────────────────
    def _on_current_changed(self, current: Optional[QTreeWidgetItem], _prev) -> None:
        if current is None:
            self.preview.show_path("")
            return
        path = current.data(COL_PATH, Qt.ItemDataRole.UserRole)
        if path and os.path.isfile(path):
            self.preview.show_path(path)
        else:
            self.preview.show_path(path or "")
            self.status.setText(f"Not on disk any more: {path}")

    # ── decisions ──────────────────────────────────────────────────────
    def _set_decision(self, decision: str) -> None:
        items = [i for i in self.tree.selectedItems() if isinstance(i, _Row)]
        if not items:
            cur = self.tree.currentItem()
            if isinstance(cur, _Row):
                items = [cur]
        if not items:
            return

        for item in items:
            key = os.path.normcase(item.data(COL_PATH, Qt.ItemDataRole.UserRole))
            if decision == UNDECIDED:
                self._decisions.pop(key, None)
            else:
                self._decisions[key] = decision
            self._paint(item)

        self._save_timer.start()
        self._update_counts()
        if len(items) == 1:
            self._advance()
        # Re-filter only when the current view is decision-based, otherwise the
        # row the user just judged would vanish from under the cursor.
        if self.state_filter.currentIndex() != 0:
            self._apply_filters()

    def _advance(self) -> None:
        cur = self.tree.currentItem()
        if cur is None:
            return
        idx = self.tree.indexOfTopLevelItem(cur)
        for i in range(idx + 1, self.tree.topLevelItemCount()):
            nxt = self.tree.topLevelItem(i)
            if not nxt.isHidden():
                self.tree.setCurrentItem(nxt)
                return

    # ── export ─────────────────────────────────────────────────────────
    def _marked_paths(self) -> List[str]:
        """Paths marked Delete, excluding anything in a parked folder.

        Parking means "leave this alone for now", so a parked folder is never
        acted on even if rows inside it were marked earlier. Unpark it first.
        """
        out = []
        for r in self._rows:
            path = r.data(COL_PATH, Qt.ItemDataRole.UserRole)
            if self._decisions.get(os.path.normcase(path)) != DELETE:
                continue
            if self._is_parked(path):
                continue
            out.append(path)
        return out

    def _export_marked(self) -> None:
        if not self._rows:
            QMessageBox.information(self, "Review & Triage", "Load a list first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export decisions", "review_decisions.csv", "CSV files (*.csv)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["decision", "tier", "zone", "size_bytes", "modified",
                            "ext", "matched", "path", "still_on_disk"])
                for r in self._rows:
                    p = r.data(COL_PATH, Qt.ItemDataRole.UserRole)
                    w.writerow([
                        self._decisions.get(os.path.normcase(p), UNDECIDED),
                        r.text(COL_TIER), r.text(COL_ZONE),
                        r.data(COL_SIZE, Qt.ItemDataRole.UserRole + 1),
                        r.text(COL_MOD), r.text(COL_TYPE), r.text(COL_TERMS),
                        p, "yes" if os.path.isfile(p) else "no",
                    ])
        except OSError as exc:
            QMessageBox.warning(self, "Review & Triage", f"Could not write:\n{exc}")
            return
        self.status.setText(f"Exported {len(self._rows)} rows to {path}")

    # ── acting on the marked files ─────────────────────────────────────
    def _run_action(self, mode: str) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "Review & Triage", "Already working — let it finish.")
            return

        paths = self._marked_paths()
        live = [p for p in paths if os.path.isfile(p)]
        if not live:
            QMessageBox.information(
                self, "Review & Triage",
                "Nothing is marked Delete, or the marked files are already gone.",
            )
            return
        total = sum(os.path.getsize(p) for p in live)

        dest = ""
        if mode == "move":
            dest = QFileDialog.getExistingDirectory(self, "Move marked files into…")
            if not dest:
                return
            question = (
                f"Move {len(live)} file(s), {human_bytes(total)}, into:\n{dest}\n\n"
                "The original folder layout is recreated underneath, so anything "
                "moved by mistake can be put back.\n\nProceed?"
            )
        else:
            question = (
                f"Permanently delete {len(live)} file(s), {human_bytes(total)}?\n\n"
                "This does not use the Recycle Bin. It cannot be undone.\n\n"
                "If you are not certain, use “Move marked to folder…” instead."
            )

        box = QMessageBox(self)
        box.setWindowTitle("Confirm")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(question)
        box.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        self._set_busy(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, len(live))
        self.progress.setValue(0)

        self._thread = QThread()
        self._worker = _ActionWorker(live, mode, dest)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_action_progress)
        self._worker.done.connect(self._on_action_done)
        self._worker.done.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()

    def _on_action_progress(self, i: int, n: int, path: str) -> None:
        self.progress.setValue(i)
        self.progress.setFormat(f"%v / %m — {Path(path).name}")

    def _on_action_done(self, result: dict) -> None:
        self.progress.setVisible(False)
        self._set_busy(False)

        verb = "Moved" if result["mode"] == "move" else "Deleted"
        bits = [f"{verb} {result['count']} file(s), {human_bytes(result['freed'])} freed."]
        if result["missing"]:
            bits.append(f"{len(result['missing'])} were already gone.")
        if result["failed"]:
            bits.append(f"{len(result['failed'])} failed.")
        if result["stopped"]:
            bits.append("Stopped early.")
        self.status.setText("  ".join(bits))

        for r in self._rows:
            self._paint(r)
        self._apply_filters()
        self._on_current_changed(self.tree.currentItem(), None)

        if result["failed"]:
            QMessageBox.warning(
                self, "Some files could not be handled",
                "\n".join(result["failed"][:15])
                + ("" if len(result["failed"]) <= 15
                   else f"\n… and {len(result['failed']) - 15} more"),
            )

    def _set_busy(self, busy: bool) -> None:
        self.delete_btn.setEnabled(not busy)
        self.quarantine_btn.setEnabled(not busy)
        self.tree.setEnabled(not busy)

    def _cleanup_thread(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._thread = None
        self._worker = None

    # ── teardown ───────────────────────────────────────────────────────
    def shutdown(self) -> None:
        if self._save_timer.isActive():
            self._save_timer.stop()
        self._save_decisions()
        stop_thread(self._thread, self._worker, wait_ms=8000)
        preview = getattr(self, "preview", None)
        if preview is not None:
            if hasattr(preview, "stop"):
                preview.stop()
            stop_thread(getattr(preview, "_verify_thread", None),
                        getattr(preview, "_verify_worker", None))
