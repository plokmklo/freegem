"""Keep-alive connection pool over the SSRF-pinned dial (issue #94).

Every ``crawl_fetch`` today pays a full TCP + TLS handshake: stdlib
``AbstractHTTPHandler.do_open`` forces ``Connection: close`` and then
closes the socket, unconditionally -- a comment in CPython's source says
the addinfourl wrapper "isn't prepared to deal with a persistent
connection". For a 1000-seed campaign that is 1000+ handshakes to hosts
that would happily have kept one connection open.

This module pools the CONNECTION OBJECTS (``http.client`` connections
natively support serial reuse) without touching the guard: a pooled
connection dials through the SAME resolve -> check-all-public -> pin
sequence as :mod:`freegem.ssrf`'s per-request classes. The invariant is
preserved structurally:

- the pool key is ``(scheme, host, port)`` and connections are dialed at
  CREATION time through the pinned path -- a checked-out live socket is
  already pinned to an address that passed the public-IP check;
- a REUSED connection re-dials nothing (the whole anti-rebinding point:
  no second resolution window exists), and when a stale reuse fails the
  replacement is dialed through the pinned path AGAIN;
- redirect hops re-dispatch through the same opener, so every hop hits
  the pool with its own host key and gets its own pinned dial;
- proxy mode never pools (the dial cannot be pinned there anyway).

Honest limits, by design:

- only fully-consumed responses return to the pool. Length-delimited
  bodies signal that through ``HTTPResponse._close_conn`` (the hook fires
  exactly when Content-Length is satisfied); chunked bodies through a
  ``read()`` returning ``b""`` with the stream still open (the terminal
  chunk). A response abandoned mid-body (cap exceeded, type refused,
  exception) never re-enters the pool -- its connection is closed;
- a server that answers ``Connection: close`` (or HTTP/1.0 without
  keep-alive) marks ``will_close``; such connections are closed at
  consumption, not pooled;
- per-key and total caps plus an idle TTL bound the socket budget, and
  ``close()`` is deterministic;
- one stale-connection retry per request: an OSError on a REUSED
  connection drops it and dials a fresh pinned one; a failure on a fresh
  dial propagates as the normal fetch taxonomy (the outcome never lies).

The default resolver and the default (verified) TLS context only: the
ladder's exotic attempts (DoH resolver, relaxed TLS) dial fresh, so a
pool entry never mixes verification postures.
"""

from __future__ import annotations

import contextlib
import http.client
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Any

from ..ssrf import Resolver, SafeRedirect, pick_public_ips

__all__ = ["KeepAlivePool", "PooledHTTPHandler", "PooledHTTPSHandler", "build_pooled_opener"]


