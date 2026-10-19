"""WARC 1.1 (ISO 28500) evidence capture for the crawler, stdlib-only.

Why an archive format: the crawl already stores raw bodies content-addressed,
but a body without its HTTP exchange is only half the evidence. A WARC record
carries the request, the status line, the exact response headers, the body,
payload digests, and freegem's own annotations (wall class, fetch outcome) --
so "we hold the bytes we quote" becomes a verifiable claim, replayable with
standard tools (warcio, pywb, Browsertrix).

What is written, per fetched URL:

* a ``warcinfo`` record once per file: software, format, the honest robots
  stance (never consulted -- attacker-controlled text, see docs/NON_GOALS.md),
  and the crawl's politeness delay;
* a ``request`` record (GET, honest UA, conditional headers when sent);
* a ``response`` record for every HTTP answer -- 2xx, 3xx redirect hops, 304
  not-modified, AND 4xx/5xx wall answers, each with block and payload
  digests (``sha-256``, base64) so tampering is detectable;
* a ``metadata`` record for fetches that produced no HTTP answer at all
  (DNS failure, timeout, SSRF refusal) carrying the outcome taxonomy as
  warc-fields -- the wall-class telemetry lives inside the archive.

Serialization is strict ISO 28500: CRLF line endings everywhere, named-field
header blocks, ``Content-Length`` over the exact block bytes, and the record
terminated by a blank line. Bodies are written as captured (no re-encoding);
spilled oversized bodies stream from their temp file. The writer is
thread-safe because the crawl loop is a worker pool.

The reader side (:func:`iter_records`, :func:`verify`, :func:`summary`)
re-reads what the writer produced -- or what any other WARC producer wrote --
and checks lengths and digests, so an archive's integrity can be proven
without trusting the writer that made it.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import os
import re
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

__all__ = [
    "WARCError",
    "WarcRecord",
    "WarcWriter",
    "iter_records",
    "summary",
    "verify",
]

#: The format string this module produces and consumes.
WARC_VERSION = "WARC/1.1"

_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_CRLF = b"\r\n"

_HTTP_STATUS_REASONS: dict[int, str] = dict(http.client.responses)

#: Guard against a corrupted Content-Length steering the reader into RAM.
_MAX_BLOCK_BYTES = 256 * 1024 * 1024


class WARCError(ValueError):
    """A WARC file (or record) violates ISO 28500 enough to refuse it."""


def _warc_date(when: float) -> str:
    """ISO 8601 UTC timestamp with seconds precision, as WARC-Date wants."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


def _digest(data: bytes) -> str:
    return "sha-256:" + base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _target_uri(url: str) -> str:
    """Percent-encode anything a named-field line could not carry."""
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=-._~'!")


def _field_lines(fields: list[tuple[str, str]]) -> bytes:
    """Serialize named-field lines; refuse anything that could split records."""
    lines: list[bytes] = []
    for name, value in fields:
        if not _TOKEN_RE.match(name):
            raise WARCError(f"invalid WARC field name: {name!r}")
        flat = str(value)
        if "\r" in flat or "\n" in flat:
            raise WARCError(f"WARC field {name} value contains a line break")
        lines.append(f"{name}: {flat}".encode())
    return _CRLF.join(lines)


