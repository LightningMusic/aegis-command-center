import os
import sys
from pathlib import Path


os.environ["QT_LOGGING_RULES"] = "*.warning=false"

BASE_DIR = Path(__file__).resolve().parent
THEME_PATH = BASE_DIR / "assets" / "theme.qss"


def _bootstrap_local_venv() -> None:
    venv_dir = BASE_DIR / ".venv"
    venv_python = venv_dir / "Scripts" / "python.exe"
    if venv_dir.exists() and venv_python.exists():
        current_prefix = Path(sys.prefix).resolve()
        target_prefix = venv_dir.resolve()
        if current_prefix != target_prefix:
            import subprocess
            sys.exit(subprocess.call([str(venv_python)] + sys.argv))


_bootstrap_local_venv()

from core.database import Database
from modules.task_manager import TaskManager


def _load_stylesheet(app) -> None:
    if THEME_PATH.exists():
        app.setStyleSheet(THEME_PATH.read_text(encoding="utf-8"))


def _app_icon():
    """A simple painted 'A' shield — no icon file needed."""
    from PyQt6.QtCore import QRectF, Qt
    from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

    pm = QPixmap(256, 256)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#1f2a35"))
    p.drawRoundedRect(QRectF(14, 14, 228, 228), 52, 52)
    p.setBrush(QColor("#4f8edc"))
    p.drawRoundedRect(QRectF(42, 42, 172, 172), 38, 38)
    p.setPen(QColor("#ffffff"))
    p.setFont(QFont("Segoe UI", 118, QFont.Weight.Bold))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "A")
    p.end()
    return QIcon(pm)


def main() -> None:
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QPixmap
        from PyQt6.QtWidgets import QApplication, QSplashScreen
    except ModuleNotFoundError as exc:
        if exc.name == "PyQt6":
            print("PyQt6 is not available for this interpreter.")
            print("Aegis looked for C:\\aegis\\.venv automatically but could not load it.")
            print("Install dependencies with:")
            print("  python -m venv .venv")
            print("  .venv\\Scripts\\python.exe -m pip install -r requirements.txt")
            raise SystemExit(1) from exc
        raise

    from ui.main_window import MainWindow

    # Required for the embedded QtWebEngine Brightspace sign-in; harmless otherwise.
    try:
        from PyQt6.QtCore import QCoreApplication
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Aegis.App")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName("Aegis")
    app.setWindowIcon(_app_icon())
    _load_stylesheet(app)

    splash_pix = QPixmap(500, 300)
    splash_pix.fill(Qt.GlobalColor.white)

    splash = QSplashScreen(splash_pix)
    splash.show()
    splash.showMessage(
        "Starting Aegis...",
        Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignCenter,
        Qt.GlobalColor.black,
    )
    app.processEvents()

    splash.showMessage(
        "Initializing database...",
        Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignCenter,
        Qt.GlobalColor.black,
    )
    app.processEvents()

    db = Database()
    task_manager = TaskManager(db)

    splash.showMessage(
        "Loading dashboard...",
        Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignCenter,
        Qt.GlobalColor.black,
    )
    app.processEvents()

    window = MainWindow(task_manager)
    window.show()

    splash.finish(window)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
