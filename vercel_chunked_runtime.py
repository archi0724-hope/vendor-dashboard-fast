"""Durable chunked Google Drive ZIP imports for Vercel + PostgreSQL.

This runtime is loaded before Streamlit executes app.py. It replaces only the
Google-Drive-link import path when the active Store uses PostgreSQL. Browser
uploads, local SQLite and DriveStore behaviour remain unchanged.

The important production properties are:
- use HTTP range reads for Drive ZIPs when possible, so every continuation does
  not download the whole archive again;
- skip checkpointed ZIP members without opening/decompressing their payloads;
- process a small bounded batch and commit every document independently;
- persist the exact ZIP-member checkpoint in vdd_meta after each committed item;
- retrying the same Drive link continues after the checkpoint.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import urlencode, urljoin
import hashlib
import json
import re
import stat
import time
import zipfile

import pandas as pd
import requests

import drive_import
import import_service
from import_service import ImportResult
from vendor_core import (
    ALLOWED_EXTENSIONS,
    build_checklist,
    classify,
    clean_company,
    company_key,
    discover_folder_companies,
    file_basename,
    natural_key,
    path_parts,
)

BATCH_FILES = 30
MAX_PROCESS_SECONDS = 42.0
MAX_ENTRIES = 5000
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024 * 1024
_PATCHED = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _source_hash(source_url: str) -> str:
    return hashlib.sha256(source_url.strip().encode("utf-8")).hexdigest()


def _resume_key(source_url: str) -> str:
    return "drive_chunk_resume:" + _source_hash(source_url)[:32]


def _source_key(source_url: str) -> str:
    return "drive_chunk_source:" + _source_hash(source_url)[:32]


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


def _true_original_download():
    # sitecustomize may already have installed the older runtime patch before
    # this module is imported. Prefer its captured original in that case.
    try:
        import vercel_resumable
        original = getattr(vercel_resumable, "_ORIGINAL_DOWNLOAD", None)
        if original:
            return original
    except Exception:
        pass
    return drive_import.download_drive_zip


def _true_original_import():
    try:
        import vercel_resumable
        original = getattr(vercel_resumable, "_ORIGINAL_IMPORT", None)
        if original:
            return original
    except Exception:
        pass
    return import_service.import_documents


_ORIGINAL_DOWNLOAD = _true_original_download()
_ORIGINAL_IMPORT = _true_original_import()


@contextmanager
def _ranged_drive_zip(link, progress=None, max_bytes=MAX_DOWNLOAD_BYTES):
    """Prefer a seekable HTTP-range Drive ZIP, with the old downloader as fallback."""
    source_url = str(link).strip()
    file_id = drive_import.drive_file_id(source_url)
    url = "https://drive.usercontent.google.com/download?" + urlencode({
        "id": file_id,
        "export": "download",
        "confirm": "t",
    })

    session = requests.Session()
    stream = None
    try:
        for attempt in range(2):
            response = drive_import.open_response(session, url)
            try:
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/html" in content_type:
                    html = response.raw.read(1024**2 + 1, decode_content=True)
                    if len(html) > 1024**2:
                        raise ValueError("Drive returned a web page instead of a ZIP.")
                    form = drive_import.ConfirmationForm()
                    form.feed(html.decode("utf-8", errors="replace"))
                    if attempt or not form.action:
                        raise ValueError(
                            "Drive did not return a ZIP. Check download permissions or quota; "
                            "sign-in-only links cannot be imported."
                        )
                    action = urljoin(url, form.action)
                    drive_import.validate_url(action)
                    url = action + ("&" if "?" in action else "?") + urlencode(form.fields)
                    continue

                length = response.headers.get("Content-Length", "")
                total = int(length) if length.isdigit() else 0
                if total > max_bytes:
                    raise ValueError("Drive ZIP exceeds the 5 GB download limit.")
                if total <= 0:
                    break

                range_url = getattr(response, "_vendor_download_url", url)
                response.close()
                stream = drive_import.DriveRangeFile(session, range_url, total, progress=progress)
                try:
                    if not zipfile.is_zipfile(stream):
                        raise ValueError("The Google Drive file is not a readable ZIP.")
                    stream.seek(0)
                    upload = drive_import.DiskUpload(stream)
                    upload.source_url = source_url
                    upload.download_started_at = time.monotonic()
                    upload.total_bytes = total
                    upload.range_backed = True
                    yield upload
                    return
                finally:
                    if stream is not None:
                        stream.close()
                        stream = None
            finally:
                try:
                    response.close()
                except Exception:
                    pass

        # Some Drive responses do not provide a usable size/range endpoint. Keep
        # the existing downloader as a compatibility fallback.
        with _ORIGINAL_DOWNLOAD(source_url, progress=progress, max_bytes=max_bytes) as upload:
            upload.source_url = source_url
            upload.download_started_at = time.monotonic()
            upload.range_backed = False
            yield upload
    finally:
        if stream is not None:
            stream.close()
        session.close()


def _usable_member(member: zipfile.ZipInfo) -> tuple[bool, str]:
    try:
        parts = path_parts(member.filename)
    except ValueError:
        return False, "Unsafe path"
    if not parts or member.is_dir():
        return False, "Directory"
    if any(part.startswith((".", "__MACOSX")) for part in parts):
        return False, "Hidden metadata"
    if parts[-1].lower() in {"thumbs.db", "desktop.ini"}:
        return False, "System metadata"
    if stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1:
        return False, "Symlink/encrypted ZIP entry"
    ext = PurePosixPath(parts[-1]).suffix.lower()
    if ext == ".zip":
        return False, "Nested ZIP - upload separately"
    if ext not in ALLOWED_EXTENSIONS:
        return False, "Unsupported file type"
    if member.file_size <= 0:
        return False, "Empty file"
    if member.file_size > MAX_FILE_BYTES:
        return False, "File over 128 MB"
    if member.compress_size > 0 and member.file_size / member.compress_size > 1000:
        return False, "Unsafe compression ratio"
    return True, ""


def _save_cloud_document(store, db, path: str, content: bytes, decision, source_batch: str) -> bool:
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


def _record_unassigned(db, source: str, path: str, reason: str) -> None:
    db.execute(
        """INSERT INTO vdd_review_queue(
        detected_name,possible_company,canonical_id,source,record,confidence,evidence,created_at)
        VALUES(?,?,?,?,?,?,?,?)""",
        ("", "", "", source, path, 0, reason, _now()),
    )


def _chunked_import(store, uploads, forced_company: str = "", read_pdf_text: bool = False,
                    progress=None, retain_archive: bool = True) -> ImportResult:
    uploads = list(uploads)
    if not uploads:
        return _ORIGINAL_IMPORT(store, uploads, forced_company, read_pdf_text, progress, retain_archive)
    upload = uploads[0]
    source_url = str(getattr(upload, "source_url", "")).strip()
    if not source_url or not getattr(store, "cloud", False):
        return _ORIGINAL_IMPORT(store, uploads, forced_company, read_pdf_text, progress, retain_archive)

    result = ImportResult(source=source_url)
    resume_key = _resume_key(source_url)
    source_key = _source_key(source_url)
    resume = _read_meta(store, resume_key)
    resume_position = int(resume.get("member_position", 0) or 0)

    names = store.matching_companies()
    try:
        detected = set(discover_folder_companies([upload])) if not forced_company.strip() else {forced_company.strip()}
    except Exception:
        detected = {forced_company.strip()} if forced_company.strip() else set()
    if detected:
        store.upsert_vendors(pd.DataFrame({"company_name": sorted(detected, key=str.casefold)}))
    for name in detected:
        names.setdefault(name, name)

    try:
        upload.seek(0)
        with zipfile.ZipFile(upload) as archive:
            all_members = sorted(archive.infolist(), key=lambda m: natural_key(m.filename))
            if len(all_members) > MAX_ENTRIES:
                raise ValueError(f"More than {MAX_ENTRIES:,} ZIP entries. Split the source ZIP into smaller archives.")

            valid: list[tuple[zipfile.ZipInfo, str]] = []
            skipped_static: list[dict] = []
            for member in all_members:
                usable, reason = _usable_member(member)
                if usable:
                    valid.append((member, "/".join(path_parts(member.filename))))
                elif reason not in {"Directory", "Hidden metadata", "System metadata"}:
                    skipped_static.append({"File": member.filename, "Reason": reason})

            total = len(valid)
            if resume_position > total:
                resume_position = 0
            if resume_position:
                result.notes.append(
                    f"Resuming the same Google Drive ZIP after {resume_position:,} of {total:,} processable files."
                )

            started = time.monotonic()
            completed_position = resume_position
            batch_done = 0
            hard_error = None

            with store.connection() as db:
                _write_meta(db, source_key, {
                    "source_url": source_url,
                    "status": "running",
                    "member_position": resume_position,
                    "total_files": total,
                    "updated_at": _now(),
                })
                db.connection.commit()

                for position, (member, path) in enumerate(valid, start=1):
                    if position <= resume_position:
                        continue
                    if batch_done >= BATCH_FILES or time.monotonic() - started >= MAX_PROCESS_SECONDS:
                        break

                    result.processed_files += 1
                    batch_done += 1
                    if progress:
                        progress(position, path)

                    try:
                        with archive.open(member) as source:
                            payload = source.read(MAX_FILE_BYTES + 1)
                        if not payload or len(payload) > MAX_FILE_BYTES:
                            result.issues.append({"File": path, "Reason": "Empty / over 128 MB"})
                            completed_position = position
                        else:
                            decision = classify(path, names, payload, forced_company, read_pdf_text)
                            if decision.company_name is None:
                                _record_unassigned(db, source_url, path, decision.reason)
                            added = _save_cloud_document(store, db, path, payload, decision, source_url)
                            result.saved_files += int(added)
                            result.duplicate_files += int(not added)
                            result.review_files += int(added and decision.needs_review)
                            if decision.company_name:
                                detected.add(decision.company_name)
                                names.setdefault(decision.company_name, decision.company_name)
                            completed_position = position

                        checkpoint = {
                            "source_url": source_url,
                            "member_position": completed_position,
                            "total_files": total,
                            "updated_at": _now(),
                        }
                        _write_meta(db, resume_key, checkpoint)
                        _write_meta(db, source_key, {**checkpoint, "status": "running"})
                        db.connection.commit()
                    except Exception as error:
                        try:
                            db.connection.rollback()
                        except Exception:
                            pass
                        hard_error = error
                        result.issues.append({
                            "File": path,
                            "Reason": f"Import stopped before this file was checkpointed: {type(error).__name__}. Retry the same Drive link.",
                        })
                        break

                complete = completed_position >= total and hard_error is None
                if complete:
                    _delete_meta(db, resume_key)
                    _write_meta(db, source_key, {
                        "source_url": source_url,
                        "status": "complete",
                        "member_position": total,
                        "total_files": total,
                        "updated_at": _now(),
                    })
                    db.connection.commit()
                else:
                    _write_meta(db, source_key, {
                        "source_url": source_url,
                        "status": "paused" if hard_error is None else "error",
                        "member_position": completed_position,
                        "total_files": total,
                        "updated_at": _now(),
                    })
                    db.connection.commit()

            result.issues.extend(skipped_static)
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

            if complete:
                result.notes.append(
                    f"Import complete: {total:,} processable ZIP files reached the final checkpoint. "
                    "Exact repeats were skipped and saved documents remain in PostgreSQL."
                )
            else:
                result.notes.append(
                    f"Import checkpoint saved at {completed_position:,} / {total:,} processable files. "
                    "Paste the same Google Drive ZIP link and press Save documents again to continue from the next file."
                )
    except Exception as error:
        result.issues.append({
            "File": getattr(upload, "name", "Google_Drive_Import.zip"),
            "Reason": str(error) if isinstance(error, ValueError) else f"ZIP import error: {type(error).__name__}",
        })
        result.notes.append(
            "Already committed documents are safe in PostgreSQL. Retry the same Google Drive ZIP link."
        )

    try:
        store.log_event("Document upload", result.to_dict())
    except Exception:
        pass
    return result


def install() -> None:
    global _PATCHED
    if _PATCHED:
        return
    drive_import.download_drive_zip = _ranged_drive_zip
    import_service.import_documents = _chunked_import
    _PATCHED = True
