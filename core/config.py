import json
from copy import deepcopy
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "brightspace": {
        "enabled": False,
        "base_url": "",
        "org_unit_id": "",
        "username": "",
        "access_token": "",
        "last_sync_at": "",
        "last_sync_status": "Not connected",
    },
    "backup": {
        "default_destination": "",
        "saved_destinations": [],
        "backup_root_name": "Aegis_Backups",
        "log_root_name": "Aegis_Backups\\logs",
        "last_destination": "",
        "last_mode": "mirror",
        "last_run_at": "",
        "last_status": "No backups run yet",
    },
    "google_workspace": {
        "enabled": False,
        "account_email": "",
        "client_id": "",
        "client_secret": "",
        "refresh_token": "",
        "access_token": "",
        "last_sync_at": "",
        "last_sync_status": "Not connected",
    },
    "outlook": {
        "enabled": False,
        "account_email": "",
        "tenant_id": "",
        "client_id": "",
        "client_secret": "",
        "refresh_token": "",
        "access_token": "",
        "last_sync_at": "",
        "last_sync_status": "Not connected",
    },
}


class ConfigManager:
    def __init__(self, path: Path | None = None):
        self.path = path or CONFIG_PATH
        self._config = self._load()

    def _load(self):
        if not self.path.exists():
            return deepcopy(DEFAULT_CONFIG)

        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return deepcopy(DEFAULT_CONFIG)

        merged = deepcopy(DEFAULT_CONFIG)
        for section, values in loaded.items():
            if isinstance(values, dict) and isinstance(merged.get(section), dict):
                merged[section].update(values)
            else:
                merged[section] = values
        return merged

    def save(self):
        self.path.write_text(
            json.dumps(self._config, indent=2),
            encoding="utf-8",
        )

    def get_brightspace_settings(self):
        return deepcopy(self._config["brightspace"])

    def update_brightspace_settings(self, settings):
        current = self._config["brightspace"]
        current.update(settings)
        self.save()
        return deepcopy(current)

    def get_backup_settings(self):
        return deepcopy(self._config["backup"])

    def update_backup_settings(self, settings):
        current = self._config["backup"]
        current.update(settings)
        self.save()
        return deepcopy(current)

    def get_google_workspace_settings(self):
        return deepcopy(self._config["google_workspace"])

    def update_google_workspace_settings(self, settings):
        current = self._config["google_workspace"]
        current.update(settings)
        self.save()
        return deepcopy(current)

    def get_outlook_settings(self):
        return deepcopy(self._config["outlook"])

    def update_outlook_settings(self, settings):
        current = self._config["outlook"]
        current.update(settings)
        self.save()
        return deepcopy(current)
