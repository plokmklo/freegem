"""Tests for document/blob store adapters (stores.py).

Sqlite + file stores are exercised for real; Postgres/S3 are tested through
a minimal fake driver so the adapter logic (SQL shape, row normalization,
key mapping) is verified without a server, plus the unavailable-package
error paths.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from freegem.stores import (
    FileBlobStore,
    PostgresDocumentStore,
    PostgresUnavailable,
    S3BlobStore,
    S3Unavailable,
    SqliteDocumentStore,
    open_blob_store,
    open_document_store,
)

# ─── sqlite document store ───────────────────────────────────────────────────


def test_sqlite_store_upsert_get_roundtrip(tmp_path: Path) -> None:
    s = SqliteDocumentStore(str(tmp_path / "docs.db"))
    doc_id = s.upsert(
        {"url": "https://x/a", "title": "A", "text": "body a", "metadata": {"lang": "en"}}
    )
    got = s.get(doc_id)
    assert got is not None
    assert got["url"] == "https://x/a"
    assert got["metadata"] == {"lang": "en"}
    assert got["doc_id"] == doc_id


def test_sqlite_store_upsert_by_url_is_replace(tmp_path: Path) -> None:
    s = SqliteDocumentStore(str(tmp_path / "docs.db"))
    id1 = s.upsert({"url": "https://x/a", "title": "v1", "text": ""})
    id2 = s.upsert({"url": "https://x/a", "title": "v2", "text": ""})
    assert s.count() == 1
    got = s.get(id1)
    assert got is not None
    assert got["title"] == "v2"
    assert id2 == id1  # same url -> same content-derived id


def test_sqlite_store_get_by_url(tmp_path: Path) -> None:
    s = SqliteDocumentStore(str(tmp_path / "docs.db"))
    s.upsert({"url": "https://x/b", "title": "B", "text": "x"})
    by_url = s.get_by_url("https://x/b")
    assert by_url is not None
    assert by_url["title"] == "B"
    assert s.get_by_url("https://x/missing") is None


def test_sqlite_store_metadata_query(tmp_path: Path) -> None:
    s = SqliteDocumentStore(str(tmp_path / "docs.db"))
    s.upsert(
        {"url": "https://x/1", "title": "", "text": "", "metadata": {"host": "a.com", "depth": 1}}
    )
    s.upsert(
        {"url": "https://x/2", "title": "", "text": "", "metadata": {"host": "b.com", "depth": 1}}
    )
    hits = s.query(metadata={"host": "a.com"})
    assert len(hits) == 1 and hits[0]["url"] == "https://x/1"
    assert s.query(metadata={"host": "nope"}) == []
    assert len(s.query(limit=1)) == 1


def test_sqlite_store_delete_and_missing(tmp_path: Path) -> None:
    s = SqliteDocumentStore(str(tmp_path / "docs.db"))
    did = s.upsert({"url": "https://x/d", "title": "D", "text": ""})
    assert s.delete(did) is True
    assert s.delete(did) is False
    assert s.get(did) is None
    s.close()


def test_sqlite_store_strips_scheme(tmp_path: Path) -> None:
    s = open_document_store(f"sqlite://{tmp_path / 'schemed.db'}")
    s.upsert({"url": "u", "title": "t", "text": ""})
    assert s.count() == 1


def test_open_document_store_unknown_scheme() -> None:
    with pytest.raises(ValueError, match="unknown document-store scheme"):
        open_document_store("mongodb://x")


# ─── postgres adapter via fake driver ────────────────────────────────────────


class FakeCursor:
    """Mimics enough of psycopg's cursor for the adapter's SQL paths."""

    def __init__(self, store: FakeConn) -> None:
        self.store = store
        self.description: Any = None
        self._rows: list[tuple] = []
        self.rowcount = 0

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.store.executed.append((" ".join(sql.split()), params))
        if "SELECT COUNT(*)" in sql:
            self.description = (type("Col", (), {"name": "n"})(),)
            self._rows = [(len(self.store.rows),)]
        elif "SELECT *" in sql:
            # tuples + a psycopg-style description (one .name per column)
            cols = ["doc_id", "url", "title", "text", "metadata", "updated_at"]
            self.description = [(type("C", (), {"name": c})) for c in cols]
            self._rows = [
                tuple(r[c] for c in cols) for r in self.store.rows if self._matches(r, params)
            ]
            self.rowcount = len(self._rows)
        elif "DELETE" in sql:
            self.rowcount = min(1, len(self.store.rows))
            self.store.rows.clear()
        elif "INSERT" in sql:
            self.store.rows.append(
                {
                    "doc_id": params[0],
                    "url": params[1],
                    "title": params[2],
                    "text": params[3],
                    "metadata": params[4],
                    "updated_at": params[5],
                }
            )

    @staticmethod
    def _matches(row: dict[str, Any], params: tuple[Any, ...]) -> bool:
        if not params:
            return True
        if len(params) == 2 and isinstance(params[0], str) and params[0].startswith("{"):
            meta = (
                json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
            )
            want = json.loads(params[0])
            return all(meta.get(k) == v for k, v in want.items())
        # single id/url param
        p = params[0]
        return row["doc_id"] == p or row["url"] == p

    def fetchall(self) -> list[tuple]:
        return self._rows

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *a: Any) -> None:
        return None


class FakeConn:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.executed: list[tuple[str, tuple]] = []
        self.committed = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture()
def pg(monkeypatch: pytest.MonkeyPatch) -> PostgresDocumentStore:
    conn = FakeConn()
    fake_mod = type("psycopg", (), {"connect": staticmethod(lambda dsn: conn)})
    monkeypatch.setitem(sys.modules, "psycopg", fake_mod)
    store = PostgresDocumentStore("postgresql://u:p@h/db")
    store._conn = conn
    return store


def test_postgres_upsert_uses_on_conflict_and_jsonb(pg: PostgresDocumentStore) -> None:
    pg.upsert({"url": "https://x/p", "title": "P", "text": "b", "metadata": {"h": 1}})
    sql, params = pg._conn.executed[-1]
    assert "ON CONFLICT(doc_id) DO UPDATE" in sql
    assert "__TABLE__" not in sql and "freegem_documents" in sql
    assert json.loads(params[4]) == {"h": 1}  # metadata serialized as JSON


def test_postgres_query_metadata_uses_jsonb_containment(pg: PostgresDocumentStore) -> None:
    pg._conn.rows.append(
        {
            "doc_id": "abc",
            "url": "u",
            "title": "t",
            "text": "x",
            "metadata": json.dumps({"lang": "en"}),
            "updated_at": 1.0,
        }
    )
    hits = pg.query(metadata={"lang": "en"})
    sql, params = pg._conn.executed[-1]
    assert "metadata @>" in sql
    assert params[0] == '{"lang": "en"}'
    assert hits[0]["metadata"] == {"lang": "en"}  # normalized from JSON string


def test_postgres_get_missing_returns_none(pg: PostgresDocumentStore) -> None:
    assert pg.get("nope") is None
    assert pg.get_by_url("nope") is None


def test_postgres_count(pg: PostgresDocumentStore) -> None:
    assert pg.count() == 0


def test_postgres_unavailable_without_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    saved = {k: sys.modules.pop(k) for k in ("psycopg", "psycopg2") if k in sys.modules}
    try:
        with pytest.raises(PostgresUnavailable, match="pip install psycopg"):
            PostgresDocumentStore("postgresql://u:p@h/db")
    finally:
        for name, mod in saved.items():
            sys.modules[name] = mod


def test_open_document_store_postgres_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn()
    fake_mod = type("psycopg", (), {"connect": staticmethod(lambda dsn: conn)})
    monkeypatch.setitem(sys.modules, "psycopg", fake_mod)
    store = open_document_store("postgresql://u:p@h/db")
    assert isinstance(store, PostgresDocumentStore)


# ─── file blob store ─────────────────────────────────────────────────────────


def test_file_blob_store_content_addressed_and_deduped(tmp_path: Path) -> None:
    s = FileBlobStore(tmp_path)
    data = b"hello blob"
    k1 = s.put(data)
    k2 = s.put(data)  # same bytes -> same key, one file
    assert k1 == k2
    assert s.get(k1) == data
    shard = tmp_path / "ab" / k1[:2] / k1
    assert shard.exists()
    assert len(list((tmp_path / "ab").rglob(k1))) == 1


def test_file_blob_store_suffix_and_missing(tmp_path: Path) -> None:
    s = FileBlobStore(tmp_path)
    k = s.put(b"pdf bytes", suffix=".pdf")
    assert s.exists(k)
    assert s.get(k) == b"pdf bytes"
    assert not s.exists("0" * 64)
    with pytest.raises(KeyError):
        s.get("0" * 64)
    assert s.delete(k) is True
    assert s.delete(k) is False


def test_open_blob_store_dispatch(tmp_path: Path) -> None:
    s = open_blob_store(f"file://{tmp_path}")
    assert isinstance(s, FileBlobStore)
    s2 = open_blob_store(str(tmp_path))  # bare path
    assert isinstance(s2, FileBlobStore)
    with pytest.raises(ValueError, match="unknown blob-store scheme"):
        open_blob_store("gcs://bucket")


def test_s3_unavailable_without_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    # Setting the slot to None makes `import boto3` raise ImportError even
    # when boto3 IS installed in the environment (pop() alone would just
    # trigger a fresh import from site-packages).
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(S3Unavailable, match="pip install boto3"):
        open_blob_store("s3://mybucket/crawl")


# ─── regression: upsert-by-url, atomicity, query window, suffix keys ─────────


def test_sqlite_store_upsert_same_url_other_doc_id_replaces(tmp_path: Path) -> None:
    s = SqliteDocumentStore(str(tmp_path / "docs.db"))
    s.upsert({"doc_id": "custom-id", "url": "https://x/a", "title": "v1", "text": "old"})
    new_id = s.upsert({"url": "https://x/a", "title": "v2", "text": "new"})
    assert new_id != "custom-id"  # content-derived id for the second write
    assert s.count() == 1
    got = s.get_by_url("https://x/a")
    assert got is not None
    assert got["doc_id"] == new_id and got["title"] == "v2"
    assert s.get("custom-id") is None
    s.close()


def test_sqlite_store_upsert_failure_rolls_back(tmp_path: Path) -> None:
    s = SqliteDocumentStore(str(tmp_path / "docs.db"))
    s.upsert({"doc_id": "keep", "url": "https://x/a", "title": "v1", "text": "old"})
    with pytest.raises(sqlite3.Error):
        # unbindable text fails inside the statement block, AFTER the
        # same-url guard DELETE has executed
        s.upsert({"doc_id": "other", "url": "https://x/a", "title": "v2", "text": object()})
    assert s.count() == 1  # the guard DELETE must not survive the failure
    assert s.get("keep") is not None
    s.close()


def test_sqlite_store_metadata_query_scans_past_limit_window(tmp_path: Path) -> None:
    s = SqliteDocumentStore(str(tmp_path / "docs.db"))
    # the only match is the OLDEST row -- outside any newest-row scan window
    for i in range(6):
        s.upsert(
            {
                "url": f"u{i}",
                "title": "",
                "text": "",
                "metadata": {"tag": "hit" if i == 0 else "miss"},
            }
        )
        s._conn.execute("UPDATE documents SET updated_at = ? WHERE url = ?", (float(i), f"u{i}"))
    s._conn.commit()
    hits = s.query(metadata={"tag": "hit"}, limit=1)
    assert [h["url"] for h in hits] == ["u0"]
    assert len(s.query(limit=1)) == 1  # unfiltered form still honors limit
    s.close()


def test_postgres_upsert_guards_url_conflict_and_casts_jsonb(pg: PostgresDocumentStore) -> None:
    pg.upsert(
        {"doc_id": "custom", "url": "https://x/u", "title": "v1", "text": "", "metadata": {"a": 1}}
    )
    pg.upsert({"url": "https://x/u", "title": "v2", "text": "", "metadata": {"a": 2}})
    deletes = [sql for sql, _ in pg._conn.executed if sql.startswith("DELETE")]
    assert deletes and all("url =" in d and "doc_id <>" in d for d in deletes)
    inserts = [sql for sql, _ in pg._conn.executed if "INSERT" in sql]
    assert inserts and "%s::jsonb" in inserts[-1]  # metadata arrives as jsonb, not text


def test_s3_blob_store_suffix_returns_retrievable_key(monkeypatch: pytest.MonkeyPatch) -> None:
    objects: dict[tuple[str, str], bytes] = {}

    class FakeS3:
        def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
            objects[(Bucket, Key)] = Body

        def get_object(self, *, Bucket: str, Key: str) -> Any:
            if (Bucket, Key) not in objects:
                raise KeyError(Key)
            body = objects[(Bucket, Key)]
            return {"Body": type("B", (), {"read": lambda self, b=body: b})()}

        def head_object(self, *, Bucket: str, Key: str) -> None:
            if (Bucket, Key) not in objects:
                raise KeyError(Key)

        def delete_object(self, *, Bucket: str, Key: str) -> None:
            objects.pop((Bucket, Key), None)

    fake_boto3 = type("boto3", (), {"client": staticmethod(lambda *a, **k: FakeS3())})
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    store = S3BlobStore("mybucket", prefix="crawl")
    key = store.put(b"pdf bytes", suffix=".pdf")
    assert key.endswith(".pdf")
    # the returned key resolves to the object put() actually stored
    assert ("mybucket", f"crawl/ab/{key[:2]}/{key}") in objects
    assert store.exists(key) is True
    assert store.get(key) == b"pdf bytes"
    assert store.delete(key) is True
    assert store.exists(key) is False
