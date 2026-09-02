"""Optional outbound alerts: ntfy on failure, Uptime Kuma push on success.

Both are best-effort. A notification failure is logged and swallowed so it can
never turn a successful renewal into a failed run.
"""

import logging

import requests

import config

_LOGGER = logging.getLogger(__name__)


def ntfy_enabled() -> bool:
    return bool(config.NTFY_URL and config.NTFY_TOPIC)


def notify_failures(failures: list[dict]) -> None:
    """Send one ntfy alert summarising failed providers."""
    if not failures or not ntfy_enabled():
        return

    if len(failures) == 1:
        title = f"{failures[0]['name']} access renewal failed"
    else:
        title = f"{len(failures)} newspaper renewals failed"

    body = "\n".join(f"{f['name']}: {f['message']}" for f in failures)

    headers = {
        "Title": title,
        "Tags": "newspaper,warning",
        "Priority": "4",
    }
    auth = None
    if config.NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {config.NTFY_TOKEN}"
    elif config.NTFY_USERNAME:
        auth = (config.NTFY_USERNAME, config.NTFY_PASSWORD)

    url = f"{config.NTFY_URL}/{config.NTFY_TOPIC}"
    try:
        response = requests.post(
            url,
            data=body.encode("utf-8"),
            headers=headers,
            auth=auth,
            timeout=15,
        )
        response.raise_for_status()
        _LOGGER.info("Sent ntfy alert to %s", url)
    except requests.RequestException as exc:
        _LOGGER.warning("ntfy alert failed: %s", exc)


def heartbeat(ok: bool, message: str) -> None:
    """Ping an Uptime Kuma push monitor. Only pings when everything succeeded."""
    if not config.KUMA_PUSH_URL or not ok:
        return
    try:
        requests.get(
            config.KUMA_PUSH_URL,
            params={"status": "up", "msg": message[:180], "ping": ""},
            timeout=15,
        ).raise_for_status()
        _LOGGER.debug("Sent Uptime Kuma heartbeat")
    except requests.RequestException as exc:
        _LOGGER.warning("Uptime Kuma heartbeat failed: %s", exc)
