import os

from core.drive_indexer import DriveIndexer
from core.organization_engine import OrganizationEngine


class FileManager:
    def __init__(self):
        self.indexer = DriveIndexer()
        self.file_index = []
        self.organizer = None

    def full_scan(self, root="C:\\"):
        print("Scanning drive...")
        self.file_index = self.indexer.scan_drive(root)
        self.organizer = OrganizationEngine(self.file_index)
        print(f"Indexed {len(self.file_index)} files.")
        return self.file_index

    def get_large_files(self, min_size_mb=500):
        if not self.organizer:
            return []
        return self.organizer.find_large_files(min_size_mb)

    def get_duplicates(self):
        if not self.organizer:
            return []
        return self.organizer.find_duplicates()

    def get_grouped_by_extension(self):
        if not self.organizer:
            return {}
        return self.organizer.group_by_extension()