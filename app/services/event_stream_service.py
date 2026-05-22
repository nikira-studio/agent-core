import json
import queue
import threading
import uuid
from datetime import datetime, timezone


class _EventHub:
    MAX_QUEUE_SIZE = 100

    def __init__(self):
        self._lock = threading.Lock()
        self._queues: dict[str, queue.Queue] = {}

    def register(self) -> tuple[str, queue.Queue]:
        client_id = str(uuid.uuid4())
        q: queue.Queue = queue.Queue(maxsize=self.MAX_QUEUE_SIZE)
        with self._lock:
            self._queues[client_id] = q
        return client_id, q

    def unregister(self, client_id: str) -> None:
        with self._lock:
            self._queues.pop(client_id, None)

    def publish(self, event_type: str, data: dict) -> None:
        payload = {
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        with self._lock:
            queues = list(self._queues.values())
        for q in queues:
            if q.full():
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._queues)


event_hub = _EventHub()
