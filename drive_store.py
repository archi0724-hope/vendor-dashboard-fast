"""Google Drive-backed Store for Render.

Large document bytes live in Google Drive. The small SQLite metadata database is
mirrored to Drive after each committed mutation, so Render's ephemeral disk is
only a cache and can be rebuilt after a restart or redeploy.
"""
from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile

import pandas as pd

from gdrive_backend import GoogleDriveBackend
from storage import Store, Query, DOC_COLUMNS, now
from vendor_core import clean_company, company_key, file_basename


class DriveStore(Store):
    def __init__(self, data_dir: Path, folder_id: str, credentials_json: str, impersonate_user: str = ""):
        self.data_dir = Path(data_dir).resolve()
        self.files_dir = self.data_dir / "files"
        self.uploads_dir = self.data_dir / "uploaded_zips"
        self.db_path = self.data_dir / "vendor_documents.db"
        self.database_url = ""
        self.cloud = False
        self.drive = GoogleDriveBackend(folder_id, credentials_json, impersonate_user)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)

        remote_db = self.drive.read_metadata()
        if remote_db:
            with tempfile.NamedTemporaryFile(dir=self.data_dir, delete=False) as temp:
                temp.write(remote_db)
                temp_path = Path(temp.name)
            os.replace(temp_path, self.db_path)
        super().initialize()
        self._sync_metadata()

    @property
    def drive_enabled(self) -> bool:
        return True

    def _sync_metadata(self) -> None:
        if not self.db_path.exists():
            return
        self.drive.write_metadata(self.db_path.read_bytes())

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        before = connection.total_changes
        try:
            yield Query(connection, False)
            connection.commit()
            changed = connection.total_changes > before
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if changed:
            self._sync_metadata()

    def documents(self) -> pd.DataFrame:
        with self.connection() as db:
            cols = ", ".join(DOC_COLUMNS)
            rows = [dict(row) for row in db.execute(
                f"SELECT {cols}, CASE WHEN payload IS NOT NULL THEN 1 ELSE 0 END AS has_payload "
                "FROM vdd_documents ORDER BY lower(company_name), filename"
            ).fetchall()]
        frame = pd.DataFrame(rows, columns=DOC_COLUMNS + ["has_payload"])
        frame["types"] = frame.types_json.map(json.loads)
        frame["available"] = frame.stored_path.map(lambda value: str(value).startswith("drive:") and len(str(value)) > 6)
        frame["needs_review"] = frame.apply(
            lambda row: not row.company_key or (not row.types and not bool(row.reviewed) and row.method != "Supporting document"),
            axis=1,
        ) if len(frame) else pd.Series(dtype=bool)
        return frame

    def save_document(self, original_path: str, content: bytes, decision, source_batch: str = "") -> bool:
        if not content:
            raise ValueError("Empty files are not saved.")
        name = decision.company_name or ""
        key = company_key(name) if name else ""
        digest = hashlib.sha256(content).hexdigest()
        with self.connection() as db:
            existing = db.execute(
                "SELECT id,stored_path,reviewed,types_json FROM vdd_documents WHERE company_key=? AND file_hash=?",
                (key, digest),
            ).fetchone()
            if existing:
                if not bool(existing["reviewed"]):
                    types = list(dict.fromkeys(json.loads(existing["types_json"]) + list(decision.document_types)))
                    db.execute(
                        "UPDATE vdd_documents SET types_json=?,method=?,reason=? WHERE id=?",
                        (json.dumps(types), decision.method, decision.reason, existing["id"]),
                    )
                if not str(existing["stored_path"]).startswith("drive:"):
                    file_id = self.drive.save_document(digest, file_basename(original_path), content)
                    db.execute(
                        "UPDATE vdd_documents SET stored_path=?,size_bytes=? WHERE id=?",
                        ("drive:" + file_id, len(content), existing["id"]),
                    )
                return False
            if name:
                self._upsert_vendor(db, name)
            file_id = self.drive.save_document(digest, file_basename(original_path), content)
            cursor = db.execute(
                """INSERT INTO vdd_documents(company_key,company_name,types_json,filename,original_path,
                stored_path,file_hash,uploaded_at,source_batch,handover,method,reason,size_bytes,payload)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(company_key,file_hash) DO NOTHING""",
                (
                    key, name, json.dumps(decision.document_types), file_basename(original_path), original_path,
                    "drive:" + file_id, digest, now(), source_batch, decision.handover, decision.method,
                    decision.reason, len(content), None,
                ),
            )
            return cursor.rowcount > 0

    def read_bytes(self, document_id: int) -> bytes | None:
        with self.connection() as db:
            row = db.execute("SELECT stored_path FROM vdd_documents WHERE id=?", (int(document_id),)).fetchone()
        if not row:
            return None
        stored = str(row["stored_path"] or "")
        if not stored.startswith("drive:"):
            return None
        return self.drive.download(stored[6:])

    def save_upload_archive(self, filename: str, content: bytes, company_names: list[str], processed_files: int, issues_count: int = 0) -> dict:
        if not content or not zipfile.is_zipfile(BytesIO(content)):
            raise ValueError("Uploaded archive is not a readable ZIP file.")
        digest = hashlib.sha256(content).hexdigest()
        archive_id = digest[:20]
        clean_name = file_basename(filename) or "Vendor_Upload.zip"
        timestamp = now()
        unique_names = sorted(
            {company_key(name): clean_company(name) for name in company_names if company_key(name)}.values(),
            key=str.casefold,
        )
        with self.connection() as db:
            existing = db.execute(
                "SELECT id,upload_count,stored_path FROM vdd_upload_archives WHERE file_hash=?", (digest,)
            ).fetchone()
            if existing and str(existing["stored_path"] or "").startswith("drive:"):
                stored = existing["stored_path"]
            else:
                file_id = self.drive.save_upload_archive(digest, clean_name, content)
                stored = "drive:" + file_id
            if existing:
                db.execute(
                    """UPDATE vdd_upload_archives SET filename=?,last_uploaded_at=?,upload_count=?,size_bytes=?,
                    detected_companies=?,company_names_json=?,processed_files=?,issues_count=?,stored_path=?,payload=NULL WHERE file_hash=?""",
                    (
                        clean_name, timestamp, int(existing["upload_count"]) + 1, len(content), len(unique_names),
                        json.dumps(unique_names, ensure_ascii=False), int(processed_files), int(issues_count), stored, digest,
                    ),
                )
                added = False
            else:
                db.execute(
                    """INSERT INTO vdd_upload_archives(id,file_hash,filename,first_uploaded_at,last_uploaded_at,upload_count,size_bytes,
                    detected_companies,company_names_json,processed_files,issues_count,stored_path,payload)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        archive_id, digest, clean_name, timestamp, timestamp, 1, len(content), len(unique_names),
                        json.dumps(unique_names, ensure_ascii=False), int(processed_files), int(issues_count), stored, None,
                    ),
                )
                added = True
        return {"id": archive_id, "added": added, "filename": clean_name, "companies": len(unique_names), "processed_files": int(processed_files)}

    def upload_archives(self) -> pd.DataFrame:
        frame = super().upload_archives()
        if len(frame):
            frame["available"] = frame.stored_path.map(lambda value: str(value).startswith("drive:") and len(str(value)) > 6)
        return frame

    def read_upload_archive(self, archive_id: str) -> bytes | None:
        with self.connection() as db:
            row = db.execute("SELECT stored_path FROM vdd_upload_archives WHERE id=?", (archive_id,)).fetchone()
        if row is None:
            return None
        stored = str(row["stored_path"] or "")
        return self.drive.download(stored[6:]) if stored.startswith("drive:") else None

    def _snapshot(self, db) -> bytes:
        vendors = [dict(row) for row in db.execute("SELECT * FROM vdd_vendors ORDER BY company_key").fetchall()]
        aliases = [dict(row) for row in db.execute("SELECT * FROM vdd_company_aliases ORDER BY alias_key").fetchall()]
        documents = [dict(row) for row in db.execute("SELECT * FROM vdd_documents ORDER BY id").fetchall()]
        uploads = [dict(row) for row in db.execute("SELECT * FROM vdd_upload_archives ORDER BY last_uploaded_at").fetchall()]
        manifest = {"format": "vendor-dashboard-backup-v3", "created_at": now(), "vendors": vendors, "aliases": aliases, "documents": [], "uploads": []}
        output = BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            written = set()
            for row in documents:
                row.pop("payload", None)
                stored = str(row.get("stored_path", ""))
                payload = self.drive.download(stored[6:]) if stored.startswith("drive:") else None
                if payload is None or hashlib.sha256(payload).hexdigest() != row["file_hash"]:
                    raise ValueError("A Google Drive document is unavailable or failed its integrity check.")
                member = "files/" + row["file_hash"]
                if member not in written:
                    archive.writestr(member, payload)
                    written.add(member)
                row["backup_member"] = member
                manifest["documents"].append(row)
            for row in uploads:
                row.pop("payload", None)
                stored = str(row.get("stored_path", ""))
                payload = self.drive.download(stored[6:]) if stored.startswith("drive:") else None
                if payload is None or hashlib.sha256(payload).hexdigest() != row["file_hash"]:
                    raise ValueError("A Google Drive ZIP is unavailable or failed its integrity check.")
                member = "uploads/" + row["file_hash"] + ".zip"
                archive.writestr(member, payload)
                row["backup_member"] = member
                manifest["uploads"].append(row)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        return output.getvalue()

    def reset_data(self, confirmation: str):
        if confirmation not in {"RESET HISTORY", "RESET DATA"}:
            raise ValueError("Type RESET HISTORY exactly to confirm.")
        with self.connection() as db:
            document_ids = [str(row["stored_path"])[6:] for row in db.execute("SELECT stored_path FROM vdd_documents").fetchall() if str(row["stored_path"] or "").startswith("drive:")]
            upload_ids = [str(row["stored_path"])[6:] for row in db.execute("SELECT stored_path FROM vdd_upload_archives").fetchall() if str(row["stored_path"] or "").startswith("drive:")]
        for file_id in document_ids + upload_ids:
            self.drive.delete(file_id)
        result = super().reset_data(confirmation)
        self._sync_metadata()
        return result
