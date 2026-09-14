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
import state as state_mod
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
    # --- nyt_redeem ---
    cookie_file: str = ""
    entitlement: str = ""

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


def _run_nyt_redeem(
    provider: Provider, session: requests.Session, card: str, entry: dict | None = None
) -> dict:
    """Redeem the library's NYT access code against a stored browser session."""
    import nyt

    entry = entry or {}
    cookie_file = provider.cookie_file or config.NYT_COOKIE_FILE
    try:
        nyt.load_cookies(session, cookie_file)
    except nyt.SessionExpired as exc:
        raise ConnectorError(str(exc)) from exc

    # The library mints the redirect that carries the access code, so the card
    # still has to be presented every time.
    location, _ = post_card(session, provider.db_id, card, follow=False)

    try:
        access_code, campaign_id = nyt.parse_redeem_url(location)
        state = nyt.account_state(session, campaign_id)
    except nyt.SessionExpired as exc:
        raise ConnectorError(str(exc)) from exc

    wanted = provider.entitlement or "AAA"
    now = datetime.now(timezone.utc)

    # NYT's data layer keeps reporting hasActiveEntitlements for hours after
    # the pass has actually lapsed, still carrying the old endDate. Trusting
    # that flag alone left the pass expired and unrenewed for ~8 hours, so the
    # end date wins whenever we have one.
    expires = state_mod.parse_ts(state.get("expires_at"))
    not_expired = expires is None or expires > now
    if expires is not None and expires <= now and state.get("has_active"):
        _LOGGER.info(
            "%s: NYT still reports an active pass but it expired at %s; renewing",
            provider.name, state.get("expires_at"),
        )

    already_entitled = (
        state.get("has_active")
        and wanted in (state.get("entitlements") or [])
        and not_expired
    )

    if already_entitled:
        # Nothing to do. Redeeming now would only return 'already redeemed'.
        return {
            "ok": True,
            "status": "ok",
            "message": f"Pass active until {state.get('expires_at') or 'unknown'}",
            "url": location,
            "renewed_at": now.isoformat(timespec="seconds"),
            "expires_at": state.get("expires_at"),
            "keep_expires": state.get("expires_at") is None,
            "code": access_code,
        }

    # The browser always runs CheckAccessCode immediately before redeeming, and
    # skipping it is the one substantive difference between our request and a
    # working browser redemption. It also tells us whether the library's
    # certificate itself is healthy, which the redemption mutation never does.
    try:
        check = nyt.check_access_code(session, access_code)
        _LOGGER.info(
            "%s: access code is %s (valid until %s)",
            provider.name,
            check.get("status") or "?",
            check.get("expiration_date") or "?",
        )
    except (nyt.RedemptionRefused, nyt.SessionExpired) as exc:
        raise ConnectorError(str(exc)) from exc

    try:
        result = nyt.redeem(session, access_code, campaign_id)
    except (nyt.RedemptionRefused, nyt.SessionExpired) as exc:
        raise ConnectorError(str(exc)) from exc

    if result.get("already_active"):
        # NYT disagrees with the data layer (usually propagation lag). Re-read
        # so the expiry we publish comes from NYT rather than a guess.
        try:
            state = nyt.account_state(session, campaign_id)
        except nyt.SessionExpired:
            state = {}
        return {
            "ok": True,
            "status": "ok",
            "message": "Code already redeemed; pass still active",
            "url": location,
            "renewed_at": now.isoformat(timespec="seconds"),
            "expires_at": state.get("expires_at"),
            "keep_expires": state.get("expires_at") is None,
            "code": access_code,
        }

    days = result.get("duration_days")

    # The mutation's own subscriptionEndDate has been seen to disagree with the
    # subscription record NYT then creates, so re-read the authoritative value
    # rather than publishing the one the mutation echoed back.
    expires_at = None
    try:
        state = nyt.account_state(session, campaign_id)
        expires_at = state.get("expires_at")
    except nyt.SessionExpired:
        pass
    if not expires_at:
        expires_at = result.get("expires_at")
    if not expires_at and provider.access_hours:
        expires_at = (
            now + timedelta(hours=provider.access_hours)
        ).isoformat(timespec="seconds")

    return {
        "ok": True,
        "status": "ok",
        "message": f"Redeemed {result.get('subscription_name') or 'access'}"
                   + (f" for {days} day(s)" if days else ""),
        "url": location,
        "renewed_at": now.isoformat(timespec="seconds"),
        "expires_at": expires_at,
        "code": access_code,
    }


_RUNNERS = {
    "ez_register": _run_ez_register,
    "link_only": _run_link_only,
    "nyt_redeem": _run_nyt_redeem,
}


def run(provider: Provider, card: str | None = None, entry: dict | None = None) -> dict:
    """Run one provider. Never raises; failures come back as ``ok: False``.

    ``entry`` is the provider's stored state, so a runner can avoid repeating
    work it already knows is pointless.
    """
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
        if provider.mode == "nyt_redeem":
            outcome = runner(provider, session, card, entry)
        else:
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
