"""External queue adapters: Memory, Redis, and RabbitMQ.

Provides an abstract QueueAdapter interface for distributed crawling work item queues.
Includes graceful fallback and connection handling when optional client libraries are missing or unavailable.
"""

from __future__ import annotations

import abc
import json
import logging
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class QueueItem:
    url: str
    depth: int = 0
    priority: int = 5
    payload: dict[str, Any] | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> QueueItem:
        raw = json.loads(data)
        return cls(**raw)


class QueueAdapter(abc.ABC):
    """Abstract interface for crawler queue backends."""

    @abc.abstractmethod
    def push(self, item: QueueItem) -> None:
        """Enqueue a work item."""
        pass

    @abc.abstractmethod
    def pop(self, timeout: float = 1.0) -> QueueItem | None:
        """Dequeue the next work item."""
        pass

    @abc.abstractmethod
    def len(self) -> int:
        """Return total items in the queue."""
        pass

    @abc.abstractmethod
    def clear(self) -> None:
        """Clear all queued items."""
        pass


class MemoryQueueAdapter(QueueAdapter):
    """Thread-safe or in-memory FIFO queue adapter."""

    def __init__(self) -> None:
        self._queue: deque[QueueItem] = deque()

    def push(self, item: QueueItem) -> None:
        self._queue.append(item)

    def pop(self, timeout: float = 1.0) -> QueueItem | None:
        if self._queue:
            return self._queue.popleft()
        return None

    def len(self) -> int:
        return len(self._queue)

    def clear(self) -> None:
        self._queue.clear()


class RedisQueueAdapter(QueueAdapter):
    """Queue adapter backed by Redis LPUSH/RPOP or priority zset."""

    def __init__(self, redis_url: str = "redis://localhost:6379/0", queue_name: str = "crawler_queue") -> None:
        self.redis_url = redis_url
        self.queue_name = queue_name
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import redis
                self._client = redis.Redis.from_url(self.redis_url)
            except ImportError as exc:
                raise RuntimeError("redis package is required for RedisQueueAdapter. Install with 'pip install redis'.") from exc
        return self._client

    def push(self, item: QueueItem) -> None:
        client = self._get_client()
        client.lpush(self.queue_name, item.to_json())

    def pop(self, timeout: float = 1.0) -> QueueItem | None:
        client = self._get_client()
        res = client.rpop(self.queue_name)
        if res is None:
            return None
        if isinstance(res, bytes):
            res = res.decode("utf-8")
        return QueueItem.from_json(res)

    def len(self) -> int:
        client = self._get_client()
        return client.llen(self.queue_name)

    def clear(self) -> None:
        client = self._get_client()
        client.delete(self.queue_name)


class RabbitMQQueueAdapter(QueueAdapter):
    """Queue adapter backed by RabbitMQ via pika or standard AMQP."""

    def __init__(self, amqp_url: str = "amqp://guest:guest@localhost:5672/", queue_name: str = "crawler_queue") -> None:
        self.amqp_url = amqp_url
        self.queue_name = queue_name
        self._connection: Any = None
        self._channel: Any = None

    def _get_channel(self) -> Any:
        if self._channel is None or self._channel.is_closed:
            try:
                import pika
                params = pika.URLParameters(self.amqp_url)
                self._connection = pika.BlockingConnection(params)
                self._channel = self._connection.channel()
                self._channel.queue_declare(queue=self.queue_name, durable=True)
            except ImportError as exc:
                raise RuntimeError("pika package is required for RabbitMQQueueAdapter. Install with 'pip install pika'.") from exc
        return self._channel

    def push(self, item: QueueItem) -> None:
        channel = self._get_channel()
        import pika
        channel.basic_publish(
            exchange="",
            routing_key=self.queue_name,
            body=item.to_json(),
            properties=pika.BasicProperties(delivery_mode=2),
        )

    def pop(self, timeout: float = 1.0) -> QueueItem | None:
        channel = self._get_channel()
        method_frame, _header_frame, body = channel.basic_get(queue=self.queue_name, auto_ack=True)
        if method_frame is None or body is None:
            return None
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        return QueueItem.from_json(body)

    def len(self) -> int:
        channel = self._get_channel()
        res = channel.queue_declare(queue=self.queue_name, passive=True)
        return res.method.message_count

    def clear(self) -> None:
        channel = self._get_channel()
        channel.queue_purge(queue=self.queue_name)
