"""Search index sinks: Elasticsearch, OpenSearch, and Meilisearch.

Exports crawler output records directly to full-text search engines.
Includes standard HTTP/client adapters with fallback error handling.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from typing import Any

from .output import redact_secrets_json

logger = logging.getLogger(__name__)


@dataclass
class SearchIndexConfig:
    node_url: str = "http://localhost:9200"
    index_name: str = "freegem_docs"
    api_key: str | None = None
    engine_type: str = "elasticsearch"  # "elasticsearch", "opensearch", "meilisearch"


class SearchIndexSink:
    """Sink adapter for full-text search engines."""

    def __init__(self, config: SearchIndexConfig | None = None) -> None:
        self.config = config or SearchIndexConfig()

    def index_document(self, doc_id: str, document: dict[str, Any]) -> bool:
        """Index a document into search index."""
        clean_doc = redact_secrets_json(document)

        if self.config.engine_type in ("elasticsearch", "opensearch"):
            url = f"{self.config.node_url.rstrip('/')}/{self.config.index_name}/_doc/{doc_id}"
            headers = {"Content-Type": "application/json"}
            if self.config.api_key:
                headers["Authorization"] = f"ApiKey {self.config.api_key}"

            data = json.dumps(clean_doc, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="PUT")

            try:
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    return resp.status in (200, 201)
            except Exception as exc:
                logger.warning("Failed to index document %s to %s: %s", doc_id, self.config.engine_type, exc)
                return False

        elif self.config.engine_type == "meilisearch":
            url = f"{self.config.node_url.rstrip('/')}/indexes/{self.config.index_name}/documents"
            headers = {"Content-Type": "application/json"}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"

            clean_doc["id"] = doc_id
            data = json.dumps([clean_doc], ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")

            try:
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    return resp.status in (200, 202)
            except Exception as exc:
                logger.warning("Failed to index document %s to meilisearch: %s", doc_id, exc)
                return False

        else:
            raise ValueError(f"Unsupported search engine type: {self.config.engine_type}")

    def close(self) -> None:
        pass