class KeepAlivePool:
    """Bounded, thread-safe pool of pinned keep-alive connections.

    ``checkout`` returns a live-or-fresh connection for a key; ``checkin``
    returns a fully-consumed one; abandoned connections are closed. All
    bookkeeping is under one lock (the campaign runs fetches in a
    ThreadPoolExecutor; a pool entry is exclusively owned between checkout
    and checkin, so sockets are never shared mid-response).
    """

    def __init__(
        self,
        *,
        per_key_cap: int = 2,
        total_cap: int = 32,
        idle_ttl_sec: float | None = 30.0,
        resolver: Resolver | None = None,
    ) -> None:
        self._per_key_cap = max(1, per_key_cap)
        self._total_cap = max(1, total_cap)
        # None disables idle expiry entirely; 0.0 expires every idle entry at
        # checkout (``if self._idle_ttl`` would silently turn 0 into "never").
        self._idle_ttl = idle_ttl_sec
        self._resolver: Resolver = resolver or pick_public_ips
        self._entries: dict[
            tuple[str, str, int], deque[tuple[float, http.client.HTTPConnection]]
        ] = {}
        self._lock = threading.Lock()
        # honest counters, surfaced in campaign events/reports
        self.stats = {
            "dials": 0,  # connections created (each dialed through the pinned path)
            "reuses": 0,  # checkouts that reused a live connection
            "stale_drops": 0,  # reuses that failed and were replaced
            "checkins": 0,  # responses fully consumed and pooled again
            "expired": 0,  # idle entries dropped at checkout (TTL)
            "capped": 0,  # checkins refused by the caps (closed instead)
            "live": 0,  # entries currently pooled
        }

    # ── lifecycle ────────────────────────────────────────────────────────────

    def checkout(
        self,
        key: tuple[str, str, int],
        *,
        timeout: float | None,
        factory: Any,
    ) -> tuple[http.client.HTTPConnection, bool]:
        """A connection for ``key``: (reused, True) or a fresh (conn, False).

        ``factory(host, port, timeout, pool)`` builds a NOT-YET-DIALED pinned
        connection; the dial happens lazily inside ``request()`` (stdlib
        ``auto_open``), which is also where a fresh dial's failures surface.
        Expired idle entries are closed and counted.
        """
        with self._lock:
            bucket = self._entries.get(key)
            while bucket:
                idle_since, conn = bucket.popleft()
                self.stats["live"] = max(0, self.stats["live"] - 1)
                if self._idle_ttl is not None and (time.monotonic() - idle_since) > self._idle_ttl:
                    self._expire(conn)
                    continue
                if conn.sock is None:  # dead entry: drop, look for another
                    self._expire(conn)
                    continue
                if timeout is not None:
                    conn.timeout = timeout
                    try:
                        conn.sock.settimeout(timeout)
                    except OSError:
                        self._expire(conn)
                        self.stats["stale_drops"] += 1
                        continue
                self.stats["reuses"] += 1
                return conn, True
        conn = factory(key[1], key[2], timeout, self)
        with self._lock:
            self.stats["dials"] += 1
        return conn, False

    def checkin(self, key: tuple[str, str, int], conn: http.client.HTTPConnection) -> None:
        """Return a fully-consumed connection to the pool (or close it)."""
        with self._lock:
            bucket = self._entries.setdefault(key, deque())
            if len(bucket) >= self._per_key_cap or self._stats_live() >= self._total_cap:
                self.stats["capped"] += 1
                self._close_quietly(conn)
                return
            bucket.append((time.monotonic(), conn))
            self.stats["live"] += 1
            self.stats["checkins"] += 1

    def discard(self, conn: http.client.HTTPConnection) -> None:
        """An abandoned/errored connection: close it, pool nothing."""
        self._close_quietly(conn)

    def note_stale_drop(self) -> None:
        with self._lock:
            self.stats["stale_drops"] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self.stats)

    def close(self) -> None:
        """Deterministic shutdown: every pooled socket closed, map emptied."""
        with self._lock:
            for bucket in self._entries.values():
                while bucket:
                    _idle, conn = bucket.popleft()
                    self._close_quietly(conn)
            self._entries.clear()
            self.stats["live"] = 0

    # ─── internals (call with the lock held) ─────────────────────────────────

    def _stats_live(self) -> int:
        return self.stats["live"]

    def _expire(self, conn: http.client.HTTPConnection) -> None:
        self.stats["expired"] += 1
        self._close_quietly(conn)

    @staticmethod
    def _close_quietly(conn: http.client.HTTPConnection) -> None:
        with contextlib.suppress(OSError):
            conn.close()


# ─── pinned connection classes (pool flavor) ─────────────────────────────────


class _PooledHTTPResponse(http.client.HTTPResponse):
    """A response that returns its connection to the pool when consumed.

    Hooks live on the PUBLIC ``read``/``close`` only (the private
    ``_close_conn`` is typeshed-undeclared and CPython may never call it
    for chunked bodies). "Fully consumed" is: a ``read()`` returning
    ``b""`` with the stream closed (Content-Length satisfied on an earlier
    read, HEAD, EOF) -- or, for chunked bodies, the terminal ``b""`` with
    the stream still open, where the fp view is closed here (the SOCKET
    survives; that is how http.client reuse works). ``Connection: close``
    responses discard instead of pooling. A ``close()`` before consumption
    means the caller abandoned the body: discard, never pool. Checkin is
    one-shot -- ``_pool`` is cleared on the first fire, so a second
    ``read()`` or a late ``close()`` cannot double-return the connection.
    """

    _pool: KeepAlivePool | None = None
    _ka_conn: _PooledConnectionBase | None = None

    def _release(self, *, consumed: bool) -> None:
        pool, conn = self._pool, self._ka_conn
        if pool is None or conn is None:
            return
        self._pool = None  # one-shot: never checkin or discard twice
        self._ka_conn = None
        if consumed and not self.will_close:
            pool.checkin(conn.key, conn)
        else:
            # abandoned mid-body, or the server said Connection: close:
            # the socket is worthless either way
            pool.discard(conn)

    def read(self, amt: int | None = None) -> bytes:
        data = super().read(amt)
        if not data and self._pool is not None and self._ka_conn is not None:
            if self.chunked and self.fp is not None and (amt is None or amt > 0):
                # terminal chunk with the stream still open: close the fp
                # view (the socket survives) so the release is clean.
                # typeshed declares fp non-optional, but CPython's own
                # _close_conn nils it exactly like this.
                fp = self.fp
                self.fp = None  # type: ignore[assignment]
                with contextlib.suppress(Exception):
                    fp.close()
            if self.fp is None:
                self._release(consumed=True)
        return data

    def close(self) -> None:
        consumed = self.fp is None
        super().close()
        if not consumed:
            # closed before the body was fully read: abandoned, never pooled
            self._release(consumed=False)


