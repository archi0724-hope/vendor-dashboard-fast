"""Runtime wiring for optional Google Drive persistence and Vercel-safe imports.

Python imports ``sitecustomize`` automatically at startup. When Google Drive
credentials are configured, the existing app transparently receives a Drive-
backed Store. For PostgreSQL deployments, Google Drive ZIP imports use a durable
chunked runtime that checkpoints progress after every committed file.
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
        counts["companies"] = int((checklist["Available"] > 0).sum()) if len(checklist) else 0
        return counts

    vendor_core.dashboard_counts = yes_only_counts


def _install_chunked_drive_imports() -> None:
    import vercel_chunked_runtime
    vercel_chunked_runtime.install()


try:
    _install_drive_store()
    _install_yes_only_headcount()
    _install_chunked_drive_imports()
except Exception:
    # Startup must remain available even if an optional runtime patch cannot be
    # installed. The main app still reports storage/configuration errors.
    pass
