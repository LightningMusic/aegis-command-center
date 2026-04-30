import os
from datetime import datetime


class DuplicateDetector:
    PROTECTED_KEYWORDS = (
        "windows",
        "program files",
        "programdata",
        "steamapps",
        "epic games",
        "ubisoft",
        "battle.net",
        "riot games",
        "origin games",
        "gog galaxy",
        "appdata",
    )

    def _parse_datetime(self, value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _is_protected(self, path):
        lower = path.lower().replace("/", "\\")
        return any(keyword in lower for keyword in self.PROTECTED_KEYWORDS)

    def _split_hash(self, hash_value):
        if not hash_value or ":" not in hash_value:
            return "legacy", hash_value
        method, value = hash_value.split(":", 1)
        return method, value

    def _candidate_sort_key(self, record):
        protected_weight = 1 if self._is_protected(record["absolute_path"]) else 0
        last_accessed = self._parse_datetime(record.get("last_accessed")) or datetime.min
        modified_at = self._parse_datetime(record.get("modified_at")) or datetime.min
        return (protected_weight, last_accessed, modified_at, -len(record["absolute_path"]))

    def _make_group(self, signature_type, signature_value, files):
        files = sorted(files, key=self._candidate_sort_key, reverse=True)
        keep = files[0]
        duplicates = files[1:]
        reclaimable = sum(file_record["size_bytes"] for file_record in duplicates)

        return {
            "match_type": signature_type,
            "signature": signature_value,
            "file_count": len(files),
            "size_bytes": keep["size_bytes"],
            "reclaimable_bytes": reclaimable,
            "keep_path": keep["absolute_path"],
            "duplicate_paths": [file_record["absolute_path"] for file_record in duplicates],
            "drives": sorted({file_record["drive"] for file_record in files if file_record.get("drive")}),
            "risk": "high" if any(self._is_protected(file_record["absolute_path"]) for file_record in files) else "review",
        }

    def build_duplicate_groups(self, records, limit=20):
        exact_groups = {}
        probable_groups = {}

        for record in records:
            hash_value = record.get("hash")
            if hash_value:
                method, signature = self._split_hash(hash_value)
                target = exact_groups if method in {"full", "legacy"} else probable_groups
                target.setdefault(signature, []).append(record)
                continue

            name_key = (
                record["size_bytes"],
                record["name"].lower(),
                record["extension"].lower(),
            )
            probable_groups.setdefault(name_key, []).append(record)

        groups = []

        for signature, files in exact_groups.items():
            if len(files) > 1:
                groups.append(self._make_group("Exact duplicate", signature, files))

        for signature, files in probable_groups.items():
            if len(files) > 1:
                groups.append(self._make_group("Probable duplicate", str(signature), files))

        groups.sort(
            key=lambda item: (item["reclaimable_bytes"], item["file_count"]),
            reverse=True,
        )
        return groups[:limit]
