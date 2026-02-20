import os
os.environ["QT_LOGGING_RULES"] = "*.warning=false"

import sys
import time

from PyQt6.QtWidgets import QApplication, QSplashScreen
from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import Qt

from core.database import Database
from modules.task_manager import TaskManager
from ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    with open("assets/theme.qss", "r") as f:
        app.setStyleSheet(f.read())


    # Splash screen
    splash_pix = QPixmap(500, 300)
    splash_pix.fill(Qt.GlobalColor.white)

    splash = QSplashScreen(splash_pix)
    splash.show()
    splash.showMessage(
        "Starting Aegis...",
        Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignCenter,
        Qt.GlobalColor.black
    )
    app.processEvents()

    splash.showMessage(
        "Initializing database...",
        Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignCenter,
        Qt.GlobalColor.black
    )
    app.processEvents()

    db = Database()
    db.initialize()

    task_manager = TaskManager(db)


    time.sleep(0.4)

    splash.showMessage(
        "Loading dashboard...",
        Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignCenter,
        Qt.GlobalColor.black
    )
    app.processEvents()

    window = MainWindow(task_manager)
    window.show()

    splash.finish(window)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
