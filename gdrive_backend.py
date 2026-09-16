"""Authenticated Google Drive storage for permanent vendor document persistence.

The Render web service can keep only a disposable local cache. This module stores
vendor document bytes and the small SQLite metadata snapshot in a user-selected
Google Drive folder. Credentials are read from Render environment variables and
are never committed to GitHub.
"""
from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import json
import mimetypes

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"
APP_FOLDER_NAME = "Vendor_Dashboard_Storage"


class GoogleDriveBackend:
    """Small Google Drive API wrapper used by :mod:`drive_store`.

    ``credentials_json`` accepts either a Google service-account JSON document or
    an OAuth authorized-user JSON document containing a refresh token. Service
    accounts can write directly to Shared Drives. For a normal My Drive folder,
    use OAuth user credentials or domain-wide delegation via ``impersonate_user``.
    """

    def __init__(self, folder_id: str, credentials_json: str, impersonate_user: str = ""):
        self.folder_id = str(folder_id).strip()
        if not self.folder_id:
            raise ValueError("GOOGLE_DRIVE_FOLDER_ID is required for Google Drive storage.")
        try:
            info = json.loads(credentials_json)
        except Exception as exc:
            raise ValueError("GOOGLE_DRIVE_CREDENTIALS_JSON is not valid JSON.") from exc

        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError("Google Drive dependencies are not installed. Run pip install -r requirements.txt.") from exc

        credential_type = str(info.get("type", "")).strip().lower()
        if credential_type == "service_account":
            credentials = service_account.Credentials.from_service_account_info(info, scopes=[DRIVE_SCOPE])
            if impersonate_user.strip():
                credentials = credentials.with_subject(impersonate_user.strip())
                self.auth_mode = "service-account delegation"
            else:
                self.auth_mode = "service-account"
        else:
            refresh_token = info.get("refresh_token")
            client_id = info.get("client_id")
            client_secret = info.get("client_secret")
            if not (refresh_token and client_id and client_secret):
                raise ValueError(
                    "Google Drive user credentials must contain refresh_token, client_id and client_secret."
                )
            credentials = Credentials(
                token=info.get("token"),
                refresh_token=refresh_token,
                token_uri=info.get("token_uri", "https://oauth2.googleapis.com/token"),
                client_id=client_id,
                client_secret=client_secret,
                scopes=[DRIVE_SCOPE],
            )
            if not credentials.valid:
                credentials.refresh(Request())
            self.auth_mode = "oauth-user"

        self.service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        root = self.service.files().get(
            fileId=self.folder_id,
            fields="id,name,mimeType,driveId",
            supportsAllDrives=True,
        ).execute()
        if root.get("mimeType") != FOLDER_MIME:
            raise ValueError("GOOGLE_DRIVE_FOLDER_ID must point to a Google Drive folder.")
        if credential_type == "service_account" and not impersonate_user.strip() and not root.get("driveId"):
            raise ValueError(
                "This is a My Drive folder. A service account has no Drive storage quota. "
                "Use OAuth user credentials, domain-wide delegation, or move the folder to a Shared Drive."
            )

        self.root_name = root.get("name", "Google Drive")
        self.app_root_id = self._ensure_folder(APP_FOLDER_NAME, self.folder_id)
        self.documents_id = self._ensure_folder("documents", self.app_root_id)
        self.uploads_id = self._ensure_folder("uploaded_zips", self.app_root_id)
        self.system_id = self._ensure_folder("system", self.app_root_id)
        self.backups_id = self._ensure_folder("metadata_backups", self.app_root_id)

    @staticmethod
    def _escape_query(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace("'", "\\'")

    def _find_child(self, parent_id: str, name: str, mime_type: str | None = None) -> dict | None:
        query = (
            f"'{self._escape_query(parent_id)}' in parents and "
            f"name = '{self._escape_query(name)}' and trashed = false"
        )
        if mime_type:
            query += f" and mimeType = '{self._escape_query(mime_type)}'"
        response = self.service.files().list(
            q=query,
            spaces="drive",
            pageSize=20,
            fields="files(id,name,mimeType,modifiedTime,size,driveId)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute()
        files = response.get("files", [])
        if not files:
            return None
        files.sort(key=lambda item: item.get("modifiedTime", ""), reverse=True)
        return files[0]

    def _ensure_folder(self, name: str, parent_id: str) -> str:
        existing = self._find_child(parent_id, name, FOLDER_MIME)
        if existing:
            return existing["id"]
        created = self.service.files().create(
            body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
            fields="id",
            supportsAllDrives=True,
        ).execute()
        return created["id"]

    @staticmethod
    def _resumable_result(request) -> dict:
        response = None
        while response is None:
            _, response = request.next_chunk(num_retries=5)
        return response

    def _put_bytes(
        self,
        parent_id: str,
        name: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
        app_properties: dict[str, str] | None = None,
        replace_existing: bool = True,
    ) -> str:
        from googleapiclient.http import MediaIoBaseUpload

        existing = self._find_child(parent_id, name)
        if existing and not replace_existing:
            return existing["id"]
        media = MediaIoBaseUpload(
            BytesIO(content),
            mimetype=mime_type or "application/octet-stream",
            chunksize=8 * 1024 * 1024,
            resumable=True,
        )
        if existing:
            request = self.service.files().update(
                fileId=existing["id"],
                body={"appProperties": app_properties or {}},
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            )
        else:
            request = self.service.files().create(
                body={
                    "name": name,
                    "parents": [parent_id],
                    "appProperties": app_properties or {},
                },
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            )
        return self._resumable_result(request)["id"]

    def save_document(self, digest: str, original_name: str, content: bytes) -> str:
        shard = self._ensure_folder(digest[:2], self.documents_id)
        # Hash-based names make retries idempotent and avoid duplicate Drive bytes.
        mime_type = mimetypes.guess_type(original_name)[0] or "application/octet-stream"
        return self._put_bytes(
            shard,
            digest,
            content,
            mime_type,
            {"sha256": digest, "originalName": Path(original_name).name[:120]},
            replace_existing=False,
        )

    def save_upload_archive(self, digest: str, original_name: str, content: bytes) -> str:
        return self._put_bytes(
            self.uploads_id,
            digest + ".zip",
            content,
            "application/zip",
            {"sha256": digest, "originalName": Path(original_name).name[:120]},
            replace_existing=False,
        )

    def download(self, file_id: str) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        output = BytesIO()
        request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
        downloader = MediaIoBaseDownload(output, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=5)
        return output.getvalue()

    def delete(self, file_id: str) -> None:
        from googleapiclient.errors import HttpError

        try:
            self.service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
        except HttpError as exc:
            if getattr(exc, "resp", None) is not None and getattr(exc.resp, "status", None) == 404:
                return
            raise

    def read_metadata(self) -> bytes | None:
        item = self._find_child(self.system_id, "vendor_documents.db")
        return self.download(item["id"]) if item else None

    def write_metadata(self, content: bytes) -> str:
        return self._put_bytes(
            self.system_id,
            "vendor_documents.db",
            content,
            "application/x-sqlite3",
            {"purpose": "vendor-dashboard-metadata"},
            replace_existing=True,
        )

    def write_metadata_backup(self, content: bytes, label: str = "") -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)[:50]
        name = f"vendor_documents_{stamp}{'_' + safe_label if safe_label else ''}.db"
        return self._put_bytes(
            self.backups_id,
            name,
            content,
            "application/x-sqlite3",
            {"purpose": "vendor-dashboard-metadata-backup"},
            replace_existing=False,
        )
