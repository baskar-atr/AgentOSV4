import asyncio
import logging
from typing import Set
from events.types import BaseEvent

logger = logging.getLogger("EventBus")

class EventBus:
    """
    Real-time event bus using asyncio.Queue for decoupling publishers and subscribers.
    Supports dynamic subscription and fan-out distribution.
    """
    def __init__(self):
        self._subscribers: Set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        """
        Subscribes to the event bus. Returns an asyncio.Queue from which the subscriber
        can read incoming events.
        """
        async with self._lock:
            queue = asyncio.Queue()
            self._subscribers.add(queue)
            logger.debug(f"Subscriber registered. Total subscribers: {len(self._subscribers)}")
            return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        """
        Unsubscribes a queue from the event bus.
        """
        async with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)
                logger.debug(f"Subscriber removed. Total subscribers: {len(self._subscribers)}")

    async def publish(self, event: BaseEvent) -> None:
        """
        Publishes an event. Sends it to all subscribed queues.
        """
        async with self._lock:
            if not self._subscribers:
                logger.debug("No active subscribers for event: %s", event.event_type)
                return
            
            # Put event into all subscriber queues concurrently
            for queue in self._subscribers:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning("Subscriber queue is full, dropping event: %s", event.event_type)
                except Exception as e:
                    logger.error(f"Error publishing event to subscriber: {e}")
