"""WACZ 1.2.0 export: zip a crawl's WARC evidence into a replayable collection.

A WARC proves the crawl holds the bytes it quotes; a WACZ makes those bytes
*replayable and self-verifying* for everyone else. WACZ (Web Archive Collection
Zipped, https://specs.webrecorder.net/wacz/1.2.0) is the standard packaging
that ReplayWeb.page, Browsertrix, and pywb consume: one zip holding the WARC
records, a CDXJ index that points back into them, a pages manifest, and a
Frictionless datapackage manifest whose sha-256 hashes bind it all together.

What ``build_wacz`` writes, from a finished WARC produced by
:mod:`freegem.crawler.warc`:

* ``archive/data.warc`` -- the source WARC, byte-for-byte (kept uncompressed so
  the CDXJ ``offset``/``length`` fields point at exact positions; the zip
  itself provides the compression, and re-deflating WARC is legal where
  re-gzipping an already-gzipped WARC is forbidden);
* ``index/index.cdx`` -- CDXJ lines ``<surt-key> <timestamp14> <json>`` for
  every ``response`` record, wins and walls alike (a 403 is real evidence of
  what the server answered);
* ``pages/pages.jsonl`` -- the spec's ``json-pages-1.0`` header line plus one
  entry per successful (2xx/3xx) response: walls are evidence, not pages;
* ``datapackage.json`` -- profile ``wacz``, sha-256 hex hashes and byte counts
  for every file above;
* ``datapackage-digest.json`` -- the sha-256 of ``datapackage.json`` itself.

``verify_wacz`` re-derives all of it without trusting the writer: it checks
the digest chain, re-hashes every resource, then seeks into the archive at
each CDXJ offset and recomputes the payload digest -- the same
"prove what you did" stance as the WARC module's own verifier.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.parse
import zipfile
from collections.abc import Iterator
from typing import Any

from .warc import iter_records_with_offsets

__all__ = ["WaczError", "build_wacz", "surt", "verify_wacz"]

#: The spec revision this module targets.
WACZ_VERSION = "1.2.0"

_COPY_CHUNK = 1024 * 1024

_ARCHIVE_PATH = "archive/data.warc"
_PAGES_PATH = "pages/pages.jsonl"
_INDEX_PATH = "index/index.cdx"


class WaczError(ValueError):
    """A WACZ file is structurally broken enough to refuse."""


def _sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _ts14(warc_date: str) -> str:
    """``2026-11-11T07:00:00Z`` -> CDXJ's ``20261111070000``."""
    return warc_date.replace("-", "").replace(":", "").replace("T", "").rstrip("Z")


def surt(uri: str) -> str:
    """SURT-ize a URI: ``https://www.Example.com/a`` -> ``com,example,www)/a``.

    Sort-friendly canonical form: lowercase host, dot-labels reversed, path
    and query kept in order. Simplifications, documented: ports are stripped
    (so ``host:8080`` and ``host`` collide into one key) and non-HTTP or
    hostless URIs are indexed lowercased as-is -- freegem's crawls are HTTP,
    and the CDXJ lines it writes always carry the original ``url`` beside the
    key, so nothing is unrecoverable.
    """
    parts = urllib.parse.urlsplit(uri)
    host = parts.netloc.rpartition("@")[2].lower()
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    if not host:
        return uri.lower()
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    key = ",".join(reversed(host.split("."))) + ")" + path
    return urllib.parse.quote(key, safe=",)?=&+;:@$'!*()~-._/[]")


def _http_status_mime(block: bytes) -> tuple[int, str]:
    """(status, mime) from an HTTP response block; (0, '') when unparseable."""
    separator = block.find(b"\r\n\r\n")
    head = block[:separator] if separator != -1 else block
    lines = head.split(b"\r\n")
    first = lines[0].decode("latin-1", "replace").split()
    status = int(first[1]) if len(first) > 1 and first[1].isdigit() else 0
    mime = ""
    for line in lines[1:]:
        name, _, value = line.decode("latin-1", "replace").partition(":")
        if name.strip().lower() == "content-type":
            mime = value.strip().split(";")[0]
            break
    return status, mime


def _response_body_offset(block: bytes) -> int:
    """Byte offset of the payload proper inside an HTTP response block."""
    separator = block.find(b"\r\n\r\n")
    return separator + 4 if separator != -1 else len(block)


