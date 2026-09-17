"""Vercel-safe Google Drive imports.

The Streamlit app can stay connected to PostgreSQL while a large Drive ZIP is
processed.  The original importer opens a fresh database connection for every
file; on Vercel that can consume most of the five-minute request/session window.

This module installs two narrow runtime patches:
1. annotate Drive downloads with their original source URL and action start time;
2. use one PostgreSQL connection for the Drive import, commit each file, and
   checkpoint progress in ``vdd_meta`` so the same Drive link resumes after an
   interrupted Vercel session instead of starting from file 1 again.

Local SQLite and Google-Drive-backed Store behaviour is intentionally unchanged.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import time

import pandas as pd

import drive_import
import import_service
from import_service import ImportResult
from vendor_core import (
    ArchiveLimits,
    build_checklist,
    classify,
    clean_company,
    company_key,
    discover_folder_companies,
    file_basename,
    iter_uploads,
)

# Keep one import action comfortably below Vercel's long-lived request/session
# ceiling.  Reusing one PostgreSQL connection makes a normal batch much faster;
# if a very large batch still needs another pass, the durable checkpoint resumes
# it from the last committed file.
MAX_ACTION_SECONDS = 125.0
_PATCHED = False
_ORIGINAL_DOWNLOAD = None
_ORIGINAL_IMPORT = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _source_hash(source_url: str) -> str:
    return hashlib.sha256(source_url.strip().encode("utf-8")).hexdigest()


def _resume_key(source_url: str) -> str:
    return "drive_import_resume:" + _source_hash(source_url)[:32]


def _source_key(source_url: str) -> str:
    return "drive_import_source:" + _source_hash(source_url)[:32]


def _read_meta(store, key: str) -> dict:
    try:
        with store.connection() as db:
            row = db.execute("SELECT value FROM vdd_meta WHERE key=?", (key,)).fetchone()
        if not row:
            return {}
        value = json.loads(row["value"])
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write_meta(db, key: str, value: dict) -> None:
    db.execute(
        "INSERT INTO vdd_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def _delete_meta(db, key: str) -> None:
    db.execute("DELETE FROM vdd_meta WHERE key=?", (key,))


def _save_cloud_document(store, db, path: str, content: bytes, decision, source_batch: str) -> bool:
    """Save one PostgreSQL document using an already-open connection."""
    if not content:
        raise ValueError("Empty files are not saved.")

    name = decision.company_name or ""
    key = company_key(name) if name else ""
    digest = hashlib.sha256(content).hexdigest()
    existing = db.execute(
        "SELECT id,reviewed,types_json FROM vdd_documents WHERE company_key=? AND file_hash=?",
        (key, digest),
    ).fetchone()

    if existing:
        if not bool(existing["reviewed"]):
            types = list(dict.fromkeys(json.loads(existing["types_json"]) + list(decision.document_types)))
            db.execute(
                "UPDATE vdd_documents SET types_json=?,method=?,reason=? WHERE id=?",
                (json.dumps(types), decision.method, decision.reason, existing["id"]),
            )
        return False

    if name:
        store._upsert_vendor(db, name)

    cursor = db.execute(
        """INSERT INTO vdd_documents(company_key,company_name,types_json,filename,original_path,
        stored_path,file_hash,uploaded_at,source_batch,handover,method,reason,size_bytes,payload)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(company_key,file_hash) DO NOTHING""",
        (
            key,
            name,
            json.dumps(decision.document_types),
            file_basename(path),
            path,
            "",
            digest,
            _now(),
            source_batch,
            decision.handover,
            decision.method,
            decision.reason,
            len(content),
            content,
        ),
    )
    return cursor.rowcount > 0


def _save_cloud_with_retry(store, db, path: str, content: bytes, decision, source_batch: str, attempts: int = 3) -> bool:
    last_error = None
    for attempt in range(attempts):
        try:
            added = _save_cloud_document(store, db, path, content, decision, source_batch)
            db.connection.commit()
            return added
        except Exception as error:
            last_error = error
            try:
                db.connection.rollback()
            except Exception:
                pass
            if attempt + 1 < attempts:
                time.sleep(0.35 * (2 ** attempt))
    raise last_error


def _record_unassigned(db, source: str, path: str, reason: str) -> None:
    db.execute(
        """INSERT INTO vdd_review_queue(
        detected_name,possible_company,canonical_id,source,record,confidence,evidence,created_at)
        VALUES(?,?,?,?,?,?,?,?)""",
        ("", "", "", source, path, 0, reason, _now()),
    )


def _cloud_drive_import(store, uploads, forced_company: str = "", read_pdf_text: bool = False,
                        progress=None, retain_archive: bool = True) -> ImportResult:
    uploads = list(uploads)
    upload = uploads[0]
    source_url = str(getattr(upload, "source_url", "")).strip()
    result = ImportResult(source=source_url or upload.name)

    names = store.matching_companies()
    detected = set(discover_folder_companies(uploads)) if not forced_company.strip() else {forced_company.strip()}
    if detected:
        store.upsert_vendors(pd.DataFrame({"company_name": sorted(detected, key=str.casefold)}))
    for name in detected:
        names.setdefault(name, name)

    resume_key = _resume_key(source_url)
    source_key = _source_key(source_url)
    resume_state = _read_meta(store, resume_key)
    resume_index = int(resume_state.get("processed_index", 0) or 0)
    if resume_index:
        result.notes.append(
            f"Resuming this Google Drive ZIP after file {resume_index}. Already committed files are not inserted again."
        )

    started = float(getattr(upload, "download_started_at", time.monotonic()))
    deadline = started + MAX_ACTION_SECONDS
    limits = ArchiveLimits()
    last_checkpoint = resume_index
    yielded_index = 0
    paused = False
    stopped_on_error = False

    try:
        with store.connection() as db:
            _write_meta(
                db,
                source_key,
                {
                    "source_url": source_url,
                    "filename": getattr(upload, "name", "Google_Drive_Import.zip"),
                    "status": "running",
                    "processed_index": resume_index,
                    "updated_at": _now(),
                },
            )
            db.connection.commit()

            for path, content, source in iter_uploads(uploads, limits):
                yielded_index += 1

                # A hard-killed Vercel session may have already committed this prefix.
                # The ZIP reader still walks the entries, but no classification/database
                # work is repeated for checkpointed files.
                if yielded_index <= resume_index:
                    content = b""
                    continue

                if time.monotonic() >= deadline:
                    paused = True
                    content = b""
                    break

                result.processed_files += 1
                if progress:
                    progress(yielded_index, path)

                try:
                    decision = classify(path, names, content, forced_company, read_pdf_text)
                    if decision.company_name is None:
                        _record_unassigned(db, source_url or source, path, decision.reason)

                    added = _save_cloud_with_retry(
                        store,
                        db,
                        path,
                        content,
                        decision,
                        source_url or source,
                    )
                    result.saved_files += int(added)
                    result.duplicate_files += int(not added)
                    result.review_files += int(added and decision.needs_review)

                    if decision.company_name:
                        detected.add(decision.company_name)
                        names.setdefault(decision.company_name, decision.company_name)

                    last_checkpoint = yielded_index
                    checkpoint = {
                        "source_url": source_url,
                        "processed_index": last_checkpoint,
                        "updated_at": _now(),
                    }
                    _write_meta(db, resume_key, checkpoint)
                    _write_meta(
                        db,
                        source_key,
                        {
                            "source_url": source_url,
                            "filename": getattr(upload, "name", "Google_Drive_Import.zip"),
                            "status": "running",
                            "processed_index": last_checkpoint,
                            "updated_at": _now(),
                        },
                    )
                    db.connection.commit()
                except Exception as error:
                    try:
                        db.connection.rollback()
                    except Exception:
                        pass
                    result.issues.append(
                        {
                            "File": path,
                            "Reason": (
                                f"Not saved after retry: {type(error).__name__}. "
                                "This file was not checkpointed; retry the same Drive link."
                            ),
                        }
                    )
                    stopped_on_error = True
                    content = b""
                    break
                finally:
                    content = b""
                    if result.processed_files and result.processed_files % 5 == 0:
                        import_service._release_memory()

            completed = not paused and not stopped_on_error and yielded_index >= last_checkpoint
            if completed:
                _delete_meta(db, resume_key)
                _write_meta(
                    db,
                    source_key,
                    {
                        "source_url": source_url,
                        "filename": getattr(upload, "name", "Google_Drive_Import.zip"),
                        "status": "complete",
                        "processed_index": yielded_index,
                        "updated_at": _now(),
                    },
                )
                db.connection.commit()
    except Exception as error:
        result.issues.append(
            {
                "File": getattr(upload, "name", "Google_Drive_Import.zip"),
                "Reason": (
                    f"Stopped: {str(error) if isinstance(error, ValueError) else type(error).__name__}. "
                    "Already committed documents are safe; retry the same Drive link."
                ),
            }
        )
        stopped_on_error = True

    result.issues.extend(limits.skipped)
    result.company_names = sorted(
        {company_key(name): clean_company(name) for name in detected if company_key(name)}.values(),
        key=str.casefold,
    )
    result.detected_companies = len(result.company_names)

    vendors = store.vendors()
    documents = store.documents()
    checklist = build_checklist(vendors, documents)
    result.total_companies = int((checklist["Available"] > 0).sum()) if len(checklist) else 0
    result.total_stored_files = int(documents.available.sum())

    if paused:
        result.notes.append(
            "Import paused safely before the Vercel session limit. All documents shown in the dashboard are already "
            f"committed to PostgreSQL through file {last_checkpoint}. Refresh the page, paste the same Google Drive "
            "ZIP link, and press Save documents again; it resumes from the checkpoint instead of starting over."
        )
    elif stopped_on_error:
        result.notes.append(
            f"Import stopped safely at file {last_checkpoint}. Retry the same Google Drive ZIP link; the next run resumes there."
        )
    else:
        result.notes.append(
            "Google Drive ZIP import complete. The original ZIP remains in Google Drive; its source URL is recorded "
            "in dashboard upload history, and the extracted documents are permanently stored in PostgreSQL."
        )

    # The normal dashboard reads this audit entry for Last upload summary.  This
    # also gives the team a durable copy of the original Drive source URL.
    store.log_event("Document upload", result.to_dict())
    import_service._release_memory()
    return result


def _patched_import_documents(store, uploads, forced_company: str = "", read_pdf_text: bool = False,
                              progress=None, retain_archive: bool = True) -> ImportResult:
    items = list(uploads)
    is_drive_source = bool(items) and all(str(getattr(item, "source_url", "")).strip() for item in items)
    if getattr(store, "cloud", False) and is_drive_source:
        return _cloud_drive_import(store, items, forced_company, read_pdf_text, progress, retain_archive)
    return _ORIGINAL_IMPORT(store, items, forced_company, read_pdf_text, progress, retain_archive)


@contextmanager
def _patched_download_drive_zip(link, *args, **kwargs):
    started = time.monotonic()
    with _ORIGINAL_DOWNLOAD(link, *args, **kwargs) as upload:
        # DiskUpload is a normal Python object, so these attributes survive until
        # app.py hands it to import_documents in the same context manager.
        upload.source_url = str(link).strip()
        upload.download_started_at = started
        yield upload


def install() -> None:
    global _PATCHED, _ORIGINAL_DOWNLOAD, _ORIGINAL_IMPORT
    if _PATCHED:
        return
    _ORIGINAL_DOWNLOAD = drive_import.download_drive_zip
    _ORIGINAL_IMPORT = import_service.import_documents
    drive_import.download_drive_zip = _patched_download_drive_zip
    import_service.import_documents = _patched_import_documents
    _PATCHED = True
