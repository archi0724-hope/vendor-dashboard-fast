"""Runtime wiring for optional Google Drive persistence and Vercel-safe imports.

Python imports ``sitecustomize`` automatically at startup. When the Google Drive
environment variables are present, the existing app transparently receives a
Drive-backed Store without hard-coding credentials in GitHub.  Vercel Drive ZIP
imports also receive a resumable PostgreSQL fast path so a long import can be
continued safely after the request/session window is reached.
"""
from __future__ import annotations

import os


def _install_drive_store() -> None:
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    credentials_json = os.environ.get("GOOGLE_DRIVE_CREDENTIALS_JSON", "").strip()
    if not (folder_id and credentials_json):
        return

    import storage
    from drive_store import DriveStore

    class RenderDriveStore(DriveStore):
        def __init__(self, data_dir, database_url=""):
            super().__init__(
                data_dir,
                folder_id=folder_id,
                credentials_json=credentials_json,
                impersonate_user=os.environ.get("GOOGLE_DRIVE_IMPERSONATE_USER", "").strip(),
            )

    storage.Store = RenderDriveStore


def _install_yes_only_headcount() -> None:
    import vendor_core

    original = vendor_core.dashboard_counts

    def yes_only_counts(vendors, documents):
        counts = original(vendors, documents)
        checklist = vendor_core.build_checklist(vendors, documents)
        # A company contributes to head count only when at least one checklist
        # category is Yes. All-No companies remain visible in the checklist but
        # do not inflate head count.
        counts["companies"] = int((checklist["Available"] > 0).sum()) if len(checklist) else 0
        return counts

    vendor_core.dashboard_counts = yes_only_counts


def _install_vercel_resumable_imports() -> None:
    # This patch is harmless on local/Render runs and activates its fast path only
    # for PostgreSQL + Google Drive link imports.
    import vercel_resumable
    vercel_resumable.install()


try:
    _install_drive_store()
    _install_yes_only_headcount()
    _install_vercel_resumable_imports()
except Exception:
    # Do not hide startup errors from the app itself; Store initialization will
    # produce the user-facing configuration error with full logging on the host.
    pass
