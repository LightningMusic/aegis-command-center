"""
core/secure_store.py
====================
Small key/value secret store backed by an XML file whose *values* are
encrypted with Windows DPAPI (via pywin32, already a dependency).

- Ciphertext is bound to the current Windows user account: the file cannot be
  decrypted on another machine or by another user, and no master password is
  needed.
- The file is still readable XML so you can see *which* secrets exist, just not
  their contents.

    store = SecureStore(Path("brightspace_secrets.xml"))
    store.set("brightspace_feed_url", "https://…/feed.ics?token=…")
    url = store.get("brightspace_feed_url")   # -> str | None
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

# Extra entropy mixed into every DPAPI blob. Not a secret (it's in source), it
# just means a blob lifted from this file can't be decrypted by another app
# running as the same user without also knowing this value.
_ENTROPY = b"aegis.securestore.v1"


class SecureStoreError(RuntimeError):
    pass


class SecureStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # ── crypto ────────────────────────────────────────────────────────────
    @staticmethod
    def _win32crypt():
        try:
            import win32crypt  # type: ignore
        except ImportError as exc:  # pragma: no cover - Windows only
            raise SecureStoreError(
                "pywin32 is required to read or write encrypted secrets."
            ) from exc
        return win32crypt

    def _encrypt(self, plaintext: str) -> str:
        blob = self._win32crypt().CryptProtectData(
            plaintext.encode("utf-8"), "Aegis secret", _ENTROPY, None, None, 0
        )
        return base64.b64encode(blob).decode("ascii")

    def _decrypt(self, token: str) -> str:
        try:
            blob = base64.b64decode(token.encode("ascii"))
            _desc, data = self._win32crypt().CryptUnprotectData(
                blob, _ENTROPY, None, None, 0
            )
        except SecureStoreError:
            raise
        except Exception as exc:
            raise SecureStoreError(
                "A saved secret could not be decrypted (wrong Windows account, "
                "or the file was copied from another machine). Re-enter it in Settings."
            ) from exc
        return data.decode("utf-8")

    # ── xml io ────────────────────────────────────────────────────────────
    def _load_tree(self) -> ET.ElementTree:
        if not self.path.exists():
            return ET.ElementTree(ET.Element("aegis-secrets", {"version": "1"}))
        try:
            return ET.parse(self.path)
        except ET.ParseError as exc:
            raise SecureStoreError(f"Secret file is corrupt: {self.path}") from exc

    def _save_tree(self, tree: ET.ElementTree) -> None:
        root = tree.getroot()
        try:
            ET.indent(tree, space="  ")
        except Exception:
            pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        ET.ElementTree(root).write(tmp, encoding="utf-8", xml_declaration=True)
        os.replace(tmp, self.path)

    @staticmethod
    def _find(root: ET.Element, name: str) -> Optional[ET.Element]:
        for el in root.findall("secret"):
            if el.get("name") == name:
                return el
        return None

    # ── public api ───────────────────────────────────────────────────────
    def get(self, name: str) -> Optional[str]:
        el = self._find(self._load_tree().getroot(), name)
        if el is None:
            return None
        value = (el.text or "").strip()
        return self._decrypt(value) if value else None

    def set(self, name: str, value: str) -> None:
        tree = self._load_tree()
        root = tree.getroot()
        el = self._find(root, name)
        if el is None:
            el = ET.SubElement(root, "secret", {"name": name})
        el.set("enc", "dpapi")
        el.text = self._encrypt(value)
        self._save_tree(tree)

    def delete(self, name: str) -> None:
        tree = self._load_tree()
        root = tree.getroot()
        el = self._find(root, name)
        if el is not None:
            root.remove(el)
            self._save_tree(tree)

    def has(self, name: str) -> bool:
        try:
            return self.get(name) is not None
        except SecureStoreError:
            # A value exists but can't be decrypted — treat as "present but broken".
            return self._find(self._load_tree().getroot(), name) is not None
