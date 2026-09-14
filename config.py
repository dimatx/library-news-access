"""Configuration loaded from environment variables.

Nothing library- or newspaper-specific is hardcoded here: the list of
newspapers lives in ``providers.json`` (overridable at runtime), and the
per-provider credentials fall back to a single shared account.
"""

import os


def _clean(value: str) -> str:
    return (value or "").strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_clean(os.environ.get(name, "")) or default)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = _clean(os.environ.get(name, "")).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# --- Library card ----------------------------------------------------------
# The card number is the only thing the library itself authenticates on.
CARD_NUMBER = _clean(os.environ.get("LNA_CARD_NUMBER", ""))

# Base URL of the library's "Connect to..." connector. Each newspaper is a
# numeric db_id under this path (e.g. https://mhl.org/connect/37577).
CONNECT_BASE = _clean(
    os.environ.get("LNA_CONNECT_BASE", "https://mhl.org/connect")
).rstrip("/")

# --- Newspaper account identity -------------------------------------------
# Used to (re)submit the publisher's registration form. Providers may override
# any of these with LNA_<PROVIDER_ID>_<FIELD>, e.g. LNA_BOSTON_GLOBE_EMAIL.
FIRST_NAME = _clean(os.environ.get("LNA_FIRST_NAME", ""))
LAST_NAME = _clean(os.environ.get("LNA_LAST_NAME", ""))
EMAIL = _clean(os.environ.get("LNA_EMAIL", ""))
PASSWORD = os.environ.get("LNA_PASSWORD", "")  # never strip a password

# --- Scheduling ------------------------------------------------------------
# How often the scheduler wakes up to see if anything is due. Each provider
# declares its own renew_every_hours; this is just the tick.
CHECK_MINUTES = _int("LNA_CHECK_MINUTES", 30)

# Renew this many hours before access actually lapses, so a transient failure
# still leaves room for several retries before the user notices.
RENEW_MARGIN_HOURS = _int("LNA_RENEW_MARGIN_HOURS", 24)

# Run a full renewal pass as soon as the container starts.
RUN_ON_START = _bool("LNA_RUN_ON_START", True)

REQUEST_TIMEOUT = _int("LNA_REQUEST_TIMEOUT", 45)

# Consecutive failures before /health reports unhealthy. One failed poll is a
# blip -- the library answered with a Cloudflare 522 once and the next tick was
# fine -- and reporting that as unhealthy pages someone for a third-party
# hiccup that needs no action. Three in a row spans an hour of backoff.
HEALTH_FAILURE_THRESHOLD = max(1, _int("LNA_HEALTH_FAILURE_THRESHOLD", 3))
USER_AGENT = _clean(
    os.environ.get(
        "LNA_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    )
)

# --- Providers -------------------------------------------------------------
# Path to the declarative newspaper list. Mount your own to add newspapers
# without rebuilding the image.
PROVIDERS_FILE = _clean(os.environ.get("LNA_PROVIDERS_FILE", "")) or None

# Netscape cookies.txt exported from a logged-in NYT browser session. NYT's
# login page is behind bot protection, so the session is captured once by hand
# rather than scripted. Lives in the data volume, not the image.
NYT_COOKIE_FILE = _clean(
    os.environ.get("LNA_NYT_COOKIE_FILE", "")
) or os.path.join(_clean(os.environ.get("LNA_DATA_DIR", "/data")), "nyt_cookies.txt")

# Comma-separated provider ids. When set, only these run (overrides the
# "enabled" flag in providers.json).
ONLY_PROVIDERS = [
    p.strip() for p in _clean(os.environ.get("LNA_ONLY_PROVIDERS", "")).split(",") if p.strip()
]

# --- MQTT / Home Assistant -------------------------------------------------
MQTT_HOST = _clean(os.environ.get("MQTT_HOST", ""))
MQTT_PORT = _int("MQTT_PORT", 1883)
MQTT_USERNAME = _clean(os.environ.get("MQTT_USERNAME", ""))
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_CLIENT_ID = _clean(os.environ.get("MQTT_CLIENT_ID", "library-news-access"))
MQTT_BASE_TOPIC = _clean(os.environ.get("MQTT_BASE_TOPIC", "library/news-access"))
HA_DISCOVERY_PREFIX = _clean(os.environ.get("HA_DISCOVERY_PREFIX", "homeassistant"))

# --- Notifications ---------------------------------------------------------
# Optional ntfy alert when a renewal fails. Matches the ShyMoose ntfy setup.
NTFY_URL = _clean(os.environ.get("LNA_NTFY_URL", "")).rstrip("/")
NTFY_TOPIC = _clean(os.environ.get("LNA_NTFY_TOPIC", ""))
NTFY_USERNAME = _clean(os.environ.get("LNA_NTFY_USERNAME", ""))
NTFY_PASSWORD = os.environ.get("LNA_NTFY_PASSWORD", "")
NTFY_TOKEN = _clean(os.environ.get("LNA_NTFY_TOKEN", ""))

# Optional Uptime Kuma push monitor. Pinged only when every provider succeeds.
KUMA_PUSH_URL = _clean(os.environ.get("LNA_KUMA_PUSH_URL", ""))

# --- Runtime ---------------------------------------------------------------
DATA_DIR = _clean(os.environ.get("LNA_DATA_DIR", "/data"))
LOG_LEVEL = _clean(os.environ.get("LNA_LOG_LEVEL", "INFO")).upper()
PORT = _int("LNA_PORT", 8781)


def credentials_for(provider_id: str) -> dict:
    """Per-provider identity, falling back to the shared account."""
    prefix = f"LNA_{provider_id.upper()}_"
    return {
        "first_name": _clean(os.environ.get(prefix + "FIRST_NAME", "")) or FIRST_NAME,
        "last_name": _clean(os.environ.get(prefix + "LAST_NAME", "")) or LAST_NAME,
        "email": _clean(os.environ.get(prefix + "EMAIL", "")) or EMAIL,
        "password": os.environ.get(prefix + "PASSWORD", "") or PASSWORD,
    }


def missing_required(needs_account: bool = True) -> list[str]:
    """Required settings that are not configured."""
    missing = []
    if not CARD_NUMBER:
        missing.append("LNA_CARD_NUMBER")
    if needs_account:
        if not FIRST_NAME:
            missing.append("LNA_FIRST_NAME")
        if not LAST_NAME:
            missing.append("LNA_LAST_NAME")
        if not EMAIL:
            missing.append("LNA_EMAIL")
        if not PASSWORD:
            missing.append("LNA_PASSWORD")
    return missing