def _scan_responses(warc_path: str) -> Iterator[dict[str, Any]]:
    """Yield one CDXJ annotation per ``response`` record, offsets exact."""
    for offset, length, record in iter_records_with_offsets(warc_path):
        if record.type != "response":
            continue
        url = record.target
        if not url:
            continue
        status, mime = _http_status_mime(record.payload)
        annotation = {
            "url": urllib.parse.unquote(url),
            "mime": mime,
            "status": status,
            "digest": record.headers.get("warc-payload-digest", ""),
            "offset": offset,
            "length": length,
            "filename": _ARCHIVE_PATH,
        }
        warc_date = record.headers.get("warc-date", "")
        yield {
            "surt": surt(urllib.parse.unquote(url)),
            "ts": _ts14(warc_date),
            "warc_date": warc_date,
            "annotation": annotation,
            "record_id": record.headers.get("warc-record-id", "")
            .strip("<>")
            .replace("urn:uuid:", ""),
            "status": status,
        }


def build_wacz(
    warc_path: str,
    out_path: str,
    *,
    software: str = "",
    title: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Package a finished WARC as a WACZ 1.2.0 zip; returns a summary.

    Raises :class:`WarcError <freegem.crawler.warc.WARCError>` when the source
    WARC is structurally broken (the scan refuses it) and :class:`WaczError`
    when it holds no indexable response records at all -- an evidence file
    with nothing captured is a caller mistake, not a collection.
    """
    entries = list(_scan_responses(warc_path))
    pages = [e for e in entries if 200 <= e["status"] < 400]

    pages_bytes = bytearray()
    pages_bytes.extend(
        (
            json.dumps({"format": "json-pages-1.0", "id": "pages", "title": "All Pages"}) + "\n"
        ).encode()
    )
    for e in pages:
        entry = {"id": e["record_id"], "url": e["annotation"]["url"], "ts": e["warc_date"]}
        pages_bytes.extend((json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))

    index_bytes = bytearray()
    for e in entries:
        line = f"{e['surt']} {e['ts']} " + json.dumps(e["annotation"], sort_keys=True)
        index_bytes.extend((line + "\n").encode("utf-8"))

    if not entries:
        raise WaczError(f"{warc_path}: no response records to index -- nothing to package")

    name = out_path.rsplit("/", 1)[-1]
    if name.endswith(".wacz"):
        name = name[: -len(".wacz")] or "collection"

    datapackage: dict[str, Any] = {
        "profile": "wacz",
        "wacz_version": WACZ_VERSION,
        "name": name,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if software:
        datapackage["software"] = software
    if title:
        datapackage["title"] = title
    if description:
        datapackage["description"] = description

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        resources: list[dict[str, Any]] = []
        with zf.open(_ARCHIVE_PATH, "w") as sink, open(warc_path, "rb") as src:
            digest = hashlib.sha256()
            total = 0
            while chunk := src.read(_COPY_CHUNK):
                digest.update(chunk)
                sink.write(chunk)
                total += len(chunk)
        resources.append(
            {
                "name": "data.warc",
                "path": _ARCHIVE_PATH,
                "hash": "sha256:" + digest.hexdigest(),
                "bytes": total,
            }
        )
        for path, blob, res_name in (
            (_PAGES_PATH, bytes(pages_bytes), "pages.jsonl"),
            (_INDEX_PATH, bytes(index_bytes), "index.cdx"),
        ):
            zf.writestr(path, blob)
            resources.append(
                {
                    "name": res_name,
                    "path": path,
                    "hash": _sha256_hex(blob),
                    "bytes": len(blob),
                }
            )
        datapackage["resources"] = resources
        dp_bytes = (json.dumps(datapackage, indent=2) + "\n").encode("utf-8")
        zf.writestr("datapackage.json", dp_bytes)
        zf.writestr(
            "datapackage-digest.json",
            json.dumps({"path": "datapackage.json", "hash": _sha256_hex(dp_bytes)}) + "\n",
        )

    return {
        "path": out_path,
        "bytes": os.path.getsize(out_path),
        "pages": len(pages),
        "cdx_entries": len(entries),
        "resources": [r["path"] for r in resources]
        + ["datapackage.json", "datapackage-digest.json"],
    }


def verify_wacz(path: str) -> dict[str, Any]:
    """Re-derive a WACZ's integrity without trusting its writer.

    Three layers, each checking the one below it: the digest file must match
    ``datapackage.json``; every resource must hash and size to its manifest
    entry; and every CDXJ line must point into ``archive/data.warc`` at an
    offset that actually holds the response the annotation claims -- the
    record's target URI and recomputed payload digest must both agree.

    Returns ``{"ok", "resources", "cdx_entries", "pages", "problems"}``;
    ``problems`` lists every mismatch found, so one bad entry does not hide
    the rest.
    """
    problems: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        for required in ("datapackage.json", "datapackage-digest.json", _PAGES_PATH, _INDEX_PATH):
            if required not in names:
                problems.append(f"missing {required}")
        if problems:
            return {"ok": False, "resources": 0, "cdx_entries": 0, "pages": 0, "problems": problems}

        dp_bytes = zf.read("datapackage.json")
        digest_entry = json.loads(zf.read("datapackage-digest.json"))
        if digest_entry.get("path") != "datapackage.json":
            problems.append("datapackage-digest.json points at " + str(digest_entry.get("path")))
        if digest_entry.get("hash") != _sha256_hex(dp_bytes):
            problems.append("datapackage.json digest mismatch")

        try:
            datapackage = json.loads(dp_bytes)
        except json.JSONDecodeError as exc:
            problems.append(f"datapackage.json is not JSON: {exc}")
            return {"ok": False, "resources": 0, "cdx_entries": 0, "pages": 0, "problems": problems}
        if datapackage.get("profile") != "wacz":
            problems.append(f"profile is {datapackage.get('profile')!r}, expected 'wacz'")
        if not str(datapackage.get("wacz_version", "")):
            problems.append("datapackage.json has no wacz_version")

        resources = {r.get("path"): r for r in datapackage.get("resources", [])}
        for res_path, res in resources.items():
            if res_path not in names:
                problems.append(f"manifest lists {res_path} but the zip lacks it")
                continue
            blob = zf.read(res_path)
            if res.get("hash") != _sha256_hex(blob):
                problems.append(f"{res_path} hash mismatch")
            if res.get("bytes") != len(blob):
                problems.append(f"{res_path} byte count {res.get('bytes')} != {len(blob)}")

        cdx_lines = zf.read(_INDEX_PATH).decode("utf-8", "replace").splitlines()
        pages_lines = zf.read(_PAGES_PATH).decode("utf-8", "replace").splitlines()
        pages_count = 0
        for line in pages_lines:
            if not line:
                continue
            obj = json.loads(line)
            if "url" in obj:
                pages_count += 1

        archive = resources.get(_ARCHIVE_PATH)
        if archive is None:
            problems.append(f"manifest lists no {_ARCHIVE_PATH}")
            return {
                "ok": False,
                "resources": len(resources),
                "cdx_entries": 0,
                "pages": pages_count,
                "problems": problems,
            }

        cdx_entries = 0
        page_urls = {obj["url"] for obj in map(json.loads, pages_lines[1:]) if "url" in obj}
        with zf.open(_ARCHIVE_PATH) as arc:
            for lineno, line in enumerate(cdx_lines, 1):
                if not line:
                    continue
                cdx_entries += 1
                parts = line.split(" ", 2)
                if len(parts) != 3:
                    problems.append(f"cdx line {lineno}: not surt ts json")
                    continue
                try:
                    annotation = json.loads(parts[2])
                except json.JSONDecodeError:
                    problems.append(f"cdx line {lineno}: annotation is not JSON")
                    continue
                offset, length = annotation.get("offset"), annotation.get("length")
                if not isinstance(offset, int) or not isinstance(length, int):
                    problems.append(f"cdx line {lineno}: offset/length missing")
                    continue
                arc.seek(offset)
                block = arc.read(length)
                record = _parse_record_block(block)
                if record is None:
                    problems.append(f"cdx line {lineno}: offset {offset} is not a WARC record")
                    continue
                headers, payload = record
                if headers.get("warc-target-uri") != annotation.get("url") and urllib.parse.unquote(
                    headers.get("warc-target-uri", "")
                ) != annotation.get("url"):
                    problems.append(
                        f"cdx line {lineno}: url {annotation.get('url')!r} not at offset"
                    )
                status, _mime = _http_status_mime(payload)
                if status != annotation.get("status"):
                    problems.append(
                        f"cdx line {lineno}: status {annotation.get('status')} != HTTP {status}"
                    )
                body = payload[_response_body_offset(payload) :]
                digest = "sha-256:" + base64.b64encode(hashlib.sha256(body).digest()).decode()
                if annotation.get("digest") and annotation["digest"] != digest:
                    problems.append(f"cdx line {lineno}: payload digest mismatch")
                if 200 <= status < 400 and annotation.get("url") not in page_urls:
                    problems.append(f"cdx line {lineno}: 2xx/3xx url missing from pages.jsonl")

    return {
        "ok": not problems,
        "resources": len(resources),
        "cdx_entries": cdx_entries,
        "pages": pages_count,
        "problems": problems,
    }


def _parse_record_block(block: bytes) -> tuple[dict[str, str], bytes] | None:
    """Parse one serialized WARC record back into (headers, payload)."""
    separator = block.find(b"\r\n\r\n")
    if separator == -1:
        return None
    lines = block[:separator].decode("utf-8", "replace").split("\r\n")
    if not lines or not lines[0].startswith("WARC/"):
        return None
    headers: dict[str, str] = {}
    for hline in lines[1:]:
        name, _, value = hline.partition(":")
        headers[name.strip().lower()] = value.strip()
    try:
        content_length = int(headers.get("content-length", "0"))
    except ValueError:
        return None
    payload = block[separator + 4 : separator + 4 + content_length]
    if len(payload) != content_length:
        return None
    return headers, payload
