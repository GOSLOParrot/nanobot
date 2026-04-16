"""ParrotCarriers Bus channel — bridges nanobot with the Parrot Bus dispatch stream.

Inbound: Redis Stream (parrot.nanobot.dispatch) → nanobot agent
Outbound: nanobot agent → Redis Pub/Sub (parrot.nanobot.results)

This adapter lets nanobot act as a background task worker for ParrotCarriers.
Tasks dispatched by the Brain Agent flow through the Scheduler, land in a Redis
Stream, and this channel feeds them into nanobot's agent loop. The agent's
response is published back so the Scheduler/Brain can pick it up.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base

# Redis is an optional dep — only needed when this channel is enabled.
try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None  # type: ignore[assignment]

# Default Redis / Stream / Channel constants (same as ParrotCarriers shared/constants.py)
_DEFAULT_STREAM = "parrot.nanobot.dispatch"
_DEFAULT_RESULTS_CH = "parrot.nanobot.results"
_DEFAULT_GROUP = "nanobot-workers"
_DEFAULT_CONSUMER = "worker-0"


class ParrotBusConfig(Base):
    """Configuration for the ParrotCarriers Bus channel."""

    enabled: bool = False
    redis_url: str = Field(
        default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0")
    )
    stream: str = _DEFAULT_STREAM
    results_channel: str = _DEFAULT_RESULTS_CH
    consumer_group: str = _DEFAULT_GROUP
    consumer_name: str = Field(
        default_factory=lambda: os.getenv("PARROT_WORKER_NAME", _DEFAULT_CONSUMER)
    )
    allow_from: list[str] = Field(default_factory=lambda: ["*"])


class ParrotBusChannel(BaseChannel):
    """Channel that consumes tasks from ParrotCarriers Redis dispatch stream."""

    name = "parrot_bus"
    display_name = "ParrotCarriers Bus"

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = ParrotBusConfig.model_validate(config)
        super().__init__(config, bus)
        self._redis: aioredis.Redis | None = None
        self._task_meta: dict[str, dict] = {}

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return ParrotBusConfig().model_dump(by_alias=True)

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            if aioredis is None:
                raise RuntimeError(
                    "parrot_bus channel requires 'redis' package. "
                    "Install with: pip install redis>=5.0"
                )
            self._redis = aioredis.from_url(
                self.config.redis_url, decode_responses=True
            )
        return self._redis

    async def _ensure_consumer_group(self) -> None:
        r = await self._get_redis()
        try:
            await r.xgroup_create(
                self.config.stream, self.config.consumer_group,
                id="0", mkstream=True,
            )
            logger.info("parrot_bus: consumer group '{}' created", self.config.consumer_group)
        except Exception:
            logger.debug("parrot_bus: consumer group already exists")

    # ── Inbound: Redis Stream → nanobot agent ──────────────────────────

    async def start(self) -> None:
        """Connect to Redis and consume the dispatch stream.

        Blocks until stop() is called, per BaseChannel contract.
        """
        self._running = True
        await self._ensure_consumer_group()
        logger.info("parrot_bus channel started (stream={})", self.config.stream)
        await self._consume_loop()

    async def _consume_loop(self) -> None:
        r = await self._get_redis()
        retry_delay = 2
        max_retry_delay = 30
        while self._running:
            try:
                entries = await r.xreadgroup(
                    self.config.consumer_group,
                    self.config.consumer_name,
                    {self.config.stream: ">"},
                    count=1,
                    block=5000,
                )
                retry_delay = 2

                if not entries:
                    continue

                for _stream_name, messages in entries:
                    for msg_id, fields in messages:
                        await self._ingest_task(r, msg_id, fields)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("parrot_bus: error in consume loop, retrying in {}s", retry_delay)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)

    async def _ingest_task(self, r, msg_id: str, fields: dict) -> None:
        """Convert a Redis Stream entry into an InboundMessage for the agent."""
        raw = fields.get("payload", "{}")
        task = json.loads(raw)
        task_id = task.get("task_id", msg_id)
        task_type = task.get("type", "unknown")
        params = task.get("params", {})

        self._task_meta[task_id] = {
            "task_id": task_id,
            "task_type": task_type,
            "msg_id": msg_id,
        }

        content = self._build_prompt(task_type, params)

        logger.info("parrot_bus: ingesting task {} (type={})", task_id, task_type)
        await self._handle_message(
            sender_id="brain-agent",
            chat_id=task_id,
            content=content,
            metadata={
                "task_id": task_id,
                "task_type": task_type,
                "priority": task.get("priority", "normal"),
            },
        )

        await r.xack(self.config.stream, self.config.consumer_group, msg_id)

    @staticmethod
    def _build_prompt(task_type: str, params: dict) -> str:
        """Turn a structured task into a natural-language prompt for the agent."""
        if task_type == "research" and "query" in params:
            return f"Research the following topic and provide a concise answer: {params['query']}"
        if task_type == "summarize" and "text" in params:
            return f"Summarize the following text:\n\n{params['text']}"
        if task_type == "remind":
            return f"Create a reminder: {params.get('message', json.dumps(params))}"
        return f"Execute task '{task_type}' with parameters: {json.dumps(params)}"

    # ── Outbound: nanobot agent → Redis Pub/Sub ────────────────────────

    async def send(self, msg: OutboundMessage) -> None:
        """Publish the agent's response as a task result to Redis."""
        r = await self._get_redis()
        task_id = msg.chat_id
        meta = self._task_meta.pop(task_id, {})

        result = {
            "task_id": task_id,
            "type": meta.get("task_type", "unknown"),
            "status": "completed",
            "result": msg.content,
            "completed_at": time.time(),
        }

        await r.publish(self.config.results_channel, json.dumps(result))
        logger.info("parrot_bus: result published for task {}", task_id)

        await self._archive_to_graphiti(msg.content, group_id="maid")

    async def _archive_to_graphiti(self, text: str, group_id: str) -> None:
        """Best-effort write to Graphiti if parrotcarriers memory module is available."""
        try:
            from parrot.memory.conversation_writer import write_nanobot_turn
            await write_nanobot_turn("assistant", text, group_id=group_id, source="maid_bus")
        except ImportError:
            pass
        except Exception:
            logger.debug("parrot_bus: graphiti archive skipped (not critical)")

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def stop(self) -> None:
        self._running = False
        if self._redis:
            await self._redis.aclose()
            self._redis = None
        logger.info("parrot_bus channel stopped")
