"""Search-index sinks: where crawled documents become queryable (2.8.0).

A crawl that only writes JSONL leaves its output to ``grep``. A sink is the
opposite contract: ``upsert()`` documents in, ``search()`` ranked results
out, everything else (persistence, tokenization, ranking) is the sink's
problem. Implementations:

* :class:`SqliteFtsSink` -- SQLite FTS5, zero-dependency default. Falls back
  to a LIKE-scan table on builds where the FTS5 module is compiled out
  (rank order degrades to recency; documented, not hidden).
* :class:`JsonlSink` -- append-only JSONL with an in-memory term index;
  useful as a HandoffSink for tests and small crawls.
* :class:`NullSink` -- accepts and forgets; lets callers disable indexing
  without branching.

All sinks are protocol-compatible: ``upsert(doc_id, url, title, text,
metadata)`` / ``search(query, limit)`` / ``close()``. Re-upserting a doc_id
REPLACES the previous document (a recrawl updates, it does not duplicate --
matching how the frontier treats URLs).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol

__all__ = ["JsonlSink", "NullSink", "SearchSink", "SqliteFtsSink", "fts5_available"]


class SearchSink(Protocol):
    """The minimal index contract every sink implements."""

    def upsert(
        self,
        doc_id: str,
        *,
        url: str,
        title: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]: ...

    def count(self) -> int: ...

    def close(self) -> None: ...


def fts5_available() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            return True
        finally:
            conn.close()
    except Exception:
        return False


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]


class SqliteFtsSink:
    """FTS5-backed sink. One file, ``sqlite3``-inspectable, crash-safe."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._use_fts5 = fts5_available()
        if self._use_fts5:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS docs(
                  doc_id TEXT PRIMARY KEY, url TEXT, title TEXT, metadata TEXT,
                  indexed_at REAL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
                  doc_id UNINDEXED, title, text
                );
                """
            )
        else:
            # FTS5-less build: keep the same API over a LIKE scan.
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS docs(
                  doc_id TEXT PRIMARY KEY, url TEXT, title TEXT,
                  text TEXT, metadata TEXT, indexed_at REAL
                );
                """
            )
        self._conn.commit()

    def upsert(
        self,
        doc_id: str,
        *,
        url: str,
        title: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        with self._lock:
            # replace, never duplicate: a recrawl updates the entry
            if self._use_fts5:
                self._conn.execute("DELETE FROM docs_fts WHERE doc_id = ?", (doc_id,))
                self._conn.execute("DELETE FROM docs WHERE doc_id = ?", (doc_id,))
                self._conn.execute(
                    "INSERT INTO docs(doc_id, url, title, metadata, indexed_at) VALUES(?,?,?,?,?)",
                    (doc_id, url, title, meta_json, now),
                )
                self._conn.execute(
                    "INSERT INTO docs_fts(doc_id, title, text) VALUES(?,?,?)",
                    (doc_id, title, text),
                )
            else:
                self._conn.execute(
                    "INSERT OR REPLACE INTO docs(doc_id, url, title, text, metadata, indexed_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (doc_id, url, title, text, meta_json, now),
                )
            self._conn.commit()

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        terms = _tokens(query)
        if not terms:
            return []
        with self._lock:
            if self._use_fts5:
                match = " OR ".join(f'"{t}"' for t in terms)
                rows = self._conn.execute(
                    "SELECT d.doc_id, d.url, d.title, d.metadata,"
                    " bm25(docs_fts) AS rank"
                    " FROM docs_fts f JOIN docs d ON d.doc_id = f.doc_id"
                    " WHERE docs_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match, limit),
                ).fetchall()
                return [
                    {"doc_id": r[0], "url": r[1], "title": r[2], "metadata": json.loads(r[3] or "{}"), "score": -r[4]}
                    for r in rows
                ]
            # FTS5-less build: substring scan. Degraded ranking (recency
            # order, no bm25) but the same API and the same call sites.
            pattern = "%" + "%".join(terms) + "%"
            rows = self._conn.execute(
                "SELECT doc_id, url, title, metadata FROM docs"
                " WHERE text LIKE ? OR title LIKE ? ORDER BY indexed_at DESC LIMIT ?",
                (pattern, pattern, limit),
            ).fetchall()
            return [
                {"doc_id": r[0], "url": r[1], "title": r[2], "metadata": json.loads(r[3] or "{}"), "score": 1.0}
                for r in rows
            ]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class JsonlSink:
    """Append-only JSONL + in-memory tf-idf term index. No server, no SQL."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = str(path) if path else None
        self._docs: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        if self.path and Path(self.path).exists():
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    self._docs[rec["doc_id"]] = rec
                    self._order.append(rec["doc_id"])

    def upsert(
        self,
        doc_id: str,
        *,
        url: str,
        title: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        rec = {
            "doc_id": doc_id,
            "url": url,
            "title": title,
            "text": text,
            "metadata": metadata or {},
            "indexed_at": time.time(),
        }
        with self._lock:
            if doc_id not in self._docs:
                self._order.append(doc_id)
            self._docs[doc_id] = rec
            if self.path:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        q = set(_tokens(query))
        if not q:
            return []
        with self._lock:
            n_docs = max(1, len(self._docs))
            results: list[dict[str, Any]] = []
            for doc_id in self._order:
                rec = self._docs[doc_id]
                toks = _tokens(rec["title"] + " " + rec["text"])
                if not toks:
                    continue
                score = 0.0
                for t in q:
                    tf = toks.count(t)
                    if tf:
                        df = sum(1 for d in self._docs.values() if t in _tokens(d["title"] + " " + d["text"]))
                        score += tf * (math.log(n_docs / df) + 1.0)
                if score > 0:
                    results.append(
                        {
                            "doc_id": doc_id,
                            "url": rec["url"],
                            "title": rec["title"],
                            "metadata": rec["metadata"],
                            "score": round(score, 4),
                        }
                    )
            results.sort(key=lambda r: r["score"], reverse=True)
            return results[:limit]

    def count(self) -> int:
        with self._lock:
            return len(self._docs)

    def close(self) -> None:
        return None


class NullSink:
    """Accepts everything, remembers nothing. For 'no indexing' runs."""

    def upsert(self, doc_id: str, **_: Any) -> None:
        return None

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        return []

    def count(self) -> int:
        return 0

    def close(self) -> None:
        return None
