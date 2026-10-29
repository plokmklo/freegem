"""Tests for REST jobs, artifacts, health, audit logs, and authentication."""

from __future__ import annotations

import os
import time
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from freegem.config import BridgeConfig
from freegem.jobs import GLOBAL_JOB_MANAGER, Job, JobManager
from freegem.metrics import get_audit_logs, record_audit_event
from freegem.server import GeminiHandler


class MockServer:
    """Matches what GeminiHandler.cfg / GeminiHandler._metrics read:
    ``self.server.config`` and ``self.server.server_metrics``."""

    def __init__(self, cfg: BridgeConfig | None = None) -> None:
        self.config = cfg or BridgeConfig(
            guard=True,
        )
        self.server_metrics = MagicMock()


def _stub_handler(cfg: BridgeConfig | None = None, req_id: str = "req-test") -> GeminiHandler:
    handler = GeminiHandler.__new__(GeminiHandler)
    # GeminiHandler.server is typed as HTTPServer; tests substitute the stub
    handler.server = MockServer(cfg)  # type: ignore[assignment]
    handler._request_id = req_id
    handler.client_address = ("127.0.0.1", 0)
    return handler


def _await_job(job_id: str, timeout_sec: float = 5.0) -> Job:
    """Wait until a background job reaches a terminal state."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        job = GLOBAL_JOB_MANAGER.get_job(job_id)
        assert job is not None
        if job.status in ("completed", "failed"):
            return job
        time.sleep(0.05)
    job = GLOBAL_JOB_MANAGER.get_job(job_id)
    assert job is not None, "job vanished"
    return job


class TestServerJobsAndAuth(unittest.TestCase):
    def setUp(self) -> None:
        self.job_mgr = JobManager()

    def test_job_manager_creation_and_artifacts(self) -> None:
        job = self.job_mgr.create_job("scrape", {"url": "https://example.com"})
        self.assertIsNotNone(job.id)
        self.assertEqual(job.status, "queued")

        retrieved = self.job_mgr.get_job(job.id)
        assert retrieved is not None
        self.assertEqual(retrieved.id, job.id)

        art = self.job_mgr.save_artifact(job.id, "test.txt", "text/plain", b"sample content")
        self.assertIsNotNone(art.id)
        self.assertTrue(os.path.exists(art.file_path))

        get_art = self.job_mgr.get_artifact(art.id)
        assert get_art is not None
        self.assertEqual(get_art.filename, "test.txt")

    def test_audit_logs(self) -> None:
        record_audit_event("test_action", "127.0.0.1", "req-123", {"detail": "info"})
        logs = get_audit_logs(10)
        self.assertTrue(len(logs) > 0)
        latest = logs[-1]
        self.assertEqual(latest["action"], "test_action")
        self.assertEqual(latest["client_ip"], "127.0.0.1")

    def test_extract_job_validates_fields_and_completes(self) -> None:
        """The 'extract' job type (2.8.0) must build a schema from fields[]
        and run BridgeProvider extraction end to end (generate mocked)."""

        def fake_generate(cfg: Any, prompt: str, model_id: int = 0, think_mode: int = 0) -> str:
            _ = cfg, prompt, model_id, think_mode
            return '{"name": "Widget", "price": 5, "in_stock": true, "category": "other"}'

        with patch("freegem.llm_extract.generate", side_effect=fake_generate):
            job = GLOBAL_JOB_MANAGER.create_job(
                "extract",
                {
                    "text": "The Widget costs five dollars and is available.",
                    "fields": [
                        {"name": "name", "type": "string"},
                        {"name": "price", "type": "number"},
                        {"name": "in_stock", "type": "boolean"},
                        {
                            "name": "category",
                            "type": "enum",
                            "enum": ["electronics", "books", "other"],
                        },
                    ],
                },
            )
            from freegem.jobs import execute_job

            execute_job(job, BridgeConfig(guard=True, log_requests=False))

        done = _await_job(job.id)
        self.assertEqual(done.status, "completed")
        assert done.result is not None
        self.assertEqual(done.result["data"]["name"], "Widget")
        self.assertEqual(done.result["data"]["price"], 5)
        self.assertEqual(done.result["data"]["in_stock"], True)

    def test_extract_job_without_fields_fails_cleanly(self) -> None:
        job = GLOBAL_JOB_MANAGER.create_job("extract", {"text": "hello"})
        from freegem.jobs import execute_job

        execute_job(job, BridgeConfig(guard=True, log_requests=False))
        done = _await_job(job.id)
        self.assertEqual(done.status, "failed")
        assert done.error is not None
        self.assertIn("fields", done.error)

    def test_selenium_job_params_are_filtered(self) -> None:
        """Unknown params must not reach SeleniumOptions' constructor."""
        from freegem.selenium_fetcher import SeleniumOptions

        allowed = {
            "url",
            "grid_url",
            "wait_selector",
            "implicit_wait_sec",
            "page_load_timeout_sec",
            "viewport_width",
            "viewport_height",
            "user_agent",
            "storage_state",
            "capture_screenshot",
            "screenshot_path",
            "capture_pdf",
            "pdf_path",
        }
        params: dict[str, Any] = {
            "url": "https://example.org",
            "evil_param": "x",
            "capture_screenshot": True,
        }
        filtered = {k: v for k, v in params.items() if k in allowed}
        opts = SeleniumOptions(**filtered)
        self.assertEqual(opts.url, "https://example.org")
        self.assertFalse(hasattr(opts, "evil_param"))
        self.assertTrue(opts.capture_screenshot)


