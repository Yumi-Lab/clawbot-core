"""
Google Drive Connector for ClawbotCore.
Syncs workspace files to a /OpenJarvis/ folder on the user's Google Drive.
License: BUSL-1.1

Dependencies: google-api-python-client, google-auth-oauthlib
"""

from __future__ import annotations

import io
import json
import os
import random
import time

from connectors import StorageConnector, CONNECTORS_DIR

TOKEN_PATH = os.path.join(CONNECTORS_DIR, "gdrive_token.json")
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
MAX_RETRIES = 3

# ── Lazy imports (fail gracefully if deps missing) ────────────────────────────

_google_deps_ok = True
_google_deps_error = ""
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
    from googleapiclient.errors import HttpError
except ImportError as e:
    _google_deps_ok = False
    _google_deps_error = (
        f"Google Drive dependencies missing: {e}. "
        "Install with: pip3 install google-api-python-client google-auth-oauthlib"
    )


def _check_deps():
    if not _google_deps_ok:
        raise RuntimeError(_google_deps_error)


# ── Retry helper ──────────────────────────────────────────────────────────────

def _retry(fn, max_retries=MAX_RETRIES):
    """Call fn() with exponential backoff on transient errors."""
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            retryable = False
            if _google_deps_ok and isinstance(e, HttpError):
                retryable = e.resp.status in (429, 500, 502, 503)
            elif isinstance(e, (ConnectionError, TimeoutError, OSError)):
                retryable = True

            if retryable and attempt < max_retries:
                delay = (2 ** attempt) + random.uniform(0, 1)
                print(f"[gdrive] Retry {attempt + 1}/{max_retries} in {delay:.1f}s: {e}")
                time.sleep(delay)
            else:
                raise


# ── GoogleDriveConnector ──────────────────────────────────────────────────────

