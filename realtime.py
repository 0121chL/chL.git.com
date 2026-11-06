"""SSE 事件总线：线程安全的发布/订阅（Agent 在线程池里跑，订阅者在事件循环里）。"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            self._subs.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(queue)

    def publish(self, message: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._lock:
            subscribers = list(self._subs)
        for queue in subscribers:
            loop.call_soon_threadsafe(self._put, queue, message)

    @staticmethod
    def _put(queue: asyncio.Queue, message: dict[str, Any]) -> None:
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:  # 慢客户端直接丢帧，不阻塞管线
            pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


def sse_frame(message: dict[str, Any]) -> str:
    """SSE 帧：event 名用消息 type，data 是单行 JSON。"""
    kind = message.get("type", "message")
    body = json.dumps(message, ensure_ascii=False)
    return f"event: {kind}\ndata: {body}\n\n"