class _PooledConnectionBase(http.client.HTTPConnection):
    """Base for pooled connections: pinned dial + pooled response wiring.

    ``__init__`` is a pure PASS-THROUGH. That is load-bearing: the HTTPS
    subclass sits between :class:`http.client.HTTPSConnection` and this
    class in the MRO, and stdlib ``HTTPSConnection.__init__`` calls
    ``super().__init__(host, port, timeout, source_address, blocksize)``
    POSITIONALLY -- a ``__init__(host, pool, ...)`` signature here broke
    every https dial with a TypeError (caught live by the 1000-page run,
    not by the http-only test server). Subclasses set ``_pool``/``key``
    AFTER their own ``super().__init__`` chain completes.
    """

    response_class = _PooledHTTPResponse
    #: set by the concrete subclasses after their super().__init__ chain
    _pool: KeepAlivePool
    #: the (scheme, host, port) pool key, set by the concrete subclasses
    key: tuple[str, str, int]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    def getresponse(self) -> http.client.HTTPResponse:
        response = super().getresponse()
        if isinstance(response, _PooledHTTPResponse):
            # response_class IS _PooledHTTPResponse; the isinstance also
            # narrows the type so the attribute writes type-check
            response._pool = self._pool
            response._ka_conn = self
        return response


class _PooledConnection(_PooledConnectionBase):
    """HTTP flavor: dial only pre-checked public IPs, in order."""

    def __init__(self, host: str, pool: KeepAlivePool, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._pool = pool
        # self.host is the bare hostname and self.port the effective port
        # (http.client normalizes host:port in __init__): canonical key form.
        self.key = ("http", self.host, self.port)

    def connect(self) -> None:
        # The SAME contract as ssrf._pinned_http_class: dial only pre-checked
        # public IPs, in order; self.host is never resolved again. Dual-stack
        # hosts keep working because the whole list is tried in sequence.
        ips = self._pool._resolver(self.host)
        last_error: OSError | None = None
        for ip in ips:
            try:
                self.sock = socket.create_connection((ip, self.port), self.timeout)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                return
            except OSError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise OSError("no checked addresses to dial")


class _PooledHTTPSConnection(http.client.HTTPSConnection, _PooledConnectionBase):
    """HTTPS flavor: SNI + certificate verification stay on the hostname."""

    #: bound by :func:`_pooled_https_class` (the handler constructs only
    #: bound subclasses, so the annotation-only class attr is safe)
    _pool_tls: ssl.SSLContext

    def __init__(
        self, host: str, pool: KeepAlivePool, context: ssl.SSLContext, **kwargs: Any
    ) -> None:
        # https://github.com/python/cpython/issues GH-86661 note: passing
        # context= through the constructor is the supported path.
        super().__init__(host, context=context, **kwargs)
        self._pool = pool
        # self.host is the BARE hostname (http.client splits host:port in
        # __init__), so the key matches the handler's parsed form below.
        self.key = ("https", self.host, self.port)

    def connect(self) -> None:
        ips = self._pool._resolver(self.host)
        last_error: OSError | None = None
        sock: socket.socket | None = None
        for ip in ips:
            try:
                sock = socket.create_connection((ip, self.port), self.timeout)
                break
            except OSError as exc:
                last_error = exc
        if sock is None:
            if last_error is not None:
                raise last_error
            raise OSError("no checked addresses to dial")
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # SNI and verification on the ORIGINAL hostname, exactly like
        # ssrf._pinned_https_class: pinning cannot downgrade TLS.
        self.sock = self._pool_tls.wrap_socket(sock, server_hostname=self.host)


def _pooled_https_class(context: ssl.SSLContext) -> type[_PooledHTTPSConnection]:
    """Bind the TLS context onto the connection class (one per opener)."""

    class _Bound(_PooledHTTPSConnection):
        _pool_tls = context

        def __init__(self, host: str, pool: KeepAlivePool, **kwargs: Any) -> None:
            kwargs.pop("context", None)
            super().__init__(host, pool, context=context, **kwargs)

    return _Bound


class _PooledHandler:
    """Shared keep-alive do_open for the http/https handlers.

    Modeled on stdlib ``AbstractHTTPHandler.do_open`` with exactly two
    changes: no forced ``Connection: close``, and the socket is returned to
    the pool by the response hooks instead of being closed here. A stale
    REUSED connection gets one fresh-pinned retry; fresh-dial failures
    propagate as ``URLError`` for the honest fetch taxonomy.
    """

    def __init__(self, pool: KeepAlivePool) -> None:
        self._pool = pool

    def do_open_pooled(
        self,
        conn_class: Any,
        req: urllib.request.Request,
    ) -> Any:
        pool = self._pool
        if not req.host:
            raise urllib.error.URLError("no host given")
        scheme = "https" if isinstance(self, PooledHTTPSHandler) else "http"
        parsed = urllib.parse.urlsplit(req.full_url)
        # Canonical key: bare hostname + effective port, matching the form
        # the connection classes record (http.client normalizes host:port).
        host_bare = parsed.hostname or req.host
        port = parsed.port or (443 if scheme == "https" else 80)
        key = (scheme, host_bare, port)

        default_port = 443 if scheme == "https" else 80

        def factory(h: str, p: int, timeout: float | None, pl: KeepAlivePool) -> Any:
            # http.client takes the port INSIDE the host argument ("host:port")
            # exactly like stdlib do_open's req.host; a bare host would silently
            # dial the scheme's default port. The conn normalizes it back into
            # self.host/self.port, so the pool key stays the bare form.
            host_arg = h if p == default_port else f"{h}:{p}"
            return conn_class(host_arg, pl, timeout=timeout)

        conn, reused = pool.checkout(key, timeout=req.timeout, factory=factory)
        headers = dict(req.unredirected_hdrs)
        headers.update({k: v for k, v in req.headers.items() if k not in headers})
        # NO forced "Connection: close" -- that forced header is the entire
        # reason stdlib urllib cannot keep connections alive.
        headers = {name.title(): val for name, val in headers.items()}
        method = req.get_method()
        selector = req.selector
        data = req.data
        encode_chunked = req.has_header("Transfer-encoding")

        def attempt(c: Any) -> Any:
            c.request(method, selector, data, headers, encode_chunked=encode_chunked)
            return c.getresponse()

        try:
            response = attempt(conn)
        except OSError as err:
            if reused:
                # Stale keep-alive socket: the classic race (server closed an
                # idle connection). Drop it, dial a FRESH pinned connection,
                # retry once. A fresh-dial failure propagates honestly.
                pool.discard(conn)
                pool.note_stale_drop()
                conn = factory(host_bare, port, req.timeout, pool)
                with pool._lock:
                    pool.stats["dials"] += 1
                try:
                    response = attempt(conn)
                except OSError as err2:
                    conn.close()
                    raise urllib.error.URLError(err2) from err2
            else:
                conn.close()
                raise urllib.error.URLError(err) from err
        response.url = req.get_full_url()
        response.msg = response.reason
        return response


class PooledHTTPHandler(urllib.request.HTTPHandler, _PooledHandler):
    """http_open through the pool (pinned dial at creation)."""

    def __init__(self, pool: KeepAlivePool) -> None:
        urllib.request.HTTPHandler.__init__(self)
        _PooledHandler.__init__(self, pool)

    def http_open(self, req: urllib.request.Request) -> Any:
        conn_class = _PooledConnection
        return self.do_open_pooled(conn_class, req)


class PooledHTTPSHandler(urllib.request.HTTPSHandler, _PooledHandler):
    """https_open through the pool (pinned dial + hostname-verified TLS)."""

    def __init__(self, pool: KeepAlivePool, context: ssl.SSLContext) -> None:
        urllib.request.HTTPSHandler.__init__(self, context=context)
        _PooledHandler.__init__(self, pool)
        self._conn_class = _pooled_https_class(context)

    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open_pooled(self._conn_class, req)


def build_pooled_opener(
    context: ssl.SSLContext,
    pool: KeepAlivePool,
    cookie_jar: http.cookiejar.CookieJar | None = None,
) -> urllib.request.OpenerDirector:
    """The guarded opener's direct-mode shape, with pooled handlers.

    Same audited-only handler stack as :func:`freegem.ssrf.build_guarded_opener`
    (no FTP/File/Data/Unknown handlers can sneak in); only the dial differs:
    http/https go through the pool. Proxy mode must NOT use this builder --
    pooling is disabled there (the dial cannot be pinned).
    """
    opener = urllib.request.OpenerDirector()
    cookie_handler: list[urllib.request.BaseHandler] = (
        [urllib.request.HTTPCookieProcessor(cookie_jar)] if cookie_jar is not None else []
    )
    for handler in (
        urllib.request.HTTPErrorProcessor(),
        urllib.request.HTTPDefaultErrorHandler(),
        PooledHTTPHandler(pool),
        PooledHTTPSHandler(pool, context),
        # Re-validation per redirect hop: the pooled handler re-dispatches
        # with the hop's own key, so cross-host redirects cannot inherit a
        # connection dialed for another host.
        SafeRedirect(),
        *cookie_handler,
    ):
        opener.add_handler(handler)
    return opener
