"""Result writers: JSONL (streaming), JSON (document), CSV (tabular).

Common contract:

* every record passes through :func:`redact_secrets` before hitting disk --
  the crawler writes untrusted-page content, and pages love to embed keys;
* writes land in a sibling temp file and are renamed at close, so a killed
  run never leaves a half-written result file behind (append mode excepted:
  resume continues an existing file by design);
* CSV columns come from the schema's field order plus fixed provenance
  columns; list values join with ``;`` and newlines are stripped, so one
  record is always one line.
"""

from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import dataclass
from typing import Any

from .hardening import redact_secrets

__all__ = ["PROVENANCE_COLUMNS", "make_writer"]

PROVENANCE_COLUMNS = ("url", "fetched_at", "content_hash", "via")


@dataclass
class _Sink:
    path: str
    handle: io.TextIOBase
    append: bool


def _open(path: str, *, append: bool) -> _Sink:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    if append:
        return _Sink(path, open(path, "a", encoding="utf-8", newline=""), True)
    tmp = path + ".tmp"
    return _Sink(path, open(tmp, "w", encoding="utf-8", newline=""), False)


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ";".join(_flatten(v) for v in value)
    text = str(value)
    return text.replace("\r", " ").replace("\n", " ")


class _JsonlWriter:
    def __init__(self, path: str, *, append: bool) -> None:
        self._sink = _open(path, append=append)

    def write(self, item: dict[str, Any]) -> None:
        line = json.dumps(redact_secrets_json(item), ensure_ascii=False)
        self._sink.handle.write(line + "\n")
        self._sink.handle.flush()

    def close(self) -> None:
        self._sink.handle.close()
        if not self._sink.append:
            os.replace(self._sink.path + ".tmp", self._sink.path)


class _JsonWriter:
    def __init__(self, path: str, *, append: bool) -> None:
        if append:
            raise ValueError("json format does not support append; use jsonl")
        self._sink = _open(path, append=False)
        self._sink.handle.write("[\n")
        self._first = True

    def write(self, item: dict[str, Any]) -> None:
        self._sink.handle.write(("" if self._first else ",\n") + "  ")
        self._first = False
        self._sink.handle.write(json.dumps(redact_secrets_json(item), ensure_ascii=False))

    def close(self) -> None:
        self._sink.handle.write("\n]\n")
        self._sink.handle.close()
        os.replace(self._sink.path + ".tmp", self._sink.path)


class _CsvWriter:
    def __init__(self, path: str, columns: list[str], *, append: bool) -> None:
        self._sink = _open(path, append=append)
        self._columns = columns
        self._writer = csv.writer(self._sink.handle, lineterminator="\n")
        if not append:
            self._writer.writerow(columns)

    def write(self, item: dict[str, Any]) -> None:
        # Same contract as the JSONL/JSON writers: every record passes
        # through redact_secrets before hitting disk (issue #43 -- CSV rows
        # used to skip it, so crawled secrets landed verbatim in exports).
        redacted = redact_secrets_json(item)
        self._writer.writerow([_flatten(redacted.get(col)) for col in self._columns])

    def close(self) -> None:
        self._sink.handle.close()
        if not self._sink.append:
            os.replace(self._sink.path + ".tmp", self._sink.path)


def redact_secrets_json(item: dict[str, Any]) -> dict[str, Any]:
    """Redact secret-shaped strings anywhere inside a result record."""
    out: dict[str, Any] = {}
    for key, value in item.items():
        if isinstance(value, str):
            out[key] = redact_secrets(value)
        elif isinstance(value, list):
            out[key] = [
                redact_secrets(v)
                if isinstance(v, str)
                else redact_secrets_json(v)
                if isinstance(v, dict)
                else v
                for v in value
            ]
        elif isinstance(value, dict):
            out[key] = redact_secrets_json(value)
        else:
            out[key] = value
    return out


def make_writer(
    path: str,
    fmt: str,
    field_names: list[str] | None = None,
    *,
    append: bool = False,
) -> _JsonlWriter | _JsonWriter | _CsvWriter:
    """Writer for ``fmt`` in ``jsonl|json|csv``.

    CSV column order: schema fields first, provenance columns after, then
    any extras the run produced (e.g. ``_index``), keeping files stable when
    pages occasionally yield an extra key.
    """
    if fmt == "jsonl":
        return _JsonlWriter(path, append=append)
    if fmt == "json":
        return _JsonWriter(path, append=append)
    if fmt == "csv":
        columns = list(field_names or [])
        for col in PROVENANCE_COLUMNS:
            if col not in columns:
                columns.append(col)
        if "_index" not in columns:
            columns.append("_index")
        return _CsvWriter(path, columns, append=append)
    raise ValueError(f"unknown output format {fmt!r} (jsonl|json|csv)")