def _request_target(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return target


@dataclass(frozen=True)
class WarcRecord:
    """One parsed WARC record: lowercase header keys + raw block bytes."""

    headers: dict[str, str]
    payload: bytes

    @property
    def type(self) -> str:
        return self.headers.get("warc-type", "")

    @property
    def target(self) -> str:
        return self.headers.get("warc-target-uri", "")

    def field(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


class WarcWriter:
    """Append-only WARC 1.1 writer over one file, safe for worker threads.

    ``uuid_gen``/``clock`` exist for tests; production callers use the
    defaults (uuid4 urns, wall-clock time). The first record written is
    implicitly preceded by the file's ``warcinfo`` record, and every later
    record references it via ``WARC-Warcinfo-ID`` -- the standard way to
    carry crawl-level provenance inside the archive.
    """

    def __init__(
        self,
        path: str,
        *,
        software: str = "freegem",
        robots_stance: str = "",
        crawl_note: str = "",
        uuid_gen: Callable[[], uuid.UUID] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.path = path
        self._uuid_gen = uuid_gen or uuid.uuid4
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._fh = open(path, "wb")  # noqa: SIM115 - append-only writer, closed in close()
        self.records = 0
        self._warcinfo_id: str | None = None
        self._software = software
        self._robots_stance = robots_stance
        self._crawl_note = crawl_note

    # ── public record writers ────────────────────────────────────────────

    def warcinfo(self, extra: dict[str, str] | None = None) -> str:
        """The file's crawl-level provenance record (written once, first)."""
        with self._lock:
            if self._warcinfo_id is not None:
                return self._warcinfo_id
            fields: list[tuple[str, str]] = [
                ("software", self._software),
                ("format", WARC_VERSION),
            ]
            if self._robots_stance:
                fields.append(("robots", self._robots_stance))
            if self._crawl_note:
                fields.append(("freegem-crawl-note", self._crawl_note))
            for name, value in sorted((extra or {}).items()):
                fields.append((name, value))
            record_id = self._write(
                warc_type="warcinfo",
                payload=_field_lines(fields),
                content_type="application/warc-fields",
            )
            self._warcinfo_id = record_id
            return record_id

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
    ) -> str:
        """One HTTP request record (GET bodies are not supported here)."""
        lines = [f"{method} {_request_target(url)} HTTP/1.1"]
        for name, value in sorted((headers or {}).items()):
            lines.append(f"{name}: {value}")
        block = _CRLF.join(line.encode("ascii", "replace") for line in lines) + _CRLF + _CRLF
        return self._typed(
            warc_type="request",
            url=url,
            payload=block,
            content_type="application/http;msgtype=request",
        )

    def response(
        self,
        url: str,
        *,
        status: int,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        body_path: str | None = None,
        truncated: str | None = None,
        request_id: str | None = None,
    ) -> str:
        """One HTTP response record; ``body_path`` streams a spilled body.

        ``truncated`` uses the standard vocabulary (``length`` for byte caps)
        and is emitted as WARC-Truncated so readers know the body is partial.
        """
        reason = _HTTP_STATUS_REASONS.get(status, "")
        head_lines = [f"HTTP/1.1 {status} {reason}".rstrip()]
        for name, value in sorted((headers or {}).items()):
            head_lines.append(f"{name}: {value}")
        head = _CRLF.join(line.encode("latin-1", "replace") for line in head_lines) + _CRLF + _CRLF

        extra: list[tuple[str, str]] = []
        if truncated:
            extra.append(("WARC-Truncated", truncated))
        if request_id:
            extra.append(("WARC-Concurrent-To", request_id))

        if body is None and body_path:
            with open(body_path, "rb") as fh:
                body = fh.read()
        body = body or b""

        payload_digest = _digest(body)
        block = head + body
        record_id = self._write(
            warc_type="response",
            url=url,
            payload=block,
            content_type="application/http;msgtype=response",
            payload_digest=payload_digest,
            extra_fields=extra,
        )
        return record_id

    def metadata(
        self,
        url: str,
        fields: dict[str, str],
        *,
        concurrent_to: str | None = None,
    ) -> str:
        """A warc-fields record carrying freegem's fetch annotations."""
        extra: list[tuple[str, str]] = []
        if concurrent_to:
            extra.append(("WARC-Concurrent-To", concurrent_to))
        return self._write(
            warc_type="metadata",
            url=url,
            payload=_field_lines(sorted(fields.items())),
            content_type="application/warc-fields",
            extra_fields=extra,
        )

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._fh.close()

    def __enter__(self) -> WarcWriter:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── internals ────────────────────────────────────────────────────────

    def _typed(
        self,
        *,
        warc_type: str,
        url: str,
        payload: bytes,
        content_type: str,
        payload_digest: str | None = None,
    ) -> str:
        return self._write(
            warc_type=warc_type,
            url=url,
            payload=payload,
            content_type=content_type,
            payload_digest=payload_digest,
        )

    def _write(
        self,
        *,
        warc_type: str,
        url: str = "",
        payload: bytes,
        content_type: str,
        payload_digest: str | None = None,
        extra_fields: list[tuple[str, str]] | None = None,
    ) -> str:
        """Serialize one record: headers, blank line, block, blank line."""
        record_id = f"<urn:uuid:{self._uuid_gen()}>"
        fields: list[tuple[str, str]] = [
            ("WARC-Record-ID", record_id),
            ("WARC-Date", _warc_date(self._clock())),
            ("WARC-Type", warc_type),
        ]
        if url:
            fields.append(("WARC-Target-URI", _target_uri(url)))
        if self._warcinfo_id and warc_type != "warcinfo":
            fields.append(("WARC-Warcinfo-ID", self._warcinfo_id))
        if payload_digest:
            fields.append(("WARC-Payload-Digest", payload_digest))
        fields.append(("Content-Type", content_type))
        if extra_fields:
            fields.extend(extra_fields)
        fields.append(("Content-Length", str(len(payload))))

        block_digest = _digest(payload)
        lines = [f"{WARC_VERSION}"]
        lines.extend(f"{name}: {value}" for name, value in fields)
        lines.append(f"WARC-Block-Digest: {block_digest}")
        record = (
            _CRLF.join(line.encode("utf-8") for line in lines)
            + _CRLF
            + _CRLF
            + payload
            + _CRLF
            + _CRLF
        )
        # _field_lines raised earlier for bad names; _write itself never fails
        # on content, so holding the lock across the write is enough.
        self._fh.write(record)
        self.records += 1
        return record_id


# ── reading / verification ───────────────────────────────────────────────────


def iter_records(path: str, *, max_block: int = _MAX_BLOCK_BYTES) -> Iterator[WarcRecord]:
    """Stream records from a WARC file; refuse structural damage loudly.

    Content-Length drives the block read, so a truncated tail raises
    :class:`WARCError` instead of yielding a phantom record.
    """
    with open(path, "rb") as fh:
        offset = 0
        while True:
            # Header block: bytes up to the first CRLF CRLF.
            header_buf = bytearray()
            while True:
                line = fh.readline()
                if not line:
                    return  # clean EOF between records
                offset += len(line)
                header_buf.extend(line)
                if header_buf.endswith(_CRLF + _CRLF):
                    break
                if len(header_buf) > 65536:
                    raise WARCError(f"{path}: header block over 64 KiB at byte {offset}")
            header_text = header_buf.decode("utf-8", "replace")
            lines = header_text.split(_CRLF.decode("ascii"))
            lines = [ln for ln in lines if ln != ""]
            if not lines or not lines[0].startswith("WARC/"):
                raise WARCError(f"{path}: record at byte {offset} does not start with WARC/")
            headers: dict[str, str] = {}
            content_length: int | None = None
            for hline in lines[1:]:
                if ":" not in hline:
                    raise WARCError(f"{path}: malformed WARC header line: {hline!r}")
                name, _, value = hline.partition(":")
                headers[name.strip().lower()] = value.strip()
                if name.strip().lower() == "content-length":
                    try:
                        content_length = int(value.strip())
                    except ValueError as exc:
                        raise WARCError(f"{path}: bad Content-Length {value!r}") from exc
            if content_length is None:
                raise WARCError(f"{path}: record at byte {offset} has no Content-Length")
            if content_length > max_block:
                raise WARCError(f"{path}: Content-Length {content_length} exceeds the read cap")
            payload = fh.read(content_length)
            if len(payload) != content_length:
                raise WARCError(f"{path}: truncated block at byte {offset}")
            offset += content_length
            tail = fh.read(2)
            if tail != _CRLF:
                raise WARCError(f"{path}: record at byte {offset} is not CRLF-terminated")
            offset += 2
            yield WarcRecord(headers=headers, payload=payload)


def verify(path: str) -> dict[str, Any]:
    """Check every record: required fields, Content-Length, both digests."""
    checked = 0
    bad: list[str] = []
    types: dict[str, int] = {}
    for index, record in enumerate(iter_records(path)):
        checked += 1
        problems: list[str] = []
        for required in ("warc-type", "warc-date", "warc-record-id", "content-length"):
            if required not in record.headers:
                problems.append(f"missing {required}")
        declared = int(record.headers.get("content-length", "-1"))
        if declared != len(record.payload):
            problems.append(f"content-length {declared} != block {len(record.payload)}")
        block_digest = record.headers.get("warc-block-digest", "")
        if block_digest and block_digest != _digest(record.payload):
            problems.append("block digest mismatch")
        payload_digest = record.headers.get("warc-payload-digest", "")
        if payload_digest:
            body = _response_body(record.payload) if record.type == "response" else record.payload
            if payload_digest != _digest(body):
                problems.append("payload digest mismatch")
        wtype = record.type
        types[wtype] = types.get(wtype, 0) + 1
        if problems:
            bad.append(f"record #{index} ({wtype or 'unknown'}): " + "; ".join(problems))
    return {"records": checked, "types": types, "problems": bad, "ok": not bad}


def summary(path: str) -> dict[str, Any]:
    """Counts by record type, targets, truncations, and wall annotations."""
    types: dict[str, int] = {}
    targets: set[str] = set()
    truncated = 0
    walls: dict[str, int] = {}
    for record in iter_records(path):
        types[record.type] = types.get(record.type, 0) + 1
        if record.target and record.type != "warcinfo":
            targets.add(record.target)
        if record.headers.get("warc-truncated"):
            truncated += 1
        wall = record.field("freegem-wall-class")
        if not wall and record.type == "metadata":
            wall = _warc_fields_field(record.payload, "freegem-wall-class")
        if wall:
            walls[wall] = walls.get(wall, 0) + 1
    return {
        "records": sum(types.values()),
        "types": types,
        "distinct_targets": len(targets),
        "truncated_records": truncated,
        "wall_classes": walls,
    }


def _warc_fields_field(payload: bytes, name: str) -> str:
    """One field from an application/warc-fields block ('' when absent)."""
    for line in payload.decode("utf-8", "replace").split("\r\n"):
        key, _, value = line.partition(":")
        if key.strip().lower() == name:
            return value.strip()
    return ""


def _response_body(block: bytes) -> bytes:
    """The payload proper of an HTTP response block (after the blank line)."""
    separator = b"\r\n\r\n"
    index = block.find(separator)
    return block[index + len(separator) :] if index != -1 else b""