if __name__ == "__main__":
    unittest.main()


class JobsRestHttpTests(unittest.TestCase):
    """The jobs REST surface must actually be ROUTED over real HTTP.

    Regression: POST /v1/jobs fell into the /v1/ chat catch-all (400
    "no prompt content") and the GET jobs/artifact handlers were never
    wired into do_GET at all. These tests speak real sockets so routing
    cannot rot silently again.
    """

    def setUp(self) -> None:
        import threading

        from freegem.jobs import reset_job_runner_for_tests
        from freegem.server import ThreadedServer

        reset_job_runner_for_tests()
        self.cfg = BridgeConfig(
            guard=True, log_requests=False, api_key="jobs-rest-key", job_db="memory"
        )
        self.server = ThreadedServer(("127.0.0.1", 0), self.cfg, max_concurrent_requests=8)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        from freegem.jobs import reset_job_runner_for_tests

        self.server.shutdown()
        reset_job_runner_for_tests()

    def _req(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        import http.client
        import json

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(
                method,
                path,
                body=json.dumps(body) if body is not None else None,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer jobs-rest-key",
                    **(headers or {}),
                },
            )
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
        finally:
            conn.close()

    def test_post_jobs_is_routed(self) -> None:
        # An unknown type keeps this hermetic: the runner accepts it, the
        # body fails cleanly, and no network is touched.
        status, payload = self._req("POST", "/v1/jobs", {"type": "no-such-type", "params": {}})
        self.assertEqual(status, 202)
        self.assertTrue(str(payload.get("id", "")).startswith("job-"))

    def test_get_jobs_list_and_single(self) -> None:
        status, created = self._req("POST", "/v1/jobs", {"type": "no-such-type", "params": {}})
        self.assertEqual(status, 202)
        job_id = created["id"]
        status, listing = self._req("GET", "/v1/jobs")
        self.assertEqual(status, 200)
        ids = [j["id"] for j in listing.get("data", [])]
        self.assertIn(job_id, ids)
        status, one = self._req("GET", f"/v1/jobs/{job_id}")
        self.assertEqual(status, 200)
        self.assertEqual(one["id"], job_id)
        status, results = self._req("GET", f"/v1/jobs/{job_id}/results")
        self.assertEqual(status, 200)
        self.assertEqual(results["id"], job_id)

    def test_delete_unknown_job_404(self) -> None:
        status, _ = self._req("DELETE", "/v1/jobs/job-does-not-exist")
        self.assertEqual(status, 404)

    def test_get_artifact_is_routed(self) -> None:
        job = GLOBAL_JOB_MANAGER.create_job("scrape", {})
        art = GLOBAL_JOB_MANAGER.save_artifact(job.id, "hello.txt", "text/plain", b"hello artifact")
        status, raw = self._req("GET", f"/v1/artifacts/{art.id}")
        self.assertEqual(status, 200)
        self.assertIn("hello artifact", raw)

    def test_unauthenticated_post_401(self) -> None:
        import http.client
        import json

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(
                "POST",
                "/v1/jobs",
                body=json.dumps({"type": "scrape", "params": {}}),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, 401)
            resp.read()
        finally:
            conn.close()

    # -- auth/header parsing edge cases -------------------------------------

    def test_bearer_scheme_is_case_insensitive(self) -> None:
        """RFC 6750: the scheme is case-insensitive; lowercase must not 401."""
        status, _ = self._req(
            "POST",
            "/v1/jobs",
            {"type": "no-such-type", "params": {}},
            headers={"Authorization": "bearer jobs-rest-key"},
        )
        self.assertEqual(status, 202)

    def test_unauthenticated_get_401_carries_request_id(self) -> None:
        """GET never set _request_id, so 401 audits and responses had none."""
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request("GET", "/v1/jobs", headers={"Authorization": "Bearer wrong"})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 401)
            self.assertTrue(resp.headers.get("X-Request-Id"))
        finally:
            conn.close()

    # -- request validation on POST /v1/jobs --------------------------------

    def test_post_jobs_non_object_body_is_400(self) -> None:
        """A JSON array body used to 500 with an AttributeError message."""
        status, payload = self._req("POST", "/v1/jobs", [1, 2, 3])
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_post_jobs_missing_type_is_400(self) -> None:
        status, _ = self._req("POST", "/v1/jobs", {})
        self.assertEqual(status, 400)

    def test_post_jobs_non_string_type_is_400(self) -> None:
        status, _ = self._req("POST", "/v1/jobs", {"type": 123, "params": {}})
        self.assertEqual(status, 400)

    def test_post_jobs_params_must_be_object(self) -> None:
        """A list params used to return 202, then crash a worker thread."""
        status, payload = self._req("POST", "/v1/jobs", {"type": "t", "params": [1, 2]})
        self.assertEqual(status, 400)
        self.assertIn("params", str(payload))

    # -- body framing on POST /v1/jobs (raw sockets; http.client cannot
    #    declare a Content-Length that disagrees with what it sends) --------

    def _raw_request(self, request: bytes, timeout: float = 5.0) -> tuple[bytes, bytes]:
        """Send a hand-built request; return (head, body) of the response."""
        import socket

        sock = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        try:
            sock.sendall(request)
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                response += chunk
            head, _, rest = response.partition(b"\r\n\r\n")
            marker = b"Content-Length:"
            if marker in head:
                length = int(
                    head.lower().split(marker.lower(), 1)[1].split(b"\r\n", 1)[0].strip()
                )
                while len(rest) < length:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    rest += chunk
            return head, rest
        finally:
            sock.close()

    def test_post_jobs_oversized_declared_length_is_413(self) -> None:
        """The chat face 413'd; the jobs face fell into the generic 500.

        The oversized body also stays unread, so the response must close the
        connection or the leftover bytes desync the keep-alive stream (#27).
        """
        head, _ = self._raw_request(
            b"POST /v1/jobs HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Authorization: Bearer jobs-rest-key\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 99999999999\r\n\r\n"
            b"{}"
        )
        self.assertTrue(head.startswith(b"HTTP/1.1 413"), head[:80])
        self.assertIn(b"connection: close", head.lower())

    def test_post_jobs_invalid_content_length_is_400(self) -> None:
        head, _ = self._raw_request(
            b"POST /v1/jobs HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Authorization: Bearer jobs-rest-key\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: not-a-number\r\n\r\n"
            b"{}"
        )
        self.assertTrue(head.startswith(b"HTTP/1.1 400"), head[:80])
        self.assertIn(b"connection: close", head.lower())

    # -- routing ------------------------------------------------------------

    def test_delete_requires_v1_jobs_prefix(self) -> None:
        """DELETE /anything/jobs/{id} used to cancel a real job (200)."""
        job = GLOBAL_JOB_MANAGER.create_job("scrape", {})
        status, _ = self._req("DELETE", f"/anything/jobs/{job.id}")
        self.assertEqual(status, 400)
        fetched = GLOBAL_JOB_MANAGER.get_job(job.id)
        assert fetched is not None
        self.assertEqual(fetched.status, "queued")

    def test_delete_still_works_on_canonical_path(self) -> None:
        """Backward compatibility: /v1/jobs/{id} keeps cancelling."""
        status, _ = self._req("DELETE", "/v1/jobs/job-does-not-exist")
        self.assertEqual(status, 404)

    def test_get_jobs_unknown_suffix_is_404(self) -> None:
        """The suffix branch used to fall through and return the job (200)."""
        status, created = self._req("POST", "/v1/jobs", {"type": "no-such-type", "params": {}})
        self.assertEqual(status, 202)
        status, _ = self._req("GET", f"/v1/jobs/{created['id']}/bogus")
        self.assertEqual(status, 404)

    def test_artifact_read_failure_does_not_leak_filesystem_paths(self) -> None:
        """A vanished artifact file 500'd with the OS error and full path."""
        import os

        job = GLOBAL_JOB_MANAGER.create_job("scrape", {})
        art = GLOBAL_JOB_MANAGER.save_artifact(job.id, "x.txt", "text/plain", b"data")
        os.remove(art.file_path)
        with patch("freegem.server.os.path.exists", return_value=True):
            status, raw = self._req("GET", f"/v1/artifacts/{art.id}")
        self.assertEqual(status, 500)
        self.assertNotIn(art.file_path, str(raw))
        self.assertIn("failed to read artifact", str(raw))

