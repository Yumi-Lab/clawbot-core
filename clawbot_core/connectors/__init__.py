"""
Storage Connectors — abstract interface + factory.
Allows ClawbotCore to sync files to cloud storage (Google Drive, Dropbox, …).
License: BUSL-1.1
"""

import json
import os
from abc import ABC, abstractmethod

CONNECTORS_DIR = "/home/pi/.openjarvis/connectors"
CONFIG_PATH = os.path.join(CONNECTORS_DIR, "config.json")

# ── Abstract interface ────────────────────────────────────────────────────────

class StorageConnector(ABC):
    """Base class for all storage connectors."""

    @abstractmethod
    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """Upload a local file to remote storage. Returns True on success."""
        ...

    @abstractmethod
    def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a remote file to local path. Returns True on success."""
        ...

    @abstractmethod
    def list_dir(self, remote_path: str) -> list:
        """List files/folders at remote_path. Returns list of names."""
        ...

    @abstractmethod
    def mkdir(self, remote_path: str) -> bool:
        """Create a remote folder (and parents). Returns True on success."""
        ...

    @abstractmethod
    def delete(self, remote_path: str) -> bool:
        """Delete a remote file or folder. Returns True on success."""
        ...

    @abstractmethod
    def file_exists(self, remote_path: str) -> bool:
        """Check if a remote file exists."""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the connector has valid credentials."""
        ...


# ── Singleton factory ─────────────────────────────────────────────────────────

_instance = None
_instance_type = None  # tracks which connector type is cached


def get_active_connector():
    """Return the active StorageConnector instance, or None if unconfigured.

    Caches a singleton — call reset_connector() after auth changes.
    """
    global _instance, _instance_type

    try:
        if not os.path.isfile(CONFIG_PATH):
            return None
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception:
        return None

    active = cfg.get("active")
    if not active:
        return None

    # Return cached instance if same type
    if _instance is not None and _instance_type == active:
        return _instance

    # Build new instance
    connector = None
    if active == "gdrive":
        try:
            from connectors.gdrive import GoogleDriveConnector
            connector = GoogleDriveConnector(cfg.get("gdrive", {}))
        except Exception as e:
            print(f"[connector] Failed to init gdrive: {e}")
            return None
    # Future: elif active == "dropbox": ...

    if connector is not None:
        _instance = connector
        _instance_type = active
    return connector


def reset_connector():
    """Clear cached connector instance (call after auth/disconnect)."""
    global _instance, _instance_type
    _instance = None
    _instance_type = None
