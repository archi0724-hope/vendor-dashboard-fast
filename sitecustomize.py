"""Runtime wiring for optional Google Drive persistence.

Python imports ``sitecustomize`` automatically at startup. When the Google Drive
environment variables are present, the existing app transparently receives a
Drive-backed Store without hard-coding credentials in GitHub.
"""
from __future__ import annotations

import os


def _install_drive_store() -> bool:
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    credentials_json = os.environ.get("GOOGLE_DRIVE_CREDENTIALS_JSON", "").strip()
    if not (folder_id and credentials_json):
        return False

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
    return True


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


def _patch_drive_status_messages() -> None:
    """Translate legacy PostgreSQL/local UI text when DriveStore is active.

    The dashboard predates DriveStore and decides its badge from ``store.cloud``,
    which specifically means PostgreSQL. DriveStore intentionally uses SQLite for
    metadata and therefore keeps ``cloud=False`` even though document bytes and
    the metadata snapshot are persisted in Google Drive. Rewriting only these
    exact legacy messages makes the status truthful without changing storage
    semantics.
    """
    import streamlit as st

    original_warning = st.warning
    original_info = st.info
    original_caption = st.caption

    def warning(message, *args, **kwargs):
        if str(message) == "Temporary cloud disk. Set DATABASE_URL for permanent uploads.":
            return st.success("Saved to Google Drive", *args, **kwargs)
        return original_warning(message, *args, **kwargs)

    def info(message, *args, **kwargs):
        if str(message) == "Local data is saved in the vendor_data folder beside app.py. Closing the browser or resetting search does not delete it.":
            return st.success("Google Drive permanent storage is configured. Documents and dashboard metadata are backed up in Drive.", *args, **kwargs)
        return original_info(message, *args, **kwargs)

    def caption(message, *args, **kwargs):
        if str(message) == "For Streamlit Community Cloud, configure DATABASE_URL. Its local disk is not guaranteed to persist.":
            message = "Render local disk is only a working cache; the permanent copy is stored in Google Drive."
        return original_caption(message, *args, **kwargs)

    st.warning = warning
    st.info = info
    st.caption = caption


try:
    drive_active = _install_drive_store()
    _install_yes_only_headcount()
    if drive_active:
        _patch_drive_status_messages()
except Exception:
    # Leave the legacy warning visible if Drive cannot be initialized. This makes
    # a missing/invalid credential obvious instead of falsely claiming persistence.
    pass
