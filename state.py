"""Durable per-provider state so expiry survives restarts."""

import json
import logging
import os
import threading
from datetime import datetime, timezone

import config

_LOGGER = logging.getLogger(__name__)
_LOCK = threading.Lock()


def _path() -> str:
    return os.path.join(config.DATA_DIR, "state.json")


def load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _LOGGER.warning("Could not read state (%s); starting fresh", exc)
        return {}


def save(state: dict) -> None:
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        # Losing state only costs us an extra renewal, so never fail the run.
        _LOGGER.warning("Could not persist state: %s", exc)


def record(provider_id: str, result: dict) -> dict:
    """Merge one result into stored state and return the updated entry."""
    with _LOCK:
        state = load()
        entry = state.get(provider_id, {})
        entry["name"] = result.get("name", entry.get("name"))
        entry["mode"] = result.get("mode", entry.get("mode"))
        entry["last_attempt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry["ok"] = result.get("ok", False)
        entry["message"] = result.get("message")
        entry["status"] = result.get(
            "status", "ok" if result.get("ok") else "failed"
        )

        if result.get("ok"):
            if result.get("renewed_at"):
                entry["last_success"] = result["renewed_at"]
            # A provider that only confirmed an existing pass has no new expiry
            # to report; keep the one we already know about.
            if not result.get("keep_expires"):
                entry["expires_at"] = result.get("expires_at")
            entry["url"] = result.get("url")
            entry["consecutive_failures"] = 0
        else:
            entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
        state[provider_id] = entry
        save(state)
        return entry


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# Backoff between retries after consecutive failures, in minutes. A provider
# that keeps failing settles at one attempt per day rather than hammering the
# publisher on every tick. Without this a permanently failing provider issued
# a request every CHECK_MINUTES forever.
_FAILURE_BACKOFF_MINUTES = (5, 15, 60, 240, 720, 1440)


def failure_backoff_seconds(failures: int) -> int:
    if failures <= 0:
        return 0
    index = min(failures, len(_FAILURE_BACKOFF_MINUTES)) - 1
    return _FAILURE_BACKOFF_MINUTES[index] * 60


def is_due(provider, entry: dict | None) -> bool:
    """Whether a provider should run now.

    Renews on ``renew_every_hours`` since the last success, and always renews
    once access is inside ``RENEW_MARGIN_HOURS`` of lapsing. A provider that
    has never run is always due.

    A provider whose last attempt *failed* waits out an increasing backoff
    first, so a publisher that is refusing us is asked once a day rather than
    on every tick.
    """
    if not entry:
        return True

    now = datetime.now(timezone.utc)

    if not entry.get("ok"):
        last_attempt = parse_ts(entry.get("last_attempt"))
        if last_attempt is None:
            return True
        wait = failure_backoff_seconds(int(entry.get("consecutive_failures", 0)))
        return (now - last_attempt).total_seconds() >= wait

    last = parse_ts(entry.get("last_success"))
    if last is None:
        return True

    if (now - last).total_seconds() >= provider.renew_every_hours * 3600:
        return True

    expires = parse_ts(entry.get("expires_at"))
    if expires is not None:
        margin = config.RENEW_MARGIN_HOURS * 3600
        if (expires - now).total_seconds() <= margin:
            return True

    return False
