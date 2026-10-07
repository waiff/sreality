"""Note attachments (migration 592): what a note may carry, how it is stored, read back and
removed. Hermetic: the tenant connection and R2 are faked; RLS and the account trigger are the
live suites' job (tests/test_tenant_isolation_live.py, tests/test_property_split_live.py)."""

from __future__ import annotations

import contextlib
import hashlib
from datetime import datetime, timezone
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import curation
from api import main as api_main
from api import note_attachments as na
from api import tenant_pool

_WHEN = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
_PDF = b"%PDF-1.4 plan"


class _R2:
    def __init__(self) -> None:
        self.put: list[tuple[str, bytes, str]] = []
        self.objects: dict[str, bytes] = {}

    def upload_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self.put.append((key, data, content_type))

    def download_bytes(self, key: str) -> bytes:
        return self.objects[key]


class _Cur:
    def __init__(self, db: "_Conn") -> None:
        self.db, self.rowcount, self._rows = db, 0, []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.db.ran.append((sql, params))
        if sql.startswith("SELECT count(f.id)"):
            self._rows = [self.db.note] if self.db.note else []
        elif sql.startswith("INSERT INTO property_note_attachments"):
            note_id, _key, filename, mime, size, _sha = params
            self._rows = [(41, note_id, filename, mime, size, _WHEN)]
        elif sql.startswith("SELECT f.storage_key"):
            self._rows = [self.db.stored] if self.db.stored else []
        elif sql.startswith("DELETE"):
            self.rowcount = self.db.deletes
        elif "FROM property_note_attachments" in sql:
            self._rows = self.db.files
        else:
            self._rows = self.db.notes

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


class _Conn:
    def __init__(self) -> None:
        self.ran: list[tuple[str, Any]] = []
        self.note: tuple | None = (0, None)          # (files on the note, already attached?)
        self.stored: tuple | None = None
        self.deletes = 1
        self.notes: list[tuple] = []
        self.files: list[tuple] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    @contextlib.contextmanager
    def transaction(self):
        yield


@pytest.fixture()
def r2(monkeypatch):
    fake = _R2()
    monkeypatch.setattr(na.image_storage, "is_configured", lambda: True)
    monkeypatch.setattr(na, "_client", fake)
    return fake


@pytest.fixture()
def conn(monkeypatch):
    db = _Conn()
    monkeypatch.setattr(na, "resolve_active_property_id", lambda _c, pid: pid)
    monkeypatch.setattr(curation, "resolve_active_property_id", lambda _c, pid: pid)
    api_main.app.dependency_overrides[tenant_pool.tenant_conn] = lambda: db
    yield db
    api_main.app.dependency_overrides.clear()


@pytest.fixture()
def client():
    return TestClient(api_main.app)


def _upload(client, name: str, data: bytes = _PDF, mime: str = "application/pdf"):
    return client.post("/properties/5/notes/7/attachments", files={"file": (name, data, mime)})


def test_an_upload_lands_in_r2_under_the_note_and_is_recorded(client, conn, r2):
    res = _upload(client, "Plán bytu.pdf")
    assert res.status_code == 200
    assert res.json() == {"id": 41, "note_id": 7, "filename": "Plán bytu.pdf",
                          "mime_type": "application/pdf", "byte_size": len(_PDF),
                          "created_at": _WHEN.isoformat()}
    [(key, data, mime)] = r2.put
    assert key.startswith("custom-attachments/note/7/") and key.endswith(".pdf")
    assert (data, mime) == (_PDF, "application/pdf")
    insert = next(p for s, p in conn.ran if s.startswith("INSERT"))
    assert insert == (7, key, "Plán bytu.pdf", "application/pdf", len(_PDF),
                      hashlib.sha256(_PDF).hexdigest())
    check = next(p for s, p in conn.ran if s.startswith("SELECT count"))
    assert check[1:] == (7, 5), "the note must be this property's"


