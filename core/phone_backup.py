import os
import shutil
import json
import psutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from modules.phone_file_organizer import PhoneFileOrganizer


class PhoneBackup:
    """Handles backup of USB-connected phones with intelligent file organization."""

    # Android folder structure to search for (Motorola and all Android phones)
    ANDROID_FOLDERS = [
        "DCIM",           # Camera pictures
        "Pictures",       # Picture folder
        "Downloads",      # Downloads folder
        "Documents",      # Documents folder
        "Music",          # Music folder
        "Videos",         # Video folder
        "Camera",         # Camera folder (some devices)
        "Screenshots",    # Screenshot folder
        "Telegram",       # Telegram app
        "WhatsApp",       # WhatsApp app
        "Messages",       # SMS/Messages
        "Android",        # Android system folder
        "safe",           # Samsung Safe folder (lowercase)
        "Safe",           # Samsung Safe folder (mixed case)
        "SAFE",           # Samsung Safe folder (uppercase)
        "Vault",          # Vault/Secure folder
        "Secure Folder",  # Secure folder
        "Saved Games",    # Game saves
        "Podcasts",       # Podcasts
        "Audiobooks",     # Audiobooks
        "Notifications",  # Notification sounds
        "Ringtones",      # Ringtone files
        "Alarms",         # Alarm sounds
        "DataBackup",     # Data backup folder
        "Photos",         # Another photo location
        "Recorder",       # Voice recorder
        "Movies",         # Movies folder
        "Motorola",       # Motorola-specific folder
        "LOST.DIR",       # Lost directory (recovery)
        "Bluetooth",      # Bluetooth files
        ".local",         # Local storage
        "GBoard",         # Google Keyboard data
        "WeChat",         # WeChat app
        "QQ",             # QQ app
        "alarms",         # Lowercase alarms
        "ringtones",      # Lowercase ringtones
        "notifications",  # Lowercase notifications
        "podcasts",       # Lowercase podcasts
    ]

    # Files/folders that indicate an Android phone (Motorola and all Android)
    ANDROID_INDICATORS = [
        ".android_secure",
        "Android/data",
        "Android/obb",
        "Android/media",
        ".thumbnails",
        "DCIM/.thumbnails",
        ".recently-used",
        "Android/system",  # Motorola system files
        "build.prop",      # Build properties
        ".Motorola",       # Motorola-specific
    ]
    
    # Motorola-specific indicators for stronger detection
    MOTOROLA_INDICATORS = [
        "Motorola",
        ".Motorola",
        "Android/system",
        "DCIM",
        "Documents",
        "Downloads",
    ]

    def __init__(self):
        self.organizer = None

    def detect_connected_phones(self) -> List[Dict]:
        """
        Detect Android phones connected via USB with multiple detection methods.
        Optimized for Motorola phones on USB file transfer mode.

        Returns:
            List of detected phone drives with metadata
        """
        detected_phones = []

        try:

            partitions = psutil.disk_partitions(all=True)
            for partition in partitions:
                if not os.path.exists(partition.mountpoint):
                    continue

                # Skip system drives more carefully
                if self._is_system_drive(partition.mountpoint):
                    continue

                phone_info = self._check_if_phone(partition.mountpoint)
                if phone_info:
                    detected_phones.append(phone_info)

        except ImportError:
            # Fallback if psutil not available
            detected_phones.extend(self._detect_phones_fallback())

        return detected_phones

    def _is_system_drive(self, path: str) -> bool:
        """Check if this is a system drive we should skip."""
        try:
            path_lower = path.lower()
            
            # Skip Windows system paths
            if path_lower.startswith("c:\\") or path_lower.startswith("c:/"):
                return True
            if "program files" in path_lower:
                return True
            if "windows" in path_lower:
                return True
            if "users" in path_lower and "\\appdata\\" in path_lower:
                return True
            if "recovery" in path_lower:
                return True
                
            # Don't skip user folders like "Users\Documents" but do skip system paths
            # This is important for USB external drives and phones
            return False
        except Exception:
            return False

    def _check_if_phone(self, mount_point: str) -> Optional[Dict]:
        """
        Comprehensive check if a mounted drive is an Android phone.
        Uses multiple detection methods for robustness.
        Optimized for Motorola phones on USB file transfer.
        """
        try:
            mount_path = Path(mount_point)
            
            # Method 1: Check for Android folder structure
            android_folder_count = 0
            found_folders = []
            motorola_folder_count = 0
            
            for folder_name in self.ANDROID_FOLDERS:
                try:
                    # Case-insensitive folder check
                    folder_path = mount_path / folder_name
                    if folder_path.exists() and folder_path.is_dir():
                        android_folder_count += 1
                        found_folders.append(folder_name)
                        
                        # Check for Motorola-specific folders
                        if folder_name in self.MOTOROLA_INDICATORS:
                            motorola_folder_count += 1
                except (OSError, PermissionError):
                    continue
            
            # Method 2: Check for Android indicator files/folders
            has_android_indicators = False
            for indicator in self.ANDROID_INDICATORS:
                try:
                    indicator_path = mount_path / indicator
                    if indicator_path.exists():
                        has_android_indicators = True
                        break
                except (OSError, PermissionError):
                    continue
            
            # Method 3: Case-insensitive search for key Android folders
            has_dcim = self._check_folder_exists_case_insensitive(mount_path, "DCIM")
            has_documents = self._check_folder_exists_case_insensitive(mount_path, "Documents")
            has_downloads = self._check_folder_exists_case_insensitive(mount_path, "Downloads")
            has_android_folder = self._check_folder_exists_case_insensitive(mount_path, "Android")
            has_pictures = self._check_folder_exists_case_insensitive(mount_path, "Pictures")
            
            # Method 4: Check for build.prop or other Android system files
            has_android_system = False
            system_file_paths = [
                mount_path / "system" / "build.prop",
                mount_path / "build.prop",
                mount_path / "Android" / "data",
                mount_path / "Android" / "obb",
            ]
            
            for sys_path in system_file_paths:
                try:
                    if sys_path.exists():
                        has_android_system = True
                        break
                except (OSError, PermissionError):
                    continue
            
            # Method 5: Check device name from various locations
            device_name = self._detect_device_name(mount_path, found_folders)
            
            # Make decision based on findings
            is_phone = False
            confidence = 0
            
            # Strong indicators for Android phone
            if has_dcim or has_android_folder or has_android_system:
                confidence += 50
                is_phone = True
            
            # Good indicators
            if android_folder_count >= 3:
                confidence += 30
                is_phone = True
            elif android_folder_count >= 1 and has_android_indicators:
                confidence += 35
                is_phone = True
            elif has_documents and has_downloads and has_pictures:
                confidence += 25
                is_phone = True
            
            # Additional confidence boosters
            if motorola_folder_count >= 2:
                confidence += 20
            
            if has_android_indicators:
                confidence += 15
            
            if android_folder_count >= 8:
                confidence += 10
            
            if has_android_system:
                confidence += 20
            
            # Must have minimum confidence
            if not is_phone or confidence < 40:
                return None
            
            # Get available space
            try:
                available_space = shutil.disk_usage(mount_point).free
            except Exception:
                available_space = 0
            
            # Check for safe/secure folders
            has_safe_folder = self._check_safe_folder_exists(mount_path, found_folders)
            
            return {
                "mount_point": mount_point,
                "device_name": device_name,
                "available_space": available_space,
                "folders_found": found_folders,
                "confidence": min(100, confidence),
                "has_safe_folder": has_safe_folder,
                "is_motorola": motorola_folder_count >= 1,
            }
        
        except Exception as e:
            return None
    
    def _check_folder_exists_case_insensitive(self, base_path: Path, folder_name: str) -> bool:
        """
        Check if a folder exists with case-insensitive matching.
        Useful for finding folders regardless of capitalization.
        """
        try:
            folder_name_lower = folder_name.lower()
            
            # Direct path check
            direct_path = base_path / folder_name
            if direct_path.exists() and direct_path.is_dir():
                return True
            
            # List and check case-insensitively
            try:
                for item in base_path.iterdir():
                    if item.is_dir() and item.name.lower() == folder_name_lower:
                        return True
            except (OSError, PermissionError):
                pass
            
            return False
        except Exception:
            return False
    
    def _check_safe_folder_exists(self, mount_path: Path, found_folders: List[str]) -> bool:
        """Check for Safe/Vault/Secure folders in various case combinations."""
        safe_folder_names = [
            "safe", "Safe", "SAFE",
            "vault", "Vault", "VAULT",
            "Secure Folder", "secure folder",
            "SecureFolder", "securefolder",
            "Secure_Folder", "secure_folder",
            "PrivateFiles", "private_files",
            "Private Files", "private files",
        ]
        
        for folder in safe_folder_names:
            if self._check_folder_exists_case_insensitive(mount_path, folder):
                return True
        
        # Also check in found_folders list
        found_lower = [f.lower() for f in found_folders]
        safe_keywords = ["safe", "vault", "secure"]
        return any(keyword in folder for folder in found_lower for keyword in safe_keywords)

    def _detect_device_name(self, mount_path: Path, found_folders: List[str]) -> str:
        """Try to detect device name from various sources. Motorola-optimized."""
        try:
            # Method 1: Check build.prop for device model (works for Motorola)
            build_prop_paths = [
                mount_path / "system" / "build.prop",
                mount_path / "build.prop",
                mount_path / "Android" / "system" / "build.prop",
            ]
            
            device_brand = ""
            device_model = ""
            device_name_prop = ""
            
            for prop_file in build_prop_paths:
                if prop_file.exists():
                    try:
                        with open(prop_file, "r", encoding="utf-8", errors="ignore") as f:
                            for line in f:
                                line = line.strip()
                                if not line or line.startswith("#"):
                                    continue
                                
                                # Extract device information
                                if "ro.product.model=" in line:
                                    device_model = line.split("=")[1].strip().replace(" ", "_")
                                elif "ro.product.brand=" in line:
                                    device_brand = line.split("=")[1].strip().replace(" ", "_")
                                elif "ro.product.name=" in line:
                                    device_name_prop = line.split("=")[1].strip().replace(" ", "_")
                                elif "ro.build.display.id=" in line:
                                    display_id = line.split("=")[1].strip()
                                    if not device_model:
                                        device_model = display_id.split(".")[0] if "." in display_id else display_id
                        
                        # If we found model and brand, return immediately
                        if device_model and device_brand:
                            return f"{device_brand}_{device_model}"
                        elif device_model:
                            return f"Android_{device_model}"
                    except Exception:
                        pass
            
            # Method 2: Try to find device info in Android folder
            try:
                android_path = mount_path / "Android"
                if android_path.exists():
                    # Look for device identifier files
                    for item in android_path.iterdir():
                        if item.is_dir() and ("data" in item.name or "media" in item.name):
                            # Check for Motorola-specific folders
                            if "motorola" in item.name.lower():
                                return "Motorola_Device"
            except (OSError, PermissionError):
                pass
            
            # Method 3: Check for device name in folder structure
            if "DCIM" in found_folders or "Camera" in found_folders:
                if device_brand:
                    return f"{device_brand}_Phone"
                return "Android_Phone"
            
            # Method 4: Look for device-specific patterns in filenames
            try:
                for folder in [mount_path / "DCIM", mount_path / "Pictures", mount_path / "Documents"]:
                    if folder.exists():
                        for item in folder.iterdir():
                            if item.is_file():
                                # Parse filename for device info
                                name_lower = item.name.lower()
                                if "motorola" in name_lower or "moto" in name_lower:
                                    return "Motorola_Device"
                                break
            except (OSError, PermissionError):
                pass
            
            # Fallback names
            if device_brand and device_brand.lower() == "motorola":
                return f"Motorola_{device_model}" if device_model else "Motorola_Phone"
            
            return device_brand + ("_" + device_model if device_model else "") if device_brand else "Android_Device"
        
        except Exception:
            return "Android_Phone"

    def _detect_phones_fallback(self) -> List[Dict]:
        """Fallback phone detection without psutil."""
        detected = []
        
        try:
            # Check all drive letters on Windows
            for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
                drive = f"{letter}:\\"
                if not os.path.exists(drive):
                    continue
                
                phone_info = self._check_if_phone(drive)
                if phone_info:
                    detected.append(phone_info)
        
        except Exception:
            pass
        
        return detected

    def backup_phone(
        self,
        phone_mount_point: str,
        device_name: str,
        destination_root: str,
        organize: bool = True,
        progress_callback=None,
        should_stop=None,
    ) -> Dict:
        """
        Backup files from a USB-connected phone, including Safe/Secure folders.

        Args:
            phone_mount_point: Mount point of the phone (e.g., "E:\\")
            device_name: Human-readable device name (e.g., "Moto_Edge_2024")
            destination_root: Root backup destination
            organize: Whether to organize files by type
            progress_callback: Optional callback(progress, message)
            should_stop: Optional callable to check if should stop

        Returns:
            Backup results dictionary
        """
        phone_path = Path(phone_mount_point)
        if not phone_path.exists():
            raise FileNotFoundError(
                f"Phone mount point does not exist: {phone_mount_point}"
            )

        if not self.organizer:
            self.organizer = PhoneFileOrganizer(destination_root)

        backup_root = Path(destination_root)
        device_backup_root = backup_root / device_name / "latest"
        device_backup_root.mkdir(parents=True, exist_ok=True)

        backup_results = {
            "device_name": device_name,
            "phone_path": str(phone_path),
            "destination": str(device_backup_root),
            "started_at": datetime.now().isoformat(),
            "completed_at": None,
            "files_copied": 0,
            "files_failed": 0,
            "total_bytes": 0,
            "categories": {},
            "errors": [],
            "folders_backed_up": [],
        }

        try:
            # Collect files from all accessible folders including Safe folder
            files_to_backup = self._collect_phone_files(phone_path)
            total_files = len(files_to_backup)

            if total_files == 0:
                backup_results["completed_at"] = datetime.now().isoformat()
                backup_results["errors"].append({
                    "phase": "collection",
                    "error": "No files found on phone"
                })
                return backup_results

            for index, file_path in enumerate(files_to_backup):
                if should_stop and should_stop():
                    backup_results["cancelled"] = True
                    break

                if progress_callback:
                    progress = int((index / max(total_files, 1)) * 50)
                    progress_callback(progress, f"Copying {file_path.name}")

                try:
                    copied_size = self._copy_phone_file(
                        file_path, device_backup_root, organize
                    )
                    backup_results["files_copied"] += 1
                    backup_results["total_bytes"] += copied_size

                    if organize:
                        category = self._get_file_category(file_path)
                        if category not in backup_results["categories"]:
                            backup_results["categories"][category] = 0
                        backup_results["categories"][category] += 1

                except OSError as e:
                    backup_results["files_failed"] += 1
                    backup_results["errors"].append({
                        "file": str(file_path),
                        "error": str(e),
                    })

            if should_stop and should_stop():
                backup_results["completed_at"] = datetime.now().isoformat()
                return backup_results

            if organize:
                if progress_callback:
                    progress_callback(50, "Organizing files by type...")

                organize_results = self.organizer.organize_phone_backup(
                    str(phone_path),
                    device_name,
                    progress_callback=lambda p, m: progress_callback(
                        50 + int(p * 0.5), m
                    ) if progress_callback else None,
                    should_stop=should_stop,
                )

                backup_results["organization"] = organize_results

            backup_results["completed_at"] = datetime.now().isoformat()

            # Create log
            log_path = backup_root / "logs"
            log_path.mkdir(parents=True, exist_ok=True)
            log_file = log_path / f"phone_backup_{device_name}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"

            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(backup_results, f, indent=2)

            backup_results["log_file"] = str(log_file)

        except Exception as e:
            backup_results["completed_at"] = datetime.now().isoformat()
            backup_results["errors"].append({
                "phase": "backup",
                "error": str(e),
            })

        if progress_callback:
            progress_callback(100, "Phone backup complete")

        return backup_results

    def _collect_phone_files(self, phone_path: Path) -> List[Path]:
        """
        Collect all files from phone that should be backed up.
        Includes Safe/Secure/Vault folders.
        Motorola-optimized.
        """
        files = []
        visited_folders = set()
        
        # First, collect from all standard Android folders
        for folder_name in self.ANDROID_FOLDERS:
            folder_path = phone_path / folder_name
            
            # Avoid visiting same folder twice (case sensitivity)
            try:
                folder_key = folder_path.resolve().as_posix().lower()
            except (OSError, PermissionError):
                continue
                
            if folder_key in visited_folders:
                continue
            visited_folders.add(folder_key)
            
            if not folder_path.exists():
                # Try case-insensitive search
                try:
                    for item in phone_path.iterdir():
                        if item.is_dir() and item.name.lower() == folder_name.lower():
                            folder_path = item
                            break
                    else:
                        continue
                except (OSError, PermissionError):
                    continue

            try:
                for root, dirs, filenames in os.walk(str(folder_path)):
                    # Limit recursion depth for safety
                    depth = len(Path(root).relative_to(folder_path).parts)
                    if depth > 20:  # Increased from 15 to handle more complex phone structures
                        dirs.clear()
                        continue
                    
                    # Skip system/hidden directories
                    dirs[:] = [d for d in dirs if not d.startswith('.')]

                    for filename in filenames:
                        try:
                            file_path = Path(root) / filename
                            if file_path.is_file() and file_path.stat().st_size > 0:
                                files.append(file_path)
                        except (OSError, PermissionError):
                            continue

            except (PermissionError, OSError):
                continue
        
        # Second pass: Look for Safe/Secure folders specifically
        safe_folder_patterns = [
            "safe", "Safe", "SAFE",
            "vault", "Vault", "VAULT",
            "secure", "Secure", "SECURE",
            "Secure Folder", "secure folder",
            "SecureFolder", "securefolder",
            "PrivateFiles", "private_files",
            "Private Files", "private files",
        ]
        
        try:
            for item in phone_path.iterdir():
                item_name_lower = item.name.lower()
                
                # Check if this is a safe folder
                is_safe_folder = False
                safe_keywords = ["safe", "vault", "secure", "private"]
                for keyword in safe_keywords:
                    if keyword in item_name_lower:
                        is_safe_folder = True
                        break
                
                if is_safe_folder and item.is_dir():
                    folder_key = item.resolve().as_posix().lower()
                    if folder_key not in visited_folders:
                        visited_folders.add(folder_key)
                        
                        try:
                            for root, dirs, filenames in os.walk(str(item)):
                                depth = len(Path(root).relative_to(item).parts)
                                if depth > 20:
                                    dirs.clear()
                                    continue
                                
                                dirs[:] = [d for d in dirs if not d.startswith('.')]
                                
                                for filename in filenames:
                                    try:
                                        file_path = Path(root) / filename
                                        if file_path.is_file() and file_path.stat().st_size > 0:
                                            files.append(file_path)
                                    except (OSError, PermissionError):
                                        continue
                        except (PermissionError, OSError):
                            continue
        except (PermissionError, OSError):
            pass
        
        return files

    def _copy_phone_file(
        self, file_path: Path, destination_root: Path, organize: bool
    ) -> int:
        """
        Copy a file from phone to backup destination.

        Args:
            file_path: Source file path
            destination_root: Destination directory
            organize: If True, organize into category folders

        Returns:
            Number of bytes copied
        """
        if organize:
            category = self._get_file_category(file_path)
            dest_dir = destination_root / category
        else:
            dest_dir = destination_root / file_path.parent.name

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / file_path.name

        if dest_file.exists():
            try:
                source_stat = file_path.stat()
                dest_stat = dest_file.stat()

                if (
                    dest_stat.st_size == source_stat.st_size
                    and int(dest_stat.st_mtime) >= int(source_stat.st_mtime)
                ):
                    return 0
            except OSError:
                pass

        shutil.copy2(file_path, dest_file)
        return file_path.stat().st_size

    def _get_file_category(self, file_path: Path) -> str:
        """Determine the category for a phone file."""
        from modules.phone_file_organizer import PHONE_CATEGORY_RULES

        extension = file_path.suffix.lower()
        parent_folder = file_path.parent.name.upper()

        for category, rules in PHONE_CATEGORY_RULES.items():
            if extension in rules["extensions"]:
                return category

            if parent_folder in rules["folders"]:
                return category

        # Special handling for Safe/Secure folders
        if "safe" in parent_folder.lower() or "vault" in parent_folder.lower():
            return "Private_Files"

        return "Miscellaneous"
