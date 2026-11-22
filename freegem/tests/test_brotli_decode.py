"""Brotli decode path against the REAL backend (not a duck-typed fake).

The 10k-site live run (2.16.2 era) classified 1,936 brotli responses as
``network`` with ``corrupt compressed body: 'brotli.Decompressor' object
has no attribute 'decompress'`` -- the streaming reader speaks the zlib
``decompressobj`` API while ``brotli``/``brotlicffi`` expose ``process()``.
The old test fake implemented ``decompress()`` and hid the mismatch. These
tests use the real installed backend end to end.
"""

from __future__ import annotations

import unittest
from typing import Any, ClassVar

from freegem.crawler.fetch import (
    ScrapeError,
    _inflate,
    _optional_decoder,
    read_body_stream,
)

try:
    import brotli as _brotli

    HAVE_BROTLI = True
except ImportError:  # pragma: no cover - CI installs the extra
    HAVE_BROTLI = False


@unittest.skipUnless(HAVE_BROTLI, "freegem[brotli] extra not installed")
class RealBrotliTests(unittest.TestCase):
    def test_shim_translates_decompress_to_process(self) -> None:
        wire = _brotli.compress(b"<html>hello brotli</html>")
        out = _inflate(_optional_decoder("br"), wire, "br")
        self.assertEqual(out, b"<html>hello brotli</html>")

    def test_streaming_reader_round_trip_multi_chunk(self) -> None:
        body = b"<html>" + b"<p>paragraph</p>" * 200 + b"</html>"
        wire = _brotli.compress(body)
        halves = [wire[: len(wire) // 2], wire[len(wire) // 2 :], b""]

        class _Reader:
            headers: ClassVar[dict[str, str]] = {"Content-Encoding": "br"}

            def __init__(self) -> None:
                self._chunks = iter(halves)

            def read(self, _n: int) -> bytes:
                return next(self._chunks)

        got, path, total = read_body_stream(
            _Reader(),
            max_bytes=1_000_000,
            spill_bytes=1_000_000,
        )
        self.assertIsNone(path)
        self.assertEqual(total, len(body))
        self.assertEqual(got, body, "incremental process() chunks must reassemble")

    def test_corrupt_brotli_raises_scrape_error(self) -> None:
        with self.assertRaises(ScrapeError):
            _inflate(_optional_decoder("br"), b"\xff\xff-not-brotli", "br")

    def test_backend_has_no_decompress_so_the_shim_is_load_bearing(self) -> None:
        import freegem.crawler.fetch as fetch_module

        dec = fetch_module._brotli.Decompressor()
        self.assertFalse(
            hasattr(dec, "decompress"),
            "backend grew a decompress(): the shim can be retired",
        )
        self.assertTrue(hasattr(dec, "process"))

    def test_advertised_encoding_matches_decoder(self) -> None:
        from freegem.crawler.fetch import supported_encodings

        encodings = supported_encodings()
        self.assertIn("br", encodings)
        decoder: Any = _optional_decoder("br")
        self.assertTrue(hasattr(decoder, "decompress"), "shim must speak the reader's API")


if __name__ == "__main__":
    unittest.main()
