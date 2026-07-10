"""
Kafka producer and consumer wrappers for the Engineering Intelligence Platform.
Both classes gracefully degrade when Kafka is unavailable (useful for local dev).
"""
import json
import logging
import os
import asyncio
from typing import Callable, List

from aiokafka import AIOKafkaProducer, AIOKafkaConsumer
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class EventPublisher:
    """Async Kafka producer. Falls back to no-op logging when Kafka is unreachable."""

    def __init__(self) -> None:
        self.bootstrap_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
        self._producer: AIOKafkaProducer | None = None

    async def start(self) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
        )
        try:
            await self._producer.start()
            logger.info("Kafka producer connected to %s", self.bootstrap_servers)
        except Exception as exc:
            logger.warning(
                "Kafka unavailable (%s). Events will be logged locally.", exc
            )
            try:
                await self._producer.stop()
            except Exception:
                pass
            self._producer = None

    async def stop(self) -> None:
        if self._producer:
            await self._producer.stop()
            logger.info("Kafka producer stopped.")

    async def publish(self, topic: str, event: BaseModel) -> None:
        payload = json.loads(event.model_dump_json(by_alias=True))
        if self._producer is None:
            logger.info("[NO-KAFKA] topic=%s event_type=%s", topic, payload.get("eventType"))
            return
        await self._producer.send_and_wait(topic, value=payload)
        logger.info("Published %s → %s", payload.get("eventType"), topic)


class EventSubscriber:
    """Async Kafka consumer. Falls back to no-op when Kafka is unreachable."""

    def __init__(self, group_id: str, topics: List[str]) -> None:
        self.bootstrap_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
        self.group_id = group_id
        self.topics = topics
        self._consumer: AIOKafkaConsumer | None = None
        self._task: asyncio.Task | None = None

    async def start(self, handler: Callable) -> None:
        self._consumer = AIOKafkaConsumer(
            *self.topics,
            bootstrap_servers=self.bootstrap_servers,
            group_id=self.group_id,
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            auto_offset_reset="earliest",
        )
        try:
            await self._consumer.start()
            self._task = asyncio.create_task(self._consume(handler))
            logger.info(
                "[%s] Subscribed to topics: %s", self.group_id, self.topics
            )
        except Exception as exc:
            logger.warning(
                "[%s] Kafka unavailable (%s). Consumer not started.", self.group_id, exc
            )
            try:
                await self._consumer.stop()
            except Exception:
                pass
            self._consumer = None

    async def _consume(self, handler: Callable) -> None:
        try:
            async for msg in self._consumer:
                try:
                    await handler(msg.topic, msg.value)
                except Exception as exc:
                    logger.error("Error in event handler: %s", exc, exc_info=True)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Consumer loop error: %s", exc, exc_info=True)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._consumer:
            await self._consumer.stop()
            logger.info("[%s] Consumer stopped.", self.group_id)
