import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.windows_drives import get_drive_map


class DeviceManager:
    """Manages device detection, registration, and naming for organized backups."""

    def __init__(self, database):
        self.db = database
        self._ensure_device_tables()

    def _ensure_device_tables(self):
        """Create device-related tables if they don't exist."""
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                device_id TEXT UNIQUE NOT NULL,
                device_name TEXT NOT NULL,
                device_type TEXT NOT NULL,
                detected_name TEXT,
                last_backup TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT,
                metadata TEXT
            )
            """
        )

        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS device_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                drive_letter TEXT,
                path TEXT,
                is_phone INTEGER DEFAULT 0,
                phone_os TEXT,
                auto_detected INTEGER DEFAULT 1,
                first_seen TEXT,
                FOREIGN KEY (device_id) REFERENCES devices(device_id)
            )
            """
        )

        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS device_backup_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                backup_path TEXT,
                backup_time TEXT,
                file_count INTEGER,
                size_bytes INTEGER,
                status TEXT,
                FOREIGN KEY (device_id) REFERENCES devices(device_id)
            )
            """
        )

    def register_device(
        self,
        device_name: str,
        device_type: str,
        drive_letter: Optional[str] = None,
        phone_os: Optional[str] = None,
    ) -> str:
        """
        Register a new device for backup organization.

        Args:
            device_name: Human-readable name (e.g., "Desktop_PC", "iPhone_Elijah")
            device_type: "pc", "phone", "external_drive"
            drive_letter: Windows drive letter (e.g., "C", "D")
            phone_os: "android" or "ios" if device_type is "phone"

        Returns:
            device_id: Unique identifier for the device
        """
        device_id = str(uuid.uuid4())[:8]
        created_at = datetime.now().isoformat()

        self.db.execute(
            """
            INSERT INTO devices
            (id, device_id, device_name, device_type, created_at, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (device_id, device_id, device_name, device_type, created_at),
        )

        if drive_letter or phone_os:
            self.db.execute(
                """
                INSERT INTO device_mappings
                (device_id, drive_letter, phone_os, first_seen)
                VALUES (?, ?, ?, ?)
                """,
                (device_id, drive_letter, phone_os, created_at),
            )

        return device_id

    def get_device_by_name(self, device_name: str) -> Optional[Dict]:
        """Get device details by human-readable name."""
        result = self.db.fetchall(
            "SELECT * FROM devices WHERE device_name = ?",
            (device_name,),
        )
        if result:
            return self._row_to_device_dict(result[0])
        return None

    def get_device_by_id(self, device_id: str) -> Optional[Dict]:
        """Get device details by device ID."""
        result = self.db.fetchall(
            "SELECT * FROM devices WHERE device_id = ?",
            (device_id,),
        )
        if result:
            return self._row_to_device_dict(result[0])
        return None

    def get_all_devices(self, active_only: bool = True) -> List[Dict]:
        """Get all registered devices, optionally filtered to active ones."""
        query = "SELECT * FROM devices"
        params = ()
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY device_name ASC"

        results = self.db.fetchall(query, params)
        return [self._row_to_device_dict(row) for row in results]

    def get_devices_by_type(self, device_type: str) -> List[Dict]:
        """Get all devices of a specific type (pc, phone, external_drive)."""
        results = self.db.fetchall(
            "SELECT * FROM devices WHERE device_type = ? AND is_active = 1 ORDER BY device_name ASC",
            (device_type,),
        )
        return [self._row_to_device_dict(row) for row in results]

    def rename_device(self, device_id: str, new_name: str) -> bool:
        """Rename a registered device."""
        self.db.execute(
            "UPDATE devices SET device_name = ? WHERE device_id = ?",
            (new_name, device_id),
        )
        return True

    def update_last_backup(self, device_id: str, backup_path: str, file_count: int, size_bytes: int) -> None:
        """Record a backup for this device."""
        now = datetime.now().isoformat()

        self.db.execute(
            "UPDATE devices SET last_backup = ? WHERE device_id = ?",
            (now, device_id),
        )

        self.db.execute(
            """
            INSERT INTO device_backup_history
            (device_id, backup_path, backup_time, file_count, size_bytes, status)
            VALUES (?, ?, ?, ?, ?, 'success')
            """,
            (device_id, backup_path, now, file_count, size_bytes),
        )

    def get_backup_history(self, device_id: str, limit: int = 10) -> List[Dict]:
        """Get recent backup history for a device."""
        results = self.db.fetchall(
            """
            SELECT * FROM device_backup_history
            WHERE device_id = ?
            ORDER BY backup_time DESC
            LIMIT ?
            """,
            (device_id, limit),
        )
        return [self._row_to_dict(row, ["id", "device_id", "backup_path", "backup_time", "file_count", "size_bytes", "status"]) for row in results]

    def detect_connected_devices(self) -> List[Dict]:
        """
        Auto-detect connected devices (drives, phones via USB).

        Returns:
            List of detected devices with info
        """
        detected = []

        drive_map = get_drive_map()
        for drive_letter, drive_info in drive_map.items():
            if not drive_info["is_backup_destination"]:
                detected.append(
                    {
                        "drive_letter": drive_letter,
                        "label": drive_info.get("label", f"Drive {drive_letter}"),
                        "device_type": "external_drive" if drive_info.get("is_external") else "pc",
                        "auto_detected": True,
                    }
                )

        detected_phones = self._detect_connected_phones()
        detected.extend(detected_phones)

        return detected

    def _detect_connected_phones(self) -> List[Dict]:
        """
        Detect Android phones connected via USB.
        
        Note: This is a basic detection that checks for common Android paths.
        For production use, consider using pyusb or pymtp libraries.
        """
        detected_phones = []

        try:
            import win32api

            for drive_letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                drive_path = f"{drive_letter}:\\"
                if not os.path.exists(drive_path):
                    continue

                dcim_path = os.path.join(drive_path, "DCIM")
                documents_path = os.path.join(drive_path, "Documents")
                downloads_path = os.path.join(drive_path, "Downloads")

                has_phone_structure = (
                    os.path.exists(dcim_path)
                    or os.path.exists(documents_path)
                    or os.path.exists(downloads_path)
                )

                if has_phone_structure:
                    detected_phones.append(
                        {
                            "drive_letter": drive_letter,
                            "label": f"Phone_{drive_letter}",
                            "device_type": "phone",
                            "phone_os": "android",
                            "path": drive_path,
                            "auto_detected": True,
                        }
                    )
        except ImportError:
            pass

        return detected_phones

    def _row_to_device_dict(self, row: tuple) -> Dict:
        """Convert database row to device dictionary."""
        return {
            "id": row[0],
            "device_id": row[1],
            "device_name": row[2],
            "device_type": row[3],
            "detected_name": row[4],
            "last_backup": row[5],
            "is_active": row[6],
            "created_at": row[7],
            "metadata": row[8],
        }

    def _row_to_dict(self, row: tuple, columns: List[str]) -> Dict:
        """Convert database row to dictionary with specified columns."""
        return {col: val for col, val in zip(columns, row)}
