"""Runtime wiring for optional Google Drive persistence and Vercel-safe imports.

Python imports ``sitecustomize`` automatically at startup. When the Google Drive
environment variables are present, the existing app transparently receives a
Drive-backed Store without hard-coding credentials in GitHub. Vercel Drive ZIP
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
        counts["companies"] = int((checklist["Available"] > 0).sum()) if len(checklist) else 0
        return counts

    vendor_core.dashboard_counts = yes_only_counts


def _install_vercel_resumable_imports() -> None:
    import vercel_resumable

    # The current Vercel container/session is ending at roughly two minutes for
    # this Streamlit workload. Stop well before that boundary so the app can
    # commit a checkpoint, render a success/paused message, and return normally.
    # The same Drive link then resumes from the saved ZIP entry.
    vercel_resumable.MAX_ACTION_SECONDS = 60.0
    vercel_resumable.install()


try:
    _install_drive_store()
    _install_yes_only_headcount()
    _install_vercel_resumable_imports()
except Exception:
    # Do not block the dashboard during startup. The app itself reports storage
    # configuration errors; import retries remain safe because PostgreSQL commits
    # each successfully stored document independently.
    pass
