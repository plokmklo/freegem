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
        self.config = cfg or BridgeConfig()
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
            handler = _stub_handler(BridgeConfig(log_requests=False), "req-extract-1")
            handler._run_job_background(job, handler.cfg)

        done = _await_job(job.id)
        self.assertEqual(done.status, "completed")
        assert done.result is not None
        self.assertEqual(done.result["data"]["name"], "Widget")
        self.assertEqual(done.result["data"]["price"], 5)
        self.assertEqual(done.result["data"]["in_stock"], True)

    def test_extract_job_without_fields_fails_cleanly(self) -> None:
        job = GLOBAL_JOB_MANAGER.create_job("extract", {"text": "hello"})
        handler = _stub_handler(BridgeConfig(log_requests=False), "req-extract-2")
        handler._run_job_background(job, handler.cfg)
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
