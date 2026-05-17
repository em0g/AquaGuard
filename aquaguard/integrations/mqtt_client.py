"""Paho-MQTT async wrapper with auto-reconnect and LWT."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

import paho.mqtt.client as mqtt

from aquaguard.config import DeviceConfig, MqttConfig

log = logging.getLogger(__name__)

Callback = Callable[[str, str], Coroutine[Any, Any, None]]


class MqttClient:
    """Async MQTT client wrapping paho-mqtt with reconnect and LWT."""

    def __init__(self, mqtt_config: MqttConfig, device_config: DeviceConfig):
        self._config = mqtt_config
        self._device = device_config
        self._client: mqtt.Client | None = None
        self._connected = asyncio.Event()
        self._subscriptions: dict[str, Callback] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def availability_topic(self) -> str:
        return f"aquaguard/{self._device.id}/availability"

    async def connect(self) -> None:
        """Set up MQTT client and start background network loop."""
        self._loop = asyncio.get_running_loop()
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id=f"aquaguard_{self._device.id}",
            clean_session=True,
        )

        if self._config.username:
            self._client.username_pw_set(
                self._config.username, self._config.password
            )

        # Last Will and Testament
        self._client.will_set(
            self.availability_topic, payload="offline", qos=1, retain=True
        )

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        self._client.connect_async(self._config.host, self._config.port)
        self._client.loop_start()
        log.info("MQTT connecting to %s:%d", self._config.host, self._config.port)

    def _on_connect(
        self, client: mqtt.Client, userdata: Any, flags: dict, rc: int
    ) -> None:
        if rc == 0:
            log.info("MQTT connected")
            # Publish online status
            client.publish(self.availability_topic, "online", qos=1, retain=True)
            # Resubscribe
            for topic in self._subscriptions:
                client.subscribe(topic, qos=1)
                log.debug("MQTT resubscribed to %s", topic)
            if self._loop:
                self._loop.call_soon_threadsafe(self._connected.set)
        else:
            log.error("MQTT connect failed with rc=%d", rc)

    def _on_disconnect(
        self, client: mqtt.Client, userdata: Any, rc: int
    ) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._connected.clear)
        if rc != 0:
            log.warning("MQTT disconnected unexpectedly (rc=%d), will reconnect", rc)

    def _on_message(
        self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage
    ) -> None:
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace")
        cb = self._subscriptions.get(topic)
        if cb and self._loop:
            self._loop.call_soon_threadsafe(
                asyncio.ensure_future, cb(topic, payload)
            )

    def subscribe(self, topic: str, callback: Callback) -> None:
        """Register a subscription with async callback."""
        self._subscriptions[topic] = callback
        if self._client and self._connected.is_set():
            self._client.subscribe(topic, qos=1)

    async def publish(
        self, topic: str, payload: str | dict, retain: bool = False
    ) -> None:
        """Publish a message. Dicts are JSON-encoded."""
        if self._client is None:
            return
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        self._client.publish(topic, payload, qos=1, retain=retain)

    async def run(self) -> None:
        """Keep-alive loop — just waits, paho handles reconnect internally."""
        while True:
            await asyncio.sleep(60)

    async def disconnect(self) -> None:
        if self._client:
            self._client.publish(
                self.availability_topic, "offline", qos=1, retain=True
            )
            self._client.loop_stop()
            self._client.disconnect()
