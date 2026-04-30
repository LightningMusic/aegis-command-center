from datetime import datetime
from urllib.error import URLError
from urllib.request import Request, urlopen


class BrightspaceClient:
    def __init__(self, config_manager):
        self.config_manager = config_manager

    def get_settings(self):
        return self.config_manager.get_brightspace_settings()

    def test_connection(self):
        settings = self.get_settings()
        base_url = settings["base_url"].strip()

        if not settings.get("enabled"):
            return False, "Brightspace integration is disabled."

        if not base_url:
            return False, "Add your Brightspace base URL in Settings first."

        if not base_url.startswith(("http://", "https://")):
            return False, "Brightspace URL must start with http:// or https://."

        request = Request(
            base_url,
            headers={"User-Agent": "Aegis Brightspace Connector"},
        )

        try:
            with urlopen(request, timeout=5) as response:
                status_code = getattr(response, "status", 200)
        except URLError as exc:
            return False, f"Connection failed: {exc.reason}"
        except Exception as exc:
            return False, f"Connection failed: {exc}"

        if 200 <= status_code < 400:
            return True, f"Connected to {base_url}"

        return False, f"Brightspace returned HTTP {status_code}"

    def sync_status(self):
        ok, message = self.test_connection()
        settings = self.config_manager.update_brightspace_settings(
            {
                "last_sync_at": datetime.now().isoformat(timespec="seconds"),
                "last_sync_status": message,
            }
        )
        return ok, settings

    def get_dashboard_snapshot(self):
        settings = self.get_settings()
        configured = bool(settings.get("base_url"))
        return {
            "enabled": bool(settings.get("enabled")),
            "configured": configured,
            "base_url": settings.get("base_url", ""),
            "org_unit_id": settings.get("org_unit_id", ""),
            "username": settings.get("username", ""),
            "last_sync_at": settings.get("last_sync_at", ""),
            "last_sync_status": settings.get("last_sync_status", "Not connected"),
        }
