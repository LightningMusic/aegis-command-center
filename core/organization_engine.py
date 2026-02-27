from collections import defaultdict
from typing import List, Dict


class OrganizationEngine:
    def __init__(self, file_index: List[Dict]):
        self.file_index = file_index

    def group_by_extension(self):
        groups = defaultdict(list)

        for file in self.file_index:
            groups[file["extension"]].append(file)

        return groups

    def find_large_files(self, min_size_mb=500):
        large_files = []

        for file in self.file_index:
            size_mb = file["size"] / (1024 * 1024)
            if size_mb >= min_size_mb:
                large_files.append(file)

        return large_files

    def find_duplicates(self):
        size_map = defaultdict(list)

        for file in self.file_index:
            size_map[file["size"]].append(file)

        duplicates = []

        for size, files in size_map.items():
            if len(files) > 1:
                duplicates.extend(files)

        return duplicates