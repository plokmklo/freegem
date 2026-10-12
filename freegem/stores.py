"""Document and blob store adapters: local defaults, Postgres/S3 at scale.

Two contracts, kept separate because documents and bytes have different
lifecycles:

* :class:`DocumentStore` — structured crawl records (url, title, text,
  metadata) with upsert-by-url and metadata queries. Implementations:
  :class:`SqliteDocumentStore` (zero-dependency default, one file) and
  :class:`PostgresDocumentStore` (optional ``psycopg``).
* :class:`BlobStore` — content-addressed opaque bytes (page bodies,
  screenshots, PDFs). Implementations: :class:`FileBlobStore` (the same
  sharded sha256 layout the crawl runner already writes) and
  :class:`S3BlobStore` (optional ``boto3``).

Every optional dependency is imported lazily and raises the specific
``*Unavailable`` error carrying the pip line — constructing without the
package is a clear message, not an ImportError traceback.

PostgreSQL note: the store uses ONLY ANSI SQL that psycopg (v2 or v3) can
run through its dictionary-row interface, and JSONB for metadata so
metadata queries stay server-side. No ORM, no migrations framework —
the schema is three statements, printed by ``schema_sql()`` for humans.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "BlobStore",
    "DocumentStore",
    "FileBlobStore",
    "PostgresDocumentStore",
    "PostgresUnavailable",
    "S3BlobStore",
    "S3Unavailable",
    "SqliteDocumentStore",
    "open_blob_store",
    "open_document_store",
]

_DOC_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
  doc_id     TEXT PRIMARY KEY,
  url        TEXT NOT NULL UNIQUE,
  title      TEXT NOT NULL DEFAULT '',
  text       TEXT NOT NULL DEFAULT '',
  metadata   TEXT NOT NULL DEFAULT '{}',
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_url ON documents(url);
"""


# ─── document stores ─────────────────────────────────────────────────────────


class DocumentStore(Protocol):
    def upsert(self, doc: dict[str, Any]) -> str: ...
    def get(self, doc_id: str) -> dict[str, Any] | None: ...
    def get_by_url(self, url: str) -> dict[str, Any] | None: ...
    def query(
        self, *, metadata: dict[str, Any] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]: ...
    def delete(self, doc_id: str) -> bool: ...
    def count(self) -> int: ...
    def close(self) -> None: ...


