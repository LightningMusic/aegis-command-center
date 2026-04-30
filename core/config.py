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
    }
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
