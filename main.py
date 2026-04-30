import os
import site
import sys
import time
from pathlib import Path


os.environ["QT_LOGGING_RULES"] = "*.warning=false"

BASE_DIR = Path(__file__).resolve().parent
THEME_PATH = BASE_DIR / "assets" / "theme.qss"


def _bootstrap_local_venv() -> None:
    if "PyQt6" in sys.modules:
        return

    candidate = BASE_DIR / ".venv" / "Lib" / f"site-packages"
    if candidate.exists():
        site.addsitedir(str(candidate))


_bootstrap_local_venv()

from core.database import Database
from modules.task_manager import TaskManager


def _load_stylesheet(app) -> None:
    if THEME_PATH.exists():
        app.setStyleSheet(THEME_PATH.read_text(encoding="utf-8"))


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

    app = QApplication(sys.argv)
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

    time.sleep(0.4)

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
