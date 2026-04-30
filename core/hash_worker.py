import hashlib
import os

from PyQt6.QtCore import QObject, pyqtSignal


FULL_HASH_MAX_SIZE = 1024 * 1024 * 1024
SAMPLED_HASH_CHUNK_SIZE = 64 * 1024


class HashWorker(QObject):
    progress = pyqtSignal(int)
    finished = pyqtSignal()

    def __init__(self, file_manager):
        super().__init__()
        self.file_manager = file_manager
        self._running = True

    def run(self):
        files = self.file_manager.db.fetchall(
            """
            SELECT absolute_path, size_bytes FROM files
            WHERE hash IS NULL
            AND size_bytes > 1048576
            """
        )

        count = 0

        for path, size in files:
            if not self._running:
                break

            try:
                hash_val = self.calculate_hash(path, size)

                if hash_val:
                    self.file_manager.db.execute(
                        """
                        UPDATE files SET hash = ?
                        WHERE absolute_path = ?
                        """,
                        (hash_val, path),
                    )

                    count += 1
                    if count % 50 == 0:
                        self.progress.emit(count)
            except Exception:
                continue

        self.finished.emit()

    def calculate_hash(self, path, size):
        digest = hashlib.sha256()

        try:
            with open(path, "rb") as file_handle:
                if size <= FULL_HASH_MAX_SIZE:
                    for chunk in iter(lambda: file_handle.read(8192), b""):
                        digest.update(chunk)
                    return f"full:{digest.hexdigest()}"

                first_chunk = file_handle.read(SAMPLED_HASH_CHUNK_SIZE)
                digest.update(first_chunk)

                midpoint = max(size // 2 - SAMPLED_HASH_CHUNK_SIZE // 2, 0)
                file_handle.seek(midpoint)
                digest.update(file_handle.read(SAMPLED_HASH_CHUNK_SIZE))

                tail_start = max(size - SAMPLED_HASH_CHUNK_SIZE, 0)
                file_handle.seek(tail_start)
                digest.update(file_handle.read(SAMPLED_HASH_CHUNK_SIZE))
        except OSError:
            return None

        digest.update(str(size).encode("utf-8"))
        return f"sample:{digest.hexdigest()}"

    def stop(self):
        self._running = False
