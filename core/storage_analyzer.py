import os
from datetime import datetime


class StorageAnalyzer:
    PROTECTED_PATH_KEYWORDS = (
        "windows",
        "program files",
        "programdata",
        "system volume information",
        "$recycle.bin",
        "appdata\\local\\microsoft",
        "appdata\\roaming\\microsoft",
        "steamapps",
        "epic games",
        "ubisoft",
        "battle.net",
        "riot games",
        "origin games",
        "gog galaxy",
        "vmware",
        "virtualbox",
    )

    USER_SPACE_KEYWORDS = (
        "downloads",
        "desktop",
        "documents",
        "videos",
        "pictures",
        "music",
    )

    APP_CRITICAL_EXTENSIONS = {
        ".exe",
        ".dll",
        ".sys",
        ".drv",
        ".msi",
        ".bat",
        ".cmd",
        ".com",
        ".scr",
        ".lnk",
    }

    PROJECT_MARKERS = (
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "pyproject.toml",
        "requirements.txt",
        "Pipfile",
        "poetry.lock",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "settings.gradle",
        "composer.json",
        "Gemfile",
        "*.sln",
    )

    PROJECT_EXTENSIONS = {
        ".py",
        ".pyw",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".java",
        ".kt",
        ".cs",
        ".cpp",
        ".c",
        ".h",
        ".hpp",
        ".rs",
        ".go",
        ".php",
        ".rb",
        ".swift",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".sln",
        ".csproj",
        ".vcxproj",
        ".dbproj",
        ".sql",
        ".ipynb",
    }

    SAFE_CLEANUP_EXTENSIONS = {
        ".zip",
        ".7z",
        ".rar",
        ".tar",
        ".gz",
        ".bz2",
        ".iso",
        ".img",
        ".bak",
        ".old",
        ".tmp",
        ".log",
        ".dmp",
        ".mp4",
        ".mkv",
        ".mov",
        ".avi",
        ".wmv",
        ".psd",
        ".blend",
        ".pdf",
        ".csv",
    }

    def parse_datetime(self, value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _path_lower(self, path):
        return path.lower().replace("/", "\\")

    def is_protected_path(self, path):
        lower = self._path_lower(path)
        return any(keyword in lower for keyword in self.PROTECTED_PATH_KEYWORDS)

    def is_user_space_path(self, path):
        lower = self._path_lower(path)
        return any(keyword in lower for keyword in self.USER_SPACE_KEYWORDS)

    def is_app_critical_file(self, path):
        return os.path.splitext(path)[1].lower() in self.APP_CRITICAL_EXTENSIONS

    def _ancestor_contains_project_marker(self, path):
        current = os.path.dirname(path)
        steps = 0

        while current and steps < 6:
            for marker in self.PROJECT_MARKERS:
                if marker.startswith("*."):
                    extension = marker[1:].lower()
                    try:
                        if any(name.lower().endswith(extension) for name in os.listdir(current)):
                            return True
                    except OSError:
                        continue
                else:
                    if os.path.exists(os.path.join(current, marker)):
                        return True

            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
            steps += 1

        return False

    def is_project_related(self, path):
        extension = os.path.splitext(path)[1].lower()
        return extension in self.PROJECT_EXTENSIONS or self._ancestor_contains_project_marker(path)

    def classify_cleanup_candidate(self, record):
        path = record["absolute_path"]
        extension = os.path.splitext(path)[1].lower()

        if self.is_protected_path(path):
            return None

        if self.is_app_critical_file(path):
            return None

        if self.is_project_related(path):
            return None

        if not self.is_user_space_path(path) and extension not in self.SAFE_CLEANUP_EXTENSIONS:
            return None

        size_bytes = record["size_bytes"] or 0
        last_accessed = self.parse_datetime(record.get("last_accessed"))
        modified_at = self.parse_datetime(record.get("modified_at"))
        now = datetime.now()

        if size_bytes < 100 * 1024 * 1024:
            return None

        if not last_accessed and not modified_at:
            return None

        age_reference = last_accessed or modified_at
        age_days = (now - age_reference).days if age_reference else 0
        modified_days = (now - modified_at).days if modified_at else age_days

        if age_days < 90 and modified_days < 120:
            return None

        reasons = []
        if last_accessed:
            reasons.append(f"last used {age_days} days ago")
        if modified_at:
            reasons.append(f"last modified {modified_days} days ago")
        if self.is_user_space_path(path):
            reasons.append("stored in a user folder")
        if extension in self.SAFE_CLEANUP_EXTENSIONS:
            reasons.append(f"{extension or 'unknown'} file type is usually reviewable")

        score = (size_bytes / (1024**3)) * 2
        score += min(age_days, 720) / 90
        score += min(modified_days, 720) / 180

        return {
            "path": path,
            "size_bytes": size_bytes,
            "size_gb": round(size_bytes / (1024**3), 2),
            "age_days": age_days,
            "modified_days": modified_days,
            "reasons": reasons,
            "score": round(score, 1),
            "risk": "low" if extension in self.SAFE_CLEANUP_EXTENSIONS else "medium",
        }

    def build_cleanup_suggestions(self, records, limit=20):
        suggestions = []

        for record in records:
            candidate = self.classify_cleanup_candidate(record)
            if candidate:
                suggestions.append(candidate)

        suggestions.sort(key=lambda item: (item["score"], item["size_bytes"]), reverse=True)
        return suggestions[:limit]
