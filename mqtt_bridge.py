"""MQTT 订阅桥：line1/events 上的事件对象 → 管线。

没有 broker 时不阻塞服务启动：状态显示 disconnected，并保留 HTTP 注入通道。
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

try:  # paho 是可选依赖：没装也能跑 HTTP 注入模式
    import paho.mqtt.client as mqtt

    HAVE_PAHO = True
except ImportError:  # pragma: no cover
    mqtt = None  # type: ignore[assignment]
    HAVE_PAHO = False


class MqttBridge:
    def __init__(
        self,
        host: str,
        port: int,
        topic: str,
        on_event: Callable[[dict[str, Any]], None],
    ) -> None:
        self.host = host
        self.port = port
        self.topic = topic
        self.on_event = on_event
        self.connected = False
        self.last_error: str | None = None
        self.last_message_at: str | None = None
        self.received = 0
        self._client: Any = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if not HAVE_PAHO:
            self.last_error = "paho-mqtt 未安装（pip install paho-mqtt）"
            return
        self._thread = threading.Thread(target=self._run, name="mqtt-bridge", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="safe-monitor-dashboard",
            clean_session=True,
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=10)
        self._client = client
        try:
            client.connect_async(self.host, self.port, keepalive=30)
            client.loop_start()
        except Exception as exc:  # noqa: BLE001 - 连不上不该拖垮服务
            self.last_error = str(exc)

    # ------------------------------------------------------------------ #
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):  # noqa: ANN001
        ok = int(getattr(reason_code, "value", 1) or 0) == 0
        self.connected = ok
        self.last_error = None if ok else str(reason_code)
        if ok:
            client.subscribe(self.topic, qos=0)

    def _on_disconnect(self, client, userdata, flags, reason_code=None, properties=None):  # noqa: ANN001
        self.connected = False

    def _on_message(self, client, userdata, msg):  # noqa: ANN001
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.last_error = f"非法事件 JSON: {exc}"
            return
        if not isinstance(payload, dict) or "event_id" not in payload:
            self.last_error = "事件缺少 event_id"
            return
        self.received += 1
        self.last_message_at = payload.get("triggered_at")
        try:
            self.on_event(payload)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"管线异常: {exc}"

    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "available": HAVE_PAHO,
            "broker": f"{self.host}:{self.port}",
            "topic": self.topic,
            "received": self.received,
            "last_error": self.last_error,
        }

    def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
