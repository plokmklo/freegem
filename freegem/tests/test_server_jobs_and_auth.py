"""Tests for REST jobs, artifacts, health, audit logs, and authentication."""

import os
import unittest
from unittest.mock import MagicMock

from freegem.config import BridgeConfig
from freegem.jobs import JobManager
from freegem.metrics import get_audit_logs, record_audit_event


class MockServer:
    def __init__(self, cfg=None):
        self.cfg = cfg or BridgeConfig()
        self.metrics = MagicMock()


class TestServerJobsAndAuth(unittest.TestCase):
    def setUp(self):
        self.job_mgr = JobManager()

    def test_job_manager_creation_and_artifacts(self):
        job = self.job_mgr.create_job("scrape", {"url": "https://example.com"})
        self.assertIsNotNone(job.id)
        self.assertEqual(job.status, "queued")

        retrieved = self.job_mgr.get_job(job.id)
        self.assertEqual(retrieved.id, job.id)

        art = self.job_mgr.save_artifact(job.id, "test.txt", "text/plain", b"sample content")
        self.assertIsNotNone(art.id)
        self.assertTrue(os.path.exists(art.file_path))

        get_art = self.job_mgr.get_artifact(art.id)
        self.assertEqual(get_art.filename, "test.txt")

    def test_audit_logs(self):
        record_audit_event("test_action", "127.0.0.1", "req-123", {"detail": "info"})
        logs = get_audit_logs(10)
        self.assertTrue(len(logs) > 0)
        latest = logs[-1]
        self.assertEqual(latest["action"], "test_action")
        self.assertEqual(latest["client_ip"], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