class GoogleDriveConnector(StorageConnector):
    """Sync files to Google Drive under /OpenJarvis/."""

    def __init__(self, config: dict):
        _check_deps()
        self._client_id = config.get("client_id", "")
        self._client_secret = config.get("client_secret", "")
        self._folder_name = config.get("folder_name", "OpenJarvis")
        self._service = None
        self._creds = None
        self._folder_cache = {}  # path -> folder_id

    # ── Auth ──────────────────────────────────────────────────────────────

    def _load_creds(self):
        """Load and refresh credentials from token file."""
        if not os.path.isfile(TOKEN_PATH):
            return None
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(TOKEN_PATH, "w") as f:
                    f.write(creds.to_json())
            except Exception as e:
                print(f"[gdrive] Token refresh failed: {e}")
                return None
        return creds if creds and creds.valid else None

    def _get_service(self):
        """Lazy-build the Drive API service."""
        if self._service is not None and self._creds and self._creds.valid:
            return self._service
        self._creds = self._load_creds()
        if self._creds is None:
            raise RuntimeError("Google Drive not authenticated. Use /core/connectors/gdrive/auth first.")
        self._service = build("drive", "v3", credentials=self._creds, cache_discovery=False)
        return self._service

    def is_connected(self) -> bool:
        try:
            creds = self._load_creds()
            return creds is not None and creds.valid
        except Exception:
            return False

    @classmethod
    def exchange_code(cls, auth_code: str, redirect_uri: str = "urn:ietf:wg:oauth:2.0:oob"):
        """Exchange OAuth authorization code for tokens and save them.

        Called from POST /core/connectors/gdrive/auth.
        """
        _check_deps()
        os.makedirs(CONNECTORS_DIR, exist_ok=True)

        # Load client config
        config_path = os.path.join(CONNECTORS_DIR, "..", "connectors", "config.json")
        # Normalize — config is at CONNECTORS_DIR/../connectors/config.json = CONNECTORS_DIR/config.json
        from connectors import CONFIG_PATH as cfg_path
        if not os.path.isfile(cfg_path):
            raise RuntimeError("Connector config not found. Set client_id/client_secret first.")

        with open(cfg_path) as f:
            cfg = json.load(f)
        gdrive_cfg = cfg.get("gdrive", {})
        client_id = gdrive_cfg.get("client_id")
        client_secret = gdrive_cfg.get("client_secret")
        if not client_id or not client_secret:
            raise RuntimeError("client_id and client_secret must be set in connector config.")

        client_config = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES, redirect_uri=redirect_uri)
        flow.fetch_token(code=auth_code)
        creds = flow.credentials

        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
        print(f"[gdrive] Token saved to {TOKEN_PATH}")

    # ── Folder management ─────────────────────────────────────────────────

    def _find_folder(self, name: str, parent_id: str = None) -> str | None:
        """Find a folder by name under parent. Returns folder ID or None."""
        svc = self._get_service()
        q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent_id:
            q += f" and '{parent_id}' in parents"
        else:
            q += " and 'root' in parents"

        def _do():
            return svc.files().list(q=q, fields="files(id)", pageSize=1).execute()

        result = _retry(_do)
        files = result.get("files", [])
        return files[0]["id"] if files else None

    def _create_folder(self, name: str, parent_id: str = None) -> str:
        """Create a folder and return its ID."""
        svc = self._get_service()
        meta = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            meta["parents"] = [parent_id]

        def _do():
            return svc.files().create(body=meta, fields="id").execute()

        result = _retry(_do)
        return result["id"]

    def _ensure_folder(self, remote_path: str) -> str:
        """Ensure all folders in path exist, return the deepest folder ID.

        remote_path: e.g. "agents/julien/output" — relative to root folder.
        """
        # Ensure root folder (e.g. "OpenJarvis")
        root_key = self._folder_name
        if root_key not in self._folder_cache:
            fid = self._find_folder(self._folder_name)
            if fid is None:
                fid = self._create_folder(self._folder_name)
                print(f"[gdrive] Created root folder '{self._folder_name}'")
            self._folder_cache[root_key] = fid

        parent_id = self._folder_cache[root_key]

        # Walk subfolders
        parts = [p for p in remote_path.split("/") if p]
        # If remote_path includes a filename, walk only the directory parts
        # Caller should pass only the directory portion
        current_path = root_key
        for part in parts:
            current_path = f"{current_path}/{part}"
            if current_path in self._folder_cache:
                parent_id = self._folder_cache[current_path]
                continue
            fid = self._find_folder(part, parent_id)
            if fid is None:
                fid = self._create_folder(part, parent_id)
                print(f"[gdrive] Created folder '{current_path}'")
            self._folder_cache[current_path] = fid
            parent_id = fid

        return parent_id

    # ── File operations ───────────────────────────────────────────────────

    def upload_file(self, local_path: str, remote_path: str) -> bool:
        svc = self._get_service()

        # Ensure parent folders exist
        dir_part = os.path.dirname(remote_path)
        filename = os.path.basename(remote_path)
        parent_id = self._ensure_folder(dir_part) if dir_part else self._ensure_folder("")

        # Check if file already exists (update instead of duplicate)
        q = (
            f"name='{filename}' and '{parent_id}' in parents "
            f"and trashed=false and mimeType!='application/vnd.google-apps.folder'"
        )

        def _list():
            return svc.files().list(q=q, fields="files(id)", pageSize=1).execute()

        existing = _retry(_list).get("files", [])

        media = MediaFileUpload(local_path, resumable=True)

        if existing:
            # Update existing file
            file_id = existing[0]["id"]

            def _update():
                return svc.files().update(fileId=file_id, media_body=media).execute()

            _retry(_update)
        else:
            # Create new file
            meta = {"name": filename, "parents": [parent_id]}

            def _create():
                return svc.files().create(body=meta, media_body=media, fields="id").execute()

            _retry(_create)

        return True

    def download_file(self, remote_path: str, local_path: str) -> bool:
        svc = self._get_service()

        file_id = self._resolve_file_id(remote_path)
        if file_id is None:
            return False

        def _do():
            request = svc.files().get_media(fileId=file_id)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                downloader = MediaIoBaseDownload(f, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            return True

        return _retry(_do)

    def list_dir(self, remote_path: str) -> list:
        svc = self._get_service()

        parent_id = self._ensure_folder(remote_path)

        def _do():
            results = []
            page_token = None
            while True:
                resp = svc.files().list(
                    q=f"'{parent_id}' in parents and trashed=false",
                    fields="nextPageToken, files(name, mimeType)",
                    pageSize=100,
                    pageToken=page_token,
                ).execute()
                for f in resp.get("files", []):
                    name = f["name"]
                    if f["mimeType"] == "application/vnd.google-apps.folder":
                        name += "/"
                    results.append(name)
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
            return results

        return _retry(_do)

    def mkdir(self, remote_path: str) -> bool:
        self._ensure_folder(remote_path)
        return True

    def delete(self, remote_path: str) -> bool:
        svc = self._get_service()

        file_id = self._resolve_file_id(remote_path)
        if file_id is None:
            # Try as folder
            dir_part = remote_path.rstrip("/")
            parts = dir_part.split("/")
            folder_name = parts[-1] if parts else ""
            parent_path = "/".join(parts[:-1])
            parent_id = self._ensure_folder(parent_path) if parent_path else self._ensure_folder("")
            fid = self._find_folder(folder_name, parent_id)
            if fid is None:
                return False
            file_id = fid

        def _do():
            svc.files().delete(fileId=file_id).execute()

        _retry(_do)
        return True

    def file_exists(self, remote_path: str) -> bool:
        return self._resolve_file_id(remote_path) is not None

    # ── Helpers ───────────────────────────────────────────────────────────

    def _resolve_file_id(self, remote_path: str) -> str | None:
        """Resolve a remote_path (relative to root) to a Drive file ID."""
        svc = self._get_service()

        dir_part = os.path.dirname(remote_path)
        filename = os.path.basename(remote_path)
        if not filename:
            return None

        try:
            parent_id = self._ensure_folder(dir_part) if dir_part else self._ensure_folder("")
        except Exception:
            return None

        q = (
            f"name='{filename}' and '{parent_id}' in parents "
            f"and trashed=false and mimeType!='application/vnd.google-apps.folder'"
        )

        def _do():
            return svc.files().list(q=q, fields="files(id)", pageSize=1).execute()

        try:
            result = _retry(_do)
            files = result.get("files", [])
            return files[0]["id"] if files else None
        except Exception:
            return None
