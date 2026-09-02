"""MQTT publisher with Home Assistant MQTT Discovery.

One HA device ("Library News Access") with, per newspaper:
  - <name> access status   (sensor: ok / failed, plus attributes)
  - <name> access expires  (sensor, timestamp)   -- renewable providers only
  - <name> access active   (binary_sensor)       -- renewable providers only

Publishing is best-effort: a broker outage must never stop a renewal.
"""

import json
import logging
import threading
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

import config
import state

_LOGGER = logging.getLogger(__name__)

_DEVICE = {
    "identifiers": ["library_news_access"],
    "name": "Library News Access",
    "manufacturer": "Memorial Hall Library",
    "model": "Newspaper pass renewal",
}


def enabled() -> bool:
    return bool(config.MQTT_HOST)


def _availability_topic() -> str:
    return f"{config.MQTT_BASE_TOPIC}/status"


def _connect() -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=config.MQTT_CLIENT_ID,
    )
    if config.MQTT_USERNAME:
        client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)

    client.will_set(_availability_topic(), "offline", qos=1, retain=True)

    connected = threading.Event()
    result = {"rc": None}

    def _on_connect(_client, _userdata, _flags, reason_code, _props=None):
        result["rc"] = reason_code
        connected.set()

    client.on_connect = _on_connect
    client.connect(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
    client.loop_start()

    if not connected.wait(timeout=15):
        client.loop_stop()
        client.disconnect()
        raise ConnectionError(
            f"MQTT connection to {config.MQTT_HOST}:{config.MQTT_PORT} timed out"
        )

    rc = result["rc"]
    if getattr(rc, "is_failure", False) or (isinstance(rc, int) and rc != 0):
        client.loop_stop()
        client.disconnect()
        raise ConnectionError(
            f"MQTT broker {config.MQTT_HOST}:{config.MQTT_PORT} rejected connection: {rc}"
        )
    return client


def _entities(provider, entry: dict) -> list[tuple[str, str, dict, str]]:
    """(component, object_id, discovery_config, payload) for one provider."""
    base = f"{config.MQTT_BASE_TOPIC}/{provider.id}"
    avail = _availability_topic()
    out: list[tuple[str, str, dict, str]] = []

    expires = state.parse_ts(entry.get("expires_at"))
    now = datetime.now(timezone.utc)

    attributes = {
        "message": entry.get("message"),
        "mode": provider.mode,
        "last_attempt": entry.get("last_attempt"),
        "last_success": entry.get("last_success"),
        "consecutive_failures": entry.get("consecutive_failures", 0),
        "url": entry.get("url"),
        "notes": provider.notes,
    }

    status_id = f"lna_{provider.id}_status"
    out.append((
        "sensor",
        status_id,
        {
            "name": f"{provider.name} access status",
            "unique_id": status_id,
            "icon": "mdi:newspaper-variant-outline",
            "state_topic": f"{base}/status/state",
            "json_attributes_topic": f"{base}/status/attributes",
            "availability_topic": avail,
            "device": _DEVICE,
        },
        "ok" if entry.get("ok") else "failed",
    ))
    out.append((
        "__attributes__",
        f"{base}/status/attributes",
        {},
        json.dumps(attributes),
    ))

    if provider.access_hours:
        expires_id = f"lna_{provider.id}_expires"
        out.append((
            "sensor",
            expires_id,
            {
                "name": f"{provider.name} access expires",
                "unique_id": expires_id,
                "device_class": "timestamp",
                "icon": "mdi:clock-outline",
                "state_topic": f"{base}/expires/state",
                "availability_topic": avail,
                "device": _DEVICE,
            },
            expires.isoformat(timespec="seconds") if expires else "",
        ))

        active_id = f"lna_{provider.id}_active"
        out.append((
            "binary_sensor",
            active_id,
            {
                "name": f"{provider.name} access active",
                "unique_id": active_id,
                "device_class": "connectivity",
                "state_topic": f"{base}/active/state",
                "availability_topic": avail,
                "device": _DEVICE,
            },
            "ON" if (expires and expires > now) else "OFF",
        ))

    return out


def publish(providers: list, stored: dict) -> None:
    """Publish discovery + current state for every provider."""
    if not enabled():
        _LOGGER.debug("MQTT_HOST not set; skipping publish")
        return

    client = _connect()
    try:
        client.publish(_availability_topic(), "online", qos=1, retain=True)

        for provider in providers:
            entry = stored.get(provider.id) or {}
            for component, key, disc, payload in _entities(provider, entry):
                if component == "__attributes__":
                    client.publish(key, payload, qos=1, retain=True)
                    continue

                disc_topic = f"{config.HA_DISCOVERY_PREFIX}/{component}/{key}/config"
                client.publish(disc_topic, json.dumps(disc), qos=1, retain=True)
                if payload != "":
                    client.publish(disc["state_topic"], payload, qos=1, retain=True)

        _LOGGER.info("Published %d provider(s) to MQTT", len(providers))
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass
