from io import BytesIO
import pytest
from runtime_policy import import_slot, ensure_durable_store, document_limit_bytes
from import_service import import_documents
from storage import Store
from vendor_core import classify


def test_render_requires_database_before_any_import(tmp_path, monkeypatch):
    monkeypatch.setenv('RENDER', 'true')
    store = Store(tmp_path)
    with pytest.raises(ValueError, match='DATABASE_URL'):
        import_documents(store, [])
    assert store.vendors().empty and store.documents().empty


def test_cloud_store_is_accepted(monkeypatch):
    monkeypatch.setenv('RENDER', 'true')
    class CloudStore: cloud = True
    ensure_durable_store(CloudStore())
    assert document_limit_bytes() == 32 * 1024**2


def test_local_limit(monkeypatch):
    monkeypatch.delenv('RENDER', raising=False)
    assert document_limit_bytes() == 128 * 1024**2


def test_import_lock_rejects_concurrent_work_and_releases_after_failure():
    with pytest.raises(RuntimeError):
        with import_slot():
            with pytest.raises(ValueError, match='Another import'):
                with import_slot(): pytest.fail('concurrent import allowed')
            raise RuntimeError('interrupted import')
    with import_slot(): pass


def test_new_store_reads_previously_saved_documents(tmp_path):
    first = Store(tmp_path)
    first.save_document('Alpha Medical/GST.txt', b'shared fixture', classify('Alpha Medical/GST.txt'))
    second = Store(tmp_path)
    assert second.vendors().equals(first.vendors())
    doc = second.documents().iloc[0]
    assert second.read_bytes(int(doc.id)) == b'shared fixture'
