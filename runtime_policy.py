"""Shared import controls and explicit durable-storage requirements."""
from contextlib import contextmanager
import os
from threading import Lock

_IMPORT_LOCK = Lock()


def durable_storage_required():
    return os.environ.get('RENDER', '').lower() == 'true'


def ensure_durable_store(store):
    if durable_storage_required() and not store.cloud:
        raise ValueError('Permanent shared storage is not connected. Configure DATABASE_URL before uploading documents; temporary server files are lost on restarts.')


def document_limit_bytes():
    # Avoid several 128 MB allocations alongside the Streamlit runtime on 512 MB.
    return (32 if durable_storage_required() else 128) * 1024**2


@contextmanager
def import_slot():
    """Only one download/import at a time in the single Render web process."""
    if not _IMPORT_LOCK.acquire(blocking=False):
        raise ValueError('Another import is running. Wait for it to finish, then retry.')
    try:
        yield
    finally:
        _IMPORT_LOCK.release()
