import json
import os
import asyncio
from aiokafka import AIOKafkaProducer, AIOKafkaConsumer
from pydantic import BaseModel

class EventPublisher:
    def __init__(self):
        self.bootstrap_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
        self.producer = None

    async def start(self):
        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            value_serializer=lambda v: json.dumps(v).encode('utf-8')
        )
        try:
            await self.producer.start()
        except Exception as e:
            print(f"Warning: Failed to start Kafka producer. Is Kafka running? Error: {e}")
            self.producer = None

    async def stop(self):
        if self.producer:
            await self.producer.stop()

    async def publish(self, topic: str, event: BaseModel):
        if not self.producer:
            print(f"Mock publish to {topic}: {event.model_dump_json(by_alias=True)}")
            return
        
        event_dict = json.loads(event.model_dump_json(by_alias=True))
        await self.producer.send_and_wait(topic, value=event_dict)

class EventSubscriber:
    def __init__(self, group_id: str, topics: list):
        self.bootstrap_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
        self.group_id = group_id
        self.topics = topics
        self.consumer = None
        self._running = False
        self._task = None
        self._handler = None

    async def start(self, handler):
        self.consumer = AIOKafkaConsumer(
            *self.topics,
            bootstrap_servers=self.bootstrap_servers,
            group_id=self.group_id,
            value_deserializer=lambda m: json.loads(m.decode('utf-8')),
            auto_offset_reset='earliest'
        )
        self._handler = handler
        try:
            await self.consumer.start()
            self._running = True
            self._task = asyncio.create_task(self._consume_loop())
            print(f"[{self.group_id}] Started listening to {self.topics}")
        except Exception as e:
            print(f"[{self.group_id}] Warning: Failed to start Kafka consumer. Is Kafka running? Error: {e}")
            self.consumer = None

    async def _consume_loop(self):
        try:
            async for msg in self.consumer:
                try:
                    await self._handler(msg.topic, msg.value)
                except Exception as e:
                    print(f"Error handling message: {e}")
        except asyncio.CancelledError:
            pass

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        if self.consumer:
            await self.consumer.stop()