class SqliteDocumentStore:
    """Zero-dependency default. One file, WAL, same inspectability story as
    the frontier."""

    def __init__(self, path: str) -> None:
        if path.startswith("sqlite://"):
            path = path[len("sqlite://") :]
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_DOC_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    @staticmethod
    def _row_to_doc(row: tuple) -> dict[str, Any]:
        return {
            "doc_id": row[0],
            "url": row[1],
            "title": row[2],
            "text": row[3],
            "metadata": json.loads(row[4] or "{}"),
            "updated_at": row[5],
        }

    def upsert(self, doc: dict[str, Any]) -> str:
        doc_id = doc.get("doc_id") or hashlib.sha256(doc["url"].encode("utf-8")).hexdigest()[:16]
        with self._lock:
            self._conn.execute(
                "INSERT INTO documents(doc_id, url, title, text, metadata, updated_at)"
                " VALUES(?,?,?,?,?,COALESCE((SELECT updated_at FROM documents WHERE doc_id = ?), ?))"
                " ON CONFLICT(doc_id) DO UPDATE SET url=excluded.url, title=excluded.title,"
                " text=excluded.text, metadata=excluded.metadata",
                (
                    doc_id,
                    doc["url"],
                    doc.get("title", ""),
                    doc.get("text", ""),
                    json.dumps(doc.get("metadata") or {}, ensure_ascii=False),
                    doc_id,
                    _now(),
                ),
            )
            self._conn.execute(
                "UPDATE documents SET updated_at = ? WHERE doc_id = ?", (_now(), doc_id)
            )
            self._conn.commit()
        return doc_id

    def get(self, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
        return self._row_to_doc(row) if row else None

    def get_by_url(self, url: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM documents WHERE url = ?", (url,)).fetchone()
        return self._row_to_doc(row) if row else None

    def query(
        self, *, metadata: dict[str, Any] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Metadata containment query: every key/value must appear in the doc's
        metadata. Client-side filtering over an indexed scan -- honest about
        scale, and identical semantics to the Postgres JSONB @> version."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM documents ORDER BY updated_at DESC LIMIT ?", (limit * 4,)
            ).fetchall()
        docs = [self._row_to_doc(r) for r in rows]
        if metadata:
            docs = [d for d in docs if all(d["metadata"].get(k) == v for k, v in metadata.items())]
        return docs[:limit]

    def delete(self, doc_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            self._conn.commit()
            return bool(cur.rowcount)

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class PostgresUnavailable(Exception):
    """psycopg is not installed."""


class PostgresDocumentStore:
    """PostgreSQL-backed document store (optional psycopg, v2 or v3).

    One DSN, auto-detection of the cursor style: psycopg 3 returns dicts
    from ``cursor(row_factory=dict_row)``; psycopg 2 needs RealDictCursor.
    Metadata lives in JSONB and the containment query runs server-side.
    """

    def __init__(self, dsn: str, table: str = "freegem_documents") -> None:
        self.table = table
        try:
            import psycopg

            self._psycopg = psycopg
            self._v3 = True
        except ImportError:
            try:
                import psycopg2 as psycopg
            except ImportError as exc:
                raise PostgresUnavailable(
                    "pip install psycopg[binary]  (or psycopg2-binary)"
                ) from exc
            self._psycopg = psycopg
            self._v3 = False
        self._conn = self._psycopg.connect(dsn)
        self._ensure_schema()

    def _q(self, sql: str) -> str:
        return sql.replace("__TABLE__", self.table)

    def _ensure_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                self._q(
                    "CREATE TABLE IF NOT EXISTS __TABLE__("
                    " doc_id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE,"
                    " title TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '',"
                    " metadata JSONB NOT NULL DEFAULT '{}', updated_at DOUBLE PRECISION NOT NULL)"
                )
            )
            self._conn.commit()

    @staticmethod
    def schema_sql(table: str = "freegem_documents") -> str:
        """The DDL, for humans who want to run it themselves."""
        return (
            f"CREATE TABLE IF NOT EXISTS {table}("
            " doc_id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE,"
            " title TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '',"
            " metadata JSONB NOT NULL DEFAULT '{}', updated_at DOUBLE PRECISION NOT NULL);"
        )

    def _rows(self, cur: Any) -> list[dict[str, Any]]:
        rows = cur.fetchall()
        out = []
        for r in rows:
            if isinstance(r, dict):
                out.append(r)
            else:
                cols = [d.name for d in cur.description]
                out.append(dict(zip(cols, r, strict=True)))
        return out

    def upsert(self, doc: dict[str, Any]) -> str:
        doc_id = doc.get("doc_id") or hashlib.sha256(doc["url"].encode("utf-8")).hexdigest()[:16]
        with self._conn.cursor() as cur:
            cur.execute(
                self._q(
                    "INSERT INTO __TABLE__(doc_id, url, title, text, metadata, updated_at)"
                    " VALUES(%s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT(doc_id) DO UPDATE SET url = EXCLUDED.url,"
                    " title = EXCLUDED.title, text = EXCLUDED.text,"
                    " metadata = EXCLUDED.metadata, updated_at = EXCLUDED.updated_at"
                ),
                (
                    doc_id,
                    doc["url"],
                    doc.get("title", ""),
                    doc.get("text", ""),
                    json.dumps(doc.get("metadata") or {}),
                    _now(),
                ),
            )
            self._conn.commit()
        return doc_id

    def get(self, doc_id: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute(self._q("SELECT * FROM __TABLE__ WHERE doc_id = %s"), (doc_id,))
            rows = self._rows(cur)
        return _normalize_pg(rows[0]) if rows else None

    def get_by_url(self, url: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute(self._q("SELECT * FROM __TABLE__ WHERE url = %s"), (url,))
            rows = self._rows(cur)
        return _normalize_pg(rows[0]) if rows else None

    def query(
        self, *, metadata: dict[str, Any] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            if metadata:
                cur.execute(
                    self._q(
                        "SELECT * FROM __TABLE__ WHERE metadata @> %s::jsonb"
                        " ORDER BY updated_at DESC LIMIT %s"
                    ),
                    (json.dumps(metadata), limit),
                )
            else:
                cur.execute(
                    self._q("SELECT * FROM __TABLE__ ORDER BY updated_at DESC LIMIT %s"), (limit,)
                )
            rows = self._rows(cur)
        return [_normalize_pg(r) for r in rows]

    def delete(self, doc_id: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(self._q("DELETE FROM __TABLE__ WHERE doc_id = %s"), (doc_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def count(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute(self._q("SELECT COUNT(*) AS n FROM __TABLE__"))
            rows = self._rows(cur)
        return int(rows[0]["n"])

    def close(self) -> None:
        self._conn.close()


def _normalize_pg(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize one psycopg row: bytes → str, metadata str → dict."""
    out = dict(row)
    meta = out.get("metadata")
    if isinstance(meta, (str, bytes)):
        out["metadata"] = json.loads(meta)
    for key in ("doc_id", "title", "text"):
        if isinstance(out.get(key), (bytes, bytearray)):
            out[key] = out[key].decode("utf-8")
    return out


def _now() -> float:
    import time

    return time.time()


def open_document_store(url: str) -> DocumentStore:
    """Scheme dispatch: sqlite:///path (default), postgres://dsn."""
    scheme = url.split(":", 1)[0].lower()
    if scheme == "sqlite":
        return SqliteDocumentStore(url)
    if scheme in ("postgres", "postgresql"):
        return PostgresDocumentStore(url)
    raise ValueError(f"unknown document-store scheme {scheme!r} in {url!r}")


# ─── blob stores ─────────────────────────────────────────────────────────────


class BlobStore(Protocol):
    def put(self, data: bytes, *, suffix: str = "") -> str: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> bool: ...
    def exists(self, key: str) -> bool: ...


class FileBlobStore:
    """Content-addressed filesystem blobs: ab/<sha256[:2]>/<sha256><suffix>.

    The same layout the crawl runner writes page bodies in, so crawl
    artifacts and pipeline blobs interoperate: putting a body that already
    exists is a no-op (content addressing dedupes for free).
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / "ab" / key[:2] / key

    def put(self, data: bytes, *, suffix: str = "") -> str:
        key = hashlib.sha256(data).hexdigest()
        name = key + suffix
        p = self._path(name)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, p)  # atomic: a crashed write never poisons the store
        return name

    def get(self, key: str) -> bytes:
        p = self._find(key)
        return p.read_bytes()

    def _find(self, key: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", key):
            p = self._path(key)
            if p.exists():
                return p
            raise KeyError(f"blob {key} not found")
        # suffixed form: <hash><suffix>
        if len(key) >= 64 and re.fullmatch(r"[0-9a-f]{64}.+", key):
            p = self._path(key)
            if p.exists():
                return p
        raise KeyError(f"blob {key} not found")

    def delete(self, key: str) -> bool:
        try:
            self._find(key).unlink()
            return True
        except KeyError:
            return False

    def exists(self, key: str) -> bool:
        try:
            self._find(key)
            return True
        except KeyError:
            return False


class S3Unavailable(Exception):
    """boto3 is not installed."""


class S3BlobStore:
    """S3 (or any S3-compatible endpoint) blob store via optional boto3.

    Keys are the same sha256 content addresses as FileBlobStore, so a crawl
    can switch local->S3 without changing anything downstream. Prefix keeps
    unrelated tenants apart inside one bucket.
    """

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "freegem",
        endpoint_url: str | None = None,
        region: str | None = None,
    ) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise S3Unavailable("pip install boto3") from exc
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)

    def _key(self, key: str) -> str:
        return f"{self.prefix}/ab/{key[:2]}/{key}"

    def put(self, data: bytes, *, suffix: str = "") -> str:
        digest = hashlib.sha256(data).hexdigest()
        full = self._key(digest) + suffix
        self._client.put_object(Bucket=self.bucket, Key=full, Body=data)
        return digest

    def get(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self.bucket, Key=self._key(key))
        return resp["Body"].read()

    def delete(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception:
            return False
        self._client.delete_object(Bucket=self.bucket, Key=self._key(key))
        return True

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except Exception:
            return False


def open_blob_store(url: str) -> BlobStore:
    """Scheme dispatch: a bare path or file:///root (default), s3://bucket/prefix."""
    if "://" not in url:  # bare filesystem path
        return FileBlobStore(url)
    scheme = url.split(":", 1)[0].lower()
    if scheme == "file":
        return FileBlobStore(url[len("file://") :])
    if scheme == "s3":
        rest = url[len("s3://") :]
        bucket, _, prefix = rest.partition("/")
        return S3BlobStore(bucket, prefix=prefix or "freegem")
    raise ValueError(f"unknown blob-store scheme {scheme!r} in {url!r}")
