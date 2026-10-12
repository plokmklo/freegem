"""Tests for MCP job submission, retrieval, and session profile management tools."""

import json
import unittest

from freegem.config import BridgeConfig
from freegem.mcp_tools import run_tool


class TestMcpJobs(unittest.TestCase):
    def test_mcp_submit_and_get_job(self) -> None:
        cfg = BridgeConfig()
        submit_res = run_tool(
            cfg,
            "freegem_submit_job",
            {"job_type": "scrape", "params": {"url": "https://example.com"}},
        )
        job_data = json.loads(submit_res)
        self.assertIn("id", job_data)
        self.assertEqual(job_data["type"], "scrape")

        job_id = job_data["id"]
        get_res = run_tool(cfg, "freegem_get_job", {"job_id": job_id})
        get_data = json.loads(get_res)
        self.assertEqual(get_data["id"], job_id)

    def test_mcp_auth_refusal(self) -> None:
        cfg = BridgeConfig.replace(BridgeConfig(), api_key="secret-key")
        with self.assertRaises(PermissionError):
            run_tool(cfg, "freegem_submit_job", {"job_type": "scrape", "params": {}})

        # Success with matching api_key
        submit_res = run_tool(
            cfg,
            "freegem_submit_job",
            {
                "job_type": "scrape",
                "params": {"url": "https://example.com"},
                "api_key": "secret-key",
            },
        )
        job_data = json.loads(submit_res)
        self.assertIn("id", job_data)

    def test_mcp_profile_management(self) -> None:
        cfg = BridgeConfig()
        list_res = run_tool(cfg, "freegem_manage_profile", {"action": "list"})
        profiles = json.loads(list_res)
        self.assertIsInstance(profiles, list)


if __name__ == "__main__":
    unittest.main()
