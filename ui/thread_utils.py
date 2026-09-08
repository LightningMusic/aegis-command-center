"""
ui/thread_utils.py
==================
Safe teardown for the worker QThreads the views run. Destroying a QThread while
it is still running calls qFatal() and takes the whole process down, so on
window close every worker has to be asked to stop and then waited on.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QThread


def stop_thread(thread: Optional[QThread], worker=None, wait_ms: int = 4000) -> None:
    """Cooperatively stop *worker*, quit *thread*'s event loop, and block until
    it has actually finished. Terminates as a last resort so shutdown can't
    hang or abort."""
    if thread is None:
        return
    try:
        if not thread.isRunning():
            return
    except RuntimeError:  # C++ object already gone
        return

    if worker is not None:
        for name in ("stop", "cancel", "request_stop", "_stop"):
            attr = getattr(worker, name, None)
            if callable(attr):
                try:
                    attr()
                except Exception:  # noqa: BLE001
                    pass
            elif name == "_stop" and hasattr(worker, "_stop"):
                try:
                    worker._stop = True
                except Exception:  # noqa: BLE001
                    pass

    thread.quit()
    if not thread.wait(wait_ms):
        thread.terminate()
        thread.wait(1000)