@pytest.mark.parametrize("name,mime", [
    ("drawing.svg", "image/svg+xml"),       # active content
    ("page.html", "text/plain"),            # a named extension decides, not the declared type
    ("tool.exe", "application/octet-stream"),
    ("noext", "text/html"),
])
def test_an_active_or_unknown_type_is_refused_before_storage(client, conn, r2, name, mime):
    assert _upload(client, name, b"x", mime).status_code == 415
    assert r2.put == []


def test_a_file_without_extension_takes_its_declared_type(client, conn, r2):
    assert _upload(client, "scan", b"\xff\xd8jpeg", "image/jpeg").status_code == 200
    assert r2.put[0][0].endswith(".jpg")


@pytest.mark.parametrize("note,status", [
    (None, 404),            # not this property's note, or not the caller's (RLS)
    ((1, True), 409),       # the same bytes are on the note already
    ((na.MAX_FILES_PER_NOTE, False), 409),
])
def test_the_note_is_checked_before_storage(client, conn, r2, note, status):
    conn.note = note
    assert _upload(client, "plan.pdf").status_code == status
    assert r2.put == []


def test_an_empty_or_oversized_file_is_refused(client, conn, r2, monkeypatch):
    assert _upload(client, "plan.pdf", b"").status_code == 400
    monkeypatch.setattr(na, "MAX_BYTES", 4)
    assert _upload(client, "plan.pdf", b"12345").status_code == 413
    assert r2.put == []


def test_a_read_serves_the_stored_type_never_sniffed(client, conn, r2):
    conn.stored = ("custom-attachments/note/7/a.pdf", "application/pdf", "Smlouva č. 1.pdf")
    r2.objects["custom-attachments/note/7/a.pdf"] = _PDF
    res = client.get("/properties/5/notes/7/attachments/41")
    assert res.status_code == 200 and res.content == _PDF
    assert res.headers["content-type"] == "application/pdf"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["cache-control"].startswith("private")
    assert res.headers["content-disposition"] == (
        "attachment; filename=\"Smlouva . 1.pdf\"; "
        "filename*=UTF-8''Smlouva%20%C4%8D.%201.pdf")


def test_a_missing_attachment_is_404_on_read_and_delete(client, conn, r2):
    conn.deletes = 0
    assert client.get("/properties/5/notes/7/attachments/41").status_code == 404
    assert client.delete("/properties/5/notes/7/attachments/41").status_code == 404


def test_a_delete_removes_the_row_scoped_to_note_and_property(client, conn, r2):
    assert client.delete("/properties/5/notes/7/attachments/41").json() == {"deleted": True}
    [(sql, params)] = [(s, p) for s, p in conn.ran if s.startswith("DELETE")]
    assert params == (41, 7, 5) and "property_id" in sql


def test_the_notes_list_carries_each_notes_attachments(conn):
    conn.notes = [(8, 5, "druhá", None, _WHEN, None), (7, 5, "první", None, _WHEN, None)]
    conn.files = [(41, 7, "plan.pdf", "application/pdf", 10, _WHEN)]
    data = curation.list_notes(conn, 5)["data"]
    assert [(n["id"], [f["id"] for f in n["attachments"]]) for n in data] == [(8, []), (7, [41])]


def test_the_routes_need_a_signed_in_user(client):
    assert _upload(client, "plan.pdf").status_code == 401


@pytest.mark.parametrize("raw,clean", [
    ("C:\\Users\\me\\plan.pdf", "plan.pdf"),
    ("../../etc/passwd", "passwd"),
    ("a\x00b\nc.txt", "abc.txt"),
    ("", "attachment"),
    ("x" * 300 + ".pdf", "x" * 251 + ".pdf"),
])
def test_a_file_name_is_cleaned_to_a_display_name(raw, clean):
    assert na.clean_filename(raw) == clean


def test_the_allowlist_holds_nothing_active():
    assert not {"text/html", "image/svg+xml", "application/javascript", "text/xml"} & set(na.ALLOWED)
    assert all(ext.startswith(".") for ext in na.BY_EXTENSION)
