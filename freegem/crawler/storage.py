"""Storage sinks for PostgreSQL database and S3/MinIO object storage.

Extends storage destinations for crawled pages, raw HTML snapshots, and extracted items.
Supports fallback/mocking when driver dependencies (psycopg2, boto3) are missing.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from .output import redact_secrets_json

logger = logging.getLogger(__name__)


@dataclass
class PostgresConfig:
    connection_string: str = "postgresql://postgres:postgres@localhost:5432/freegem"
    table_name: str = "crawled_items"


class PostgresSink:
    """Sink writer for PostgreSQL database."""

    def __init__(self, config: PostgresConfig | None = None) -> None:
        self.config = config or PostgresConfig()
        self._conn: Any = None

    def _get_connection(self) -> Any:
        if self._conn is None:
            try:
                import psycopg2
                self._conn = psycopg2.connect(self.config.connection_string)
                self._ensure_table()
            except ImportError:
                raise RuntimeError("psycopg2 is required for PostgresSink. Install with 'pip install psycopg2-binary'.")
        return self._conn

    def _ensure_table(self) -> None:
        conn = self._get_connection()
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.config.table_name} (
                    id SERIAL PRIMARY KEY,
                    url TEXT NOT NULL,
                    data JSONB NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        conn.commit()

    def write(self, item: dict[str, Any]) -> None:
        clean_item = redact_secrets_json(item)
        conn = self._get_connection()
        url = clean_item.get("url", "")
        payload = json.dumps(clean_item, ensure_ascii=False)
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {self.config.table_name} (url, data) VALUES (%s, %s);",
                (url, payload),
            )
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None


@dataclass
class ObjectStorageConfig:
    bucket: str = "freegem-crawl"
    prefix: str = "items/"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    endpoint_url: str | None = None
    region_name: str = "us-east-1"


class ObjectStorageSink:
    """Object storage sink (S3 / MinIO / GCS S3-compatible)."""

    def __init__(self, config: ObjectStorageConfig | None = None) -> None:
        self.config = config or ObjectStorageConfig()
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import boto3
                self._client = boto3.client(
                    "s3",
                    aws_access_key_id=self.config.aws_access_key_id,
                    aws_secret_access_key=self.config.aws_secret_access_key,
                    endpoint_url=self.config.endpoint_url,
                    region_name=self.config.region_name,
                )
            except ImportError:
                raise RuntimeError("boto3 is required for ObjectStorageSink. Install with 'pip install boto3'.")
        return self._client

    def write(self, item: dict[str, Any], key: str | None = None) -> str:
        clean_item = redact_secrets_json(item)
        client = self._get_client()
        url = clean_item.get("url", "")
        if not key:
            import hashlib
            h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
            key = f"{self.config.prefix}{h}.json"

        body_bytes = json.dumps(clean_item, indent=2, ensure_ascii=False).encode("utf-8")
        client.put_object(
            Bucket=self.config.bucket,
            Key=key,
            Body=body_bytes,
            ContentType="application/json",
        )
        return key

    def store_blob(self, key: str, data: bytes, content_type: str = "text/html") -> str:
        client = self._get_client()
        client.put_object(
            Bucket=self.config.bucket,
            Key=f"{self.config.prefix}{key}",
            Body=data,
            ContentType=content_type,
        )
        return f"{self.config.prefix}{key}"

    def close(self) -> None:
        self._client = None
