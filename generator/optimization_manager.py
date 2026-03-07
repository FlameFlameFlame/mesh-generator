import queue
import threading
from typing import Iterator


class OptimizationJobManager:
    """Manage a single in-process optimization job and its SSE event stream."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._running = False
        self._cancel_requested = False
        self._result: dict = {}

    @property
    def queue(self) -> queue.Queue:
        return self._queue

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested

    def get_result(self) -> dict:
        with self._lock:
            return dict(self._result)

    def prepare_new_job(self) -> None:
        with self._lock:
            self._result = {}
            self._cancel_requested = False
        self.drain_queue()

    def mark_running(self) -> None:
        with self._lock:
            self._running = True
            self._cancel_requested = False

    def mark_finished(self) -> None:
        with self._lock:
            self._running = False

    def request_cancel(self) -> bool:
        with self._lock:
            if not self._running:
                return False
            self._cancel_requested = True
            return True

    def set_result(self, result: dict) -> None:
        with self._lock:
            self._result = dict(result or {})

    def drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def put(self, item) -> None:
        self._queue.put(item)

    def iter_sse_events(self, keepalive_timeout_s: float = 30.0) -> Iterator[str]:
        while True:
            try:
                item = self._queue.get(timeout=keepalive_timeout_s)
            except queue.Empty:
                yield "data: {}\n\n"
                continue
            if isinstance(item, dict):
                import json

                yield f"data: {json.dumps(item)}\n\n"
                if item.get("done") or item.get("error") or item.get("canceled"):
                    break
            else:
                import json

                yield f"data: {json.dumps({'log': item})}\n\n"
