"""Provider engine.

Two modes:

``ez_register``
    The library hands off to a publisher form that re-ups an existing account
    for a fixed window (the Boston Globe's 72 hours). Fill it, submit it, and
    confirm we get bounced to the publisher's site. This is the mode that
    actually needs a scheduler.

``link_only``
    The library hands off to a static redemption URL that a human must redeem
    once while logged in at the publisher. There is nothing to renew, so this
    just resolves and reports the current link.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import config
from connector import ConnectorError, post_card

_LOGGER = logging.getLogger(__name__)

_DEFAULT_PROVIDERS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "providers.json")

# Text that means the publisher rejected the submission. Kept broad because
# the registrar renders errors as plain text, not in a stable container.
_ERROR_HINTS = (
    "already", "invalid", "incorrect", "not valid", "error", "sorry",
    "must be", "required", "does not match", "try again", "unable",
)


@dataclass
class Provider:
    id: str
    name: str
    db_id: str
    mode: str = "ez_register"
    enabled: bool = True
    access_hours: int | None = None
    renew_every_hours: int = 24
    registrar_hosts: list[str] = field(default_factory=list)
    success_hosts: list[str] = field(default_factory=list)
    field_map: dict[str, list[str]] = field(default_factory=dict)
    notes: str = ""

    @property
    def needs_account(self) -> bool:
        return self.mode == "ez_register"


def load_providers() -> list[Provider]:
    path = config.PROVIDERS_FILE or _DEFAULT_PROVIDERS
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)

    providers = []
    for item in raw:
        known = {f for f in Provider.__dataclass_fields__}
        providers.append(Provider(**{k: v for k, v in item.items() if k in known}))

    if config.ONLY_PROVIDERS:
        wanted = set(config.ONLY_PROVIDERS)
        return [p for p in providers if p.id in wanted]
    return [p for p in providers if p.enabled]


def _host_matches(host: str, candidates: list[str]) -> bool:
    host = (host or "").lower()
    return any(host == c.lower() or host.endswith("." + c.lower()) for c in candidates)


def _pick_form(soup: BeautifulSoup, provider: Provider):
    """The form containing the provider's mapped fields."""
    wanted = {n for names in provider.field_map.values() for n in names}
    for form in soup.find_all("form"):
        present = {i.get("name") for i in form.find_all(("input", "select", "textarea"))}
        if wanted & present:
            return form
    return None


def _build_payload(form, provider: Provider, creds: dict) -> dict:
    """Echo every existing field (VIEWSTATE etc.), then overwrite ours."""
    payload: dict[str, str] = {}

    for node in form.find_all(("input", "select", "textarea")):
        name = node.get("name")
        if not name:
            continue
        node_type = (node.get("type") or "").lower()
        if node_type in {"checkbox", "radio"} and not node.has_attr("checked"):
            continue
        if node_type == "submit":
            continue
        payload[name] = node.get("value") or ""

    # A named submit button is part of the postback on some stacks; the Globe's
    # has no name, so this is a no-op there.
    for button in form.find_all("button"):
        name = button.get("name")
        if name and (button.get("type") or "submit").lower() == "submit":
            payload[name] = button.get("value") or ""
            break

    missing = []
    for logical, names in provider.field_map.items():
        value = creds.get(logical)
        hit = False
        for name in names:
            if name in payload:
                payload[name] = value if value is not None else ""
                hit = True
        if not hit:
            missing.append(f"{logical} ({'/'.join(names)})")

    if missing:
        raise ConnectorError(
            "the publisher's form no longer has the expected fields: "
            + ", ".join(missing)
        )
    return payload


