"""
ui/preview_panel.py
===================
Right-hand preview for the Consolidate view. Shows the selected file so you can
eyeball it before it moves or gets deleted:

  * images          -> rendered inline, full decode
  * video / audio    -> embedded player with a "play through & verify" pass that
                        decodes every packet (uses Qt's bundled ffmpeg backend,
                        no external ffmpeg needed)
  * PDF              -> paged viewer (QtPdf)
  * text / code      -> first chunk shown
  * anything else     -> "Open in default app"

"Verify (full decode)" runs core.integrity.verify_playthrough on a worker
thread for non-media types and reports back.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QSize, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.doc_text import DOC_TEXT_EXTS, extract_text
from core.integrity import _AUD_EXTS, _IMG_EXTS, _TEXT_EXTS, _VID_EXTS, verify_playthrough

# Optional Qt modules — present in the standard PyQt6 wheel, but degrade quietly.
try:
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PyQt6.QtMultimediaWidgets import QVideoWidget

    _HAVE_MEDIA = True
except Exception:  # noqa: BLE001
    _HAVE_MEDIA = False

try:
    from PyQt6.QtPdf import QPdfDocument
    from PyQt6.QtPdfWidgets import QPdfView

    _HAVE_PDF = True
except Exception:  # noqa: BLE001
    _HAVE_PDF = False


_BADGE = {
    "ok": ("#2e7d32", "verified"),
    "unverified": ("#b26a00", "not fully checked"),
    "corrupt": ("#c62828", "corrupt"),
    "unreadable": ("#c62828", "unreadable"),
    "empty": ("#666", "empty"),
    "": ("#666", "not checked"),
}


def _fmt_time(ms: int) -> str:
    s = max(0, ms // 1000)
    return f"{s // 60}:{s % 60:02d}"


class _VerifyWorker(QObject):
    done = pyqtSignal(str, str)

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

    def run(self) -> None:
        try:
            status, detail = verify_playthrough(self.path)
        except Exception as exc:  # noqa: BLE001
            status, detail = "corrupt", f"{type(exc).__name__}: {exc}"
        self.done.emit(status, detail)


class PreviewPanel(QWidget):
    """Set the current file with show_path(); emits verified(path, status, detail)."""

    verified = pyqtSignal(str, str, str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._path = ""
        self._orig_pixmap: Optional[QPixmap] = None   # unscaled, for re-fitting
        self._verify_thread: Optional[QThread] = None
        self._verify_worker: Optional[_VerifyWorker] = None
        self._media_verify = False
        self._last_pos = -1
        self._stall_ticks = 0
        self._elapsed = 0

        self._build_ui()

    # ── construction ───────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        self.name_label = QLabel("No file selected")
        self.name_label.setStyleSheet("font-weight: 600;")
        self.name_label.setWordWrap(True)
        root.addWidget(self.name_label)

        self.badge = QLabel()
        self.badge.setWordWrap(True)
        self.badge.setStyleSheet("font-size: 12px;")
        root.addWidget(self.badge)

        self.stack = QStackedWidget()
        self.stack.setMinimumHeight(200)
        root.addWidget(self.stack, 1)

        # 0 — fallback / message
        self.msg_page = QLabel("Select a file to preview it.")
        self.msg_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.msg_page.setWordWrap(True)
        self.msg_page.setStyleSheet("color: #888;")
        self.stack.addWidget(self.msg_page)

        # 1 — image
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_scroll = QScrollArea()
        self.image_scroll.setWidgetResizable(True)
        self.image_scroll.setWidget(self.image_label)
        # The image is always scaled to fit, so scrollbars would never be
        # needed -- and leaving them enabled causes a feedback loop where a bar
        # appears, shrinks the viewport, and triggers another re-fit.
        self.image_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.image_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stack.addWidget(self.image_scroll)

        # 2 — media
        self.stack.addWidget(self._build_media_page())

        # 3 — pdf
        if _HAVE_PDF:
            self.pdf_doc = QPdfDocument(self)
            self.pdf_view = QPdfView(self)
            self.pdf_view.setDocument(self.pdf_doc)
            try:
                self.pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
            except Exception:  # noqa: BLE001
                pass
            self.stack.addWidget(self.pdf_view)
        else:
            self.stack.addWidget(QLabel("QtPdf not available."))

        # 4 — text
        self.text_view = QPlainTextEdit()
        self.text_view.setReadOnly(True)
        self.text_view.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")
        self.stack.addWidget(self.text_view)

        # ── controls row ───────────────────────────────────────────────
        row = QHBoxLayout()
        self.open_btn = QPushButton("Open in default app")
        self.open_btn.clicked.connect(self._open_external)
        row.addWidget(self.open_btn)

        self.verify_btn = QPushButton("Verify (full decode)")
        self.verify_btn.clicked.connect(self._verify_now)
        row.addWidget(self.verify_btn)
        row.addStretch()
        root.addLayout(row)

        self.verify_status = QLabel("")
        self.verify_status.setWordWrap(True)
        self.verify_status.setStyleSheet("font-size: 12px; color: #888;")
        root.addWidget(self.verify_status)

        self.verify_bar = QProgressBar()
        self.verify_bar.setVisible(False)
        root.addWidget(self.verify_bar)

        self._reset_state()

    def _build_media_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)

        if not _HAVE_MEDIA:
            lay.addWidget(QLabel("QtMultimedia not available — use Open in default app."))
            return page

        self.video = QVideoWidget()
        self.video.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # Letterbox inside whatever room the pane has rather than filling and
        # cropping, and allow it to shrink well below a 16:9 frame so a short
        # pane squeezes the picture instead of cutting it off.
        try:
            self.video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        except Exception:  # noqa: BLE001 - older Qt without the setter
            pass
        self.video.setMinimumHeight(120)
        self.video.setStyleSheet("background: #000;")
        lay.addWidget(self.video, 1)

        self.player = QMediaPlayer()
        self.audio_out = QAudioOutput()
        self.player.setAudioOutput(self.audio_out)
        self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(self._on_media_error)

        ctl = QHBoxLayout()
        self.play_btn = QPushButton("Play")
        self.play_btn.clicked.connect(self._toggle_play)
        ctl.addWidget(self.play_btn)

        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 0)
        self.seek.sliderMoved.connect(self.player.setPosition)
        ctl.addWidget(self.seek, 1)

        self.time_label = QLabel("0:00 / 0:00")
        ctl.addWidget(self.time_label)

        self.playthrough_btn = QPushButton("▶ Play through & verify")
        self.playthrough_btn.clicked.connect(self._verify_media_playthrough)
        ctl.addWidget(self.playthrough_btn)
        lay.addLayout(ctl)

        self._stall_timer = QTimer(self)
        self._stall_timer.setInterval(1000)
        self._stall_timer.timeout.connect(self._media_verify_tick)
        return page

    # ── public API ─────────────────────────────────────────────────────
    def show_path(self, path: str, integrity: str = "", detail: str = "") -> None:
        self._stop_media()
        self._path = path or ""
        if not path or not os.path.isfile(path):
            self.name_label.setText("No file selected" if not path else f"Missing: {path}")
            self.badge.clear()
            self.stack.setCurrentWidget(self.msg_page)
            self.msg_page.setText("Select a file to preview it.")
            self._enable_controls(False)
            return

        self.name_label.setText(path)
        self._set_badge(integrity, detail)
        self.verify_status.clear()
        self._enable_controls(True)

        ext = Path(path).suffix.lower()
        if ext in _IMG_EXTS:
            self._show_image(path)
        elif _HAVE_MEDIA and (ext in _VID_EXTS or ext in _AUD_EXTS):
            self._show_media(path, video=ext in _VID_EXTS)
        elif ext == ".pdf" and _HAVE_PDF:
            self._show_pdf(path)
        elif ext in _TEXT_EXTS:
            self._show_text(path)
        elif ext in DOC_TEXT_EXTS:
            self._show_document(path)
        else:
            self.stack.setCurrentWidget(self.msg_page)
            self.msg_page.setText(
                f"No inline preview for {ext or 'this type'}.\n"
                "Use “Open in default app”, or “Verify (full decode)” to check it."
            )

    # ── per-type loaders ──────────────────────────────────────────────
    def _show_image(self, path: str) -> None:
        pix = QPixmap(path)
        if pix.isNull():
            pix = self._pil_pixmap(path)  # HEIC/AVIF/etc via Pillow(+pillow-heif)
        if pix is None or pix.isNull():
            self._orig_pixmap = None
            ext = Path(path).suffix.lower()
            self.stack.setCurrentWidget(self.msg_page)
            self.msg_page.setText(
                f"No built-in preview for {ext or 'this image'} on this system.\n"
                "Use “Open in default app”. For inline HEIC/AVIF preview, "
                "install pillow-heif (pip install pillow pillow-heif).\n\n"
                "This does NOT mean the file is corrupt — run "
                "“Verify (full decode)” to check it."
            )
            return
        self._orig_pixmap = pix
        self.stack.setCurrentWidget(self.image_scroll)
        self._fit_image()

    def _fit_image(self) -> None:
        """Scale the current image down to sit wholly inside the pane.

        Fits both dimensions, not just width -- scaling to width alone left
        anything portrait-shaped running off the bottom. Never scales up, so a
        small image is shown at its true size rather than interpolated.
        """
        pix = self._orig_pixmap
        if pix is None or pix.isNull():
            return
        vp = self.image_scroll.viewport().size()
        avail = QSize(max(vp.width() - 4, 64), max(vp.height() - 4, 64))
        if pix.width() > avail.width() or pix.height() > avail.height():
            pix = pix.scaled(avail, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        self.image_label.setPixmap(pix)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        # Re-fit as the splitter or window changes, so the image tracks the
        # pane instead of being sized once when it was loaded.
        if self.stack.currentWidget() is self.image_scroll:
            self._fit_image()

    @staticmethod
    def _pil_pixmap(path: str) -> Optional[QPixmap]:
        try:
            from PIL import Image  # type: ignore
        except ImportError:
            return None
        try:
            from pillow_heif import register_heif_opener  # type: ignore

            register_heif_opener()
        except Exception:  # noqa: BLE001
            pass
        try:
            from PyQt6.QtGui import QImage

            with Image.open(path) as im:
                im = im.convert("RGBA")
                buf = im.tobytes("raw", "RGBA")
                qimg = QImage(buf, im.width, im.height, QImage.Format.Format_RGBA8888)
                return QPixmap.fromImage(qimg.copy())
        except Exception:  # noqa: BLE001
            return None

    def _show_media(self, path: str, video: bool) -> None:
        self.video.setVisible(video)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.play_btn.setText("Play")
        self.stack.setCurrentIndex(2)

    def _show_pdf(self, path: str) -> None:
        self.pdf_doc.load(path)
        self.stack.setCurrentWidget(self.pdf_view)

    def _show_text(self, path: str) -> None:
        try:
            data = Path(path).read_bytes()[:200_000]
            text = data.decode("utf-8", errors="replace")
        except OSError as exc:
            text = f"(could not read: {exc})"
        if os.path.getsize(path) > 200_000:
            text += "\n\n… (truncated preview) …"
        self.text_view.setPlainText(text)
        self.stack.setCurrentWidget(self.text_view)

    def _show_document(self, path: str) -> None:
        """.docx / .rtf / .doc / .odt / .pptx — extracted body text."""
        text, note = extract_text(path)
        if not text:
            ext = Path(path).suffix.lower()
            self.stack.setCurrentWidget(self.msg_page)
            self.msg_page.setText(
                f"No text could be read out of this {ext} file"
                + (f":\n{note}" if note else ".")
                + "\n\nIt may hold only images, or be corrupt. "
                "Use “Open in default app”, or “Verify (full decode)”."
            )
            return
        if note:
            text = f"[{note}]\n\n{text}"
        self.text_view.setPlainText(text)
        self.stack.setCurrentWidget(self.text_view)

    # ── media controls ────────────────────────────────────────────────
    def _toggle_play(self) -> None:
        if not _HAVE_MEDIA:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play_btn.setText("Play")
        else:
            self.player.play()
            self.play_btn.setText("Pause")

    def _on_position(self, pos: int) -> None:
        if not self.seek.isSliderDown():
            self.seek.setValue(pos)
        self.time_label.setText(f"{_fmt_time(pos)} / {_fmt_time(self.player.duration())}")

    def _on_duration(self, dur: int) -> None:
        self.seek.setRange(0, dur)

    def _on_media_status(self, status) -> None:
        if not self._media_verify:
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._finish_media_verify("ok", "played to the end — every packet decoded")
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._finish_media_verify("corrupt", "player rejected the file as invalid media")

    def _on_media_error(self, error, msg: str) -> None:
        if _HAVE_MEDIA and error != QMediaPlayer.Error.NoError:
            if self._media_verify:
                self._finish_media_verify("corrupt", f"decode error: {msg}")
            else:
                self.verify_status.setText(f"Player error: {msg}")

    def _verify_media_playthrough(self) -> None:
        if not _HAVE_MEDIA or not self._path:
            return
        self._media_verify = True
        self._last_pos = -1
        self._stall_ticks = 0
        self._elapsed = 0
        self.audio_out.setMuted(True)
        self.player.setPlaybackRate(4.0)
        self.player.setPosition(0)
        self.player.play()
        self.play_btn.setText("Pause")
        self.playthrough_btn.setEnabled(False)
        self.verify_bar.setVisible(True)
        self.verify_bar.setRange(0, 0)
        self.verify_status.setText("Decoding every frame at 4× …")
        self._stall_timer.start()

    def _media_verify_tick(self) -> None:
        self._elapsed += 1
        pos = self.player.position()
        if pos <= self._last_pos:
            self._stall_ticks += 1
        else:
            self._stall_ticks = 0
            self._last_pos = pos
        if self._stall_ticks >= 30:
            self._finish_media_verify(
                "corrupt", f"playback stalled near {_fmt_time(pos)} — likely corrupt"
            )
        elif self._elapsed > 30 * 60:
            self._finish_media_verify("unverified", "gave up after 30 minutes of decoding")

    def _finish_media_verify(self, status: str, detail: str) -> None:
        self._media_verify = False
        self._stall_timer.stop()
        self.player.pause()
        self.player.setPlaybackRate(1.0)
        self.audio_out.setMuted(False)
        self.play_btn.setText("Play")
        self.playthrough_btn.setEnabled(True)
        self.verify_bar.setVisible(False)
        self._set_badge(status, detail)
        self.verify_status.setText(f"{status.upper()} — {detail}")
        if self._path:
            self.verified.emit(self._path, status, detail)

    # ── non-media verify (worker thread) ─────────────────────────────
    def _verify_now(self) -> None:
        if not self._path or self._verify_thread is not None:
            return
        ext = Path(self._path).suffix.lower()
        if _HAVE_MEDIA and (ext in _VID_EXTS or ext in _AUD_EXTS):
            self._verify_media_playthrough()
            return

        self.verify_btn.setEnabled(False)
        self.verify_bar.setVisible(True)
        self.verify_bar.setRange(0, 0)
        self.verify_status.setText("Fully decoding …")

        self._verify_thread = QThread()
        self._verify_worker = _VerifyWorker(self._path)
        self._verify_worker.moveToThread(self._verify_thread)
        self._verify_thread.started.connect(self._verify_worker.run)
        self._verify_worker.done.connect(self._on_verify_done)
        self._verify_worker.done.connect(self._verify_thread.quit)
        self._verify_worker.done.connect(self._verify_worker.deleteLater)
        self._verify_thread.finished.connect(self._cleanup_verify_thread)
        self._verify_thread.finished.connect(self._verify_thread.deleteLater)
        self._verify_thread.start()

    def _on_verify_done(self, status: str, detail: str) -> None:
        self.verify_bar.setVisible(False)
        self.verify_btn.setEnabled(True)
        self._set_badge(status, detail)
        self.verify_status.setText(f"{status.upper()} — {detail}")
        if self._path:
            self.verified.emit(self._path, status, detail)

    def _cleanup_verify_thread(self) -> None:
        self._verify_thread = None
        self._verify_worker = None

    # ── misc ──────────────────────────────────────────────────────────
    def _open_external(self) -> None:
        if self._path and os.path.isfile(self._path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._path))

    def _set_badge(self, integrity: str, detail: str) -> None:
        color, word = _BADGE.get(integrity or "", _BADGE[""])
        text = f"<span style='color:{color}'>&#9679; {word}</span>"
        if detail:
            text += f" <span style='color:#888'>— {detail}</span>"
        self.badge.setText(text)

    def _enable_controls(self, on: bool) -> None:
        self.open_btn.setEnabled(on)
        self.verify_btn.setEnabled(on)

    def _reset_state(self) -> None:
        self.stack.setCurrentWidget(self.msg_page)
        self._enable_controls(False)

    def _stop_media(self) -> None:
        if _HAVE_MEDIA and getattr(self, "player", None) is not None:
            if self._media_verify:
                self._media_verify = False
                self._stall_timer.stop()
                self.player.setPlaybackRate(1.0)
                self.audio_out.setMuted(False)
                self.playthrough_btn.setEnabled(True)
                self.verify_bar.setVisible(False)
            self.player.stop()
            self.player.setSource(QUrl())

    def stop(self) -> None:
        self._stop_media()