def _page_error(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    for node in soup.select(".error, .validation-message, .alert, span[style*='red'], font[color='#FF0000']"):
        text = node.get_text(" ", strip=True)
        if text:
            return text[:300]

    text = " ".join(soup.get_text(" ").split())
    lowered = text.lower()
    for hint in _ERROR_HINTS:
        idx = lowered.find(hint)
        if idx != -1:
            return text[max(0, idx - 90): idx + 150].strip()[:300]
    return None


def _run_ez_register(provider: Provider, session: requests.Session, card: str) -> dict:
    creds = config.credentials_for(provider.id)
    blank = [k for k, v in creds.items() if not v]
    if blank:
        raise ConnectorError(f"missing account details: {', '.join(sorted(blank))}")

    _, landing = post_card(session, provider.db_id, card, follow=True)
    if landing is None:
        raise ConnectorError("no landing page returned by the library")

    registrar_hosts = provider.registrar_hosts or [urlparse(landing.url).hostname or ""]
    soup = BeautifulSoup(landing.text, "lxml")
    form = _pick_form(soup, provider)
    if form is None:
        # Landing somewhere unexpected usually means the publisher changed the
        # flow; say so instead of silently reporting success.
        raise ConnectorError(
            f"no registration form found at {landing.url} "
            "(the publisher may have changed their flow)"
        )

    action = urljoin(landing.url, form.get("action") or landing.url)
    payload = _build_payload(form, provider, creds)

    try:
        result = session.post(
            action,
            data=payload,
            headers={"Referer": landing.url},
            timeout=config.REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ConnectorError(f"submitting the publisher form failed: {exc}") from exc

    final_host = urlparse(result.url).hostname or ""

    if provider.success_hosts and _host_matches(final_host, provider.success_hosts):
        landed = True
    else:
        # Fallback: leaving the registrar at all counts as success.
        landed = not _host_matches(final_host, registrar_hosts)

    if not landed:
        raise ConnectorError(
            _page_error(result.text)
            or f"the publisher kept us on {final_host} without an explicit error"
        )

    now = datetime.now(timezone.utc)
    expires_at = (
        now + timedelta(hours=provider.access_hours) if provider.access_hours else None
    )
    return {
        "ok": True,
        "message": f"Access renewed via {final_host}",
        "url": result.url,
        "renewed_at": now.isoformat(timespec="seconds"),
        "expires_at": expires_at.isoformat(timespec="seconds") if expires_at else None,
    }


def _run_link_only(provider: Provider, session: requests.Session, card: str) -> dict:
    location, _ = post_card(session, provider.db_id, card, follow=False)
    now = datetime.now(timezone.utc)
    return {
        "ok": True,
        "message": "Redemption link resolved (redeem manually while signed in)",
        "url": location,
        "renewed_at": now.isoformat(timespec="seconds"),
        "expires_at": None,
    }


_RUNNERS = {
    "ez_register": _run_ez_register,
    "link_only": _run_link_only,
}


def run(provider: Provider, card: str | None = None) -> dict:
    """Run one provider. Never raises; failures come back as ``ok: False``."""
    card = card or config.CARD_NUMBER
    base = {"id": provider.id, "name": provider.name, "mode": provider.mode}

    runner = _RUNNERS.get(provider.mode)
    if runner is None:
        return {**base, "ok": False, "message": f"unknown mode '{provider.mode}'",
                "url": None, "renewed_at": None, "expires_at": None}

    session = requests.Session()
    session.headers["User-Agent"] = config.USER_AGENT
    try:
        if not card:
            raise ConnectorError("no library card number configured")
        outcome = runner(provider, session, card)
        _LOGGER.info("%s: %s", provider.name, outcome["message"])
        return {**base, **outcome}
    except ConnectorError as exc:
        _LOGGER.error("%s: %s", provider.name, exc)
        return {**base, "ok": False, "message": str(exc), "url": None,
                "renewed_at": None, "expires_at": None}
    except Exception as exc:  # noqa: BLE001 - a provider must never kill the loop
        _LOGGER.exception("%s: unexpected failure", provider.name)
        return {**base, "ok": False, "message": f"unexpected error: {exc}", "url": None,
                "renewed_at": None, "expires_at": None}
    finally:
        session.close()
