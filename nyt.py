"""New York Times: redeem the library's access code against your own account.

The library hands out a bulk certificate. Redeeming it grants a 24-hour pass
attached to an NYT account, so it has to be redeemed again once it lapses.

Unlike the Boston Globe, there is no form to submit. The redemption is a
GraphQL mutation, and NYT's login page sits behind DataDome, so this module
deliberately never attempts to log in. It reuses a browser session you
exported once (Netscape cookies.txt) and only ever calls two endpoints:

  * the data layer, to ask whether a pass is currently active
  * `redeemAccessCode`, to mint a new one when it is not

Reading real entitlement state is better than trusting a timer: it survives
clock drift, restarts, and a pass being redeemed by hand in a browser.
"""

import http.cookiejar
import logging
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import requests

import config

_LOGGER = logging.getLogger(__name__)

DATA_LAYER_URL = "https://a.nytimes.com/svc/nyt/data-layer"
GRAPHQL_URL = "https://samizdat-graphql.nytimes.com/graphql/v2"
REDEEM_REFERER = "https://www.nytimes.com/activate-access/access-code"

# Pulled from window.__preloadedData.config.gqlRequestHeaders on the redeem
# page. Static per NYT front-end build; overridable if they ever rotate it.
NYT_APP_TYPE = "project-vi"
NYT_APP_VERSION = "0.0.5"
NYT_TOKEN = os.environ.get("LNA_NYT_TOKEN", "") or (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAs+/oUCTBmD/cLdmcecrnBMHiU/pxQCn2"
    "DDyaPKUOXxi4p0uUSZQzsuq1pJ1m5z1i0YGPd1U1OeGHAChWtqoxC7bFMCXcwnE1oyui9G1uobgp"
    "m1GdhtwkR7ta7akVTcsF8zxiXx7DNXIPd2nIJFH83rmkZueKrC4JVaNzjvD+Z03piLn5bHWU6+w+"
    "rA+kyJtGgZNTXKyPh6EC6o5N+rknNMG5+CdTq35p8f99WjFawSvYgP9V64kgckbTbtdJ6YhVP58T"
    "nuYgr12urtwnIqWP9KSJ1e5vmgf3tunMqWNm6+AnsqNj8mCLdCuc5cEB74CwUeQcP2HQQmbCddBy"
    "2y0mEwIDAQAB"
)

REDEEM_MUTATION = """
mutation redeemAccessCode($accessCode: String!, $campaignId: String!) {
  redeemAccessCode(
    redeemAccessCodeInput: {
      accessCode: $accessCode
      trackingMetadata: { key: "campaignId", value: $campaignId }
    }
  ) {
    success
    emailAddress
    subscriptionName
    subscriptionProducts
    subscriptionEndDate
    subscriptionDurationDays
    purchaseToken
  }
}
""".strip()

# The browser always runs this immediately before redeeming. Captured from a
# working browser redemption; skipping it was the difference between 414 failed
# automated attempts and a browser that works every time.
CHECK_QUERY = """
query CheckAccessCode($surfaceCode: String!, $accessCode: String!) {
  accessCode(surfaceCode: $surfaceCode, accessCode: $accessCode) {
    expirationDate
    status
    subscriptionName
  }
}
""".strip()

# Surface the redeem landing page identifies itself with.
DEFAULT_SURFACE_CODE = "access-code-redemption-lp-all_access"

READY_FOR_REDEMPTION = "READY_FOR_REDEMPTION"

# NYT reports this when the account already holds an active redemption. It is
# the expected steady state, not a failure.
ALREADY_REDEEMED = "access_code_already_redeemed"

# A generic refusal. NYT returns this instead of a specific reason, and the web
# UI shows its catch-all error for it. It is NOT proof the code is used up: a
# browser redemption on the same account and code succeeded while the API was
# still answering with this.
REDEMPTION_ERROR = "access_code_redemption_error"


class SessionExpired(Exception):
    """The stored cookies are no longer a logged-in NYT session."""


class RedemptionRefused(Exception):
    """NYT declined to redeem the code, without saying why.

    Distinct from ``SessionExpired`` because the session is fine and the
    request was understood: NYT simply refused. Observed for ten days straight
    while a manual redemption in a browser, using the same account and the same
    code, succeeded. The cause is not yet known, so this is a retryable failure
    rather than a terminal one.
    """


def load_cookies(session: requests.Session, path: str) -> None:
    """Load a Netscape cookies.txt export into the session."""
    if not path:
        raise SessionExpired("no NYT cookie file configured")
    if not os.path.exists(path):
        raise SessionExpired(f"NYT cookie file not found at {path}")

    jar = http.cookiejar.MozillaCookieJar()
    try:
        # ignore_expires: a browser export often marks the session cookie as
        # expiring, and NYT still honours it.
        jar.load(path, ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as exc:
        raise SessionExpired(f"could not read NYT cookies: {exc}") from exc

    count = 0
    for cookie in jar:
        session.cookies.set_cookie(cookie)
        count += 1
    if count == 0:
        raise SessionExpired("NYT cookie file contained no cookies")
    _LOGGER.debug("Loaded %d NYT cookies", count)


def _parse_end_date(raw: str | None) -> str | None:
    """Normalise NYT's subscription end date to an ISO UTC timestamp.

    NYT sends ISO 8601 with a trailing Z (2026-09-03T23:38:03Z), so no
    timezone guessing is needed. The US-style fallback exists only because
    some tooling reformats the field; it is not what the API returns.
    """
    if not raw:
        return None
    text = str(raw).strip()

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%m/%d/%Y %H:%M:%S")
        except ValueError:
            _LOGGER.warning("Could not parse NYT end date %r", raw)
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def account_state(session: requests.Session, campaign_id: str = "") -> dict:
    """Ask NYT what this session currently has.

    Picks out the library's own subscription rather than any subscription, so
    a personal paid plan is never mistaken for the library pass (or vice
    versa). Matching order: the campaign id the library sent us, then any
    active bulk-certificate redemption, then any active subscription.
    """
    try:
        response = session.get(DATA_LAYER_URL, timeout=config.REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise SessionExpired(f"could not read NYT account state: {exc}") from exc

    if not isinstance(data, dict):
        raise SessionExpired("NYT returned an unexpected account payload")

    # isLoggedIn lives under `session`, not `user`.
    if data.get("session", {}).get("isLoggedIn") is False:
        raise SessionExpired("NYT session is no longer logged in; re-export cookies")

    subs = (data.get("user", {}).get("subInfo", {}) or {}).get("subscriptions") or []
    if isinstance(subs, dict):
        subs = [subs]

    def active(sub):
        return str(sub.get("status", "")).upper() == "ACTIVE"

    chosen = None
    if campaign_id:
        chosen = next(
            (s for s in subs if active(s) and str(s.get("campaignId")) == campaign_id),
            None,
        )
    if chosen is None:
        chosen = next(
            (s for s in subs
             if active(s) and "BULK_CERT_REDEMPTION" in (s.get("subscriptionLabels") or [])),
            None,
        )
    if chosen is None:
        chosen = next((s for s in subs if active(s)), None)

    if chosen is None:
        return {"logged_in": True, "entitlements": [], "has_active": False,
                "expires_at": None, "subscription_name": None}

    return {
        "logged_in": True,
        "entitlements": list(chosen.get("entitlements") or []),
        "has_active": bool(chosen.get("hasActiveEntitlements")),
        "expires_at": _parse_end_date(chosen.get("endDate")),
        "subscription_name": chosen.get("subscriptionName"),
    }


def parse_redeem_url(url: str) -> tuple[str, str]:
    """Pull the access code and campaign id out of the library's redirect."""
    query = parse_qs(urlparse(url).query)
    code = (query.get("gift_code") or query.get("access_code") or [""])[0]
    campaign = (query.get("campaignId") or [""])[0]
    if not code:
        raise SessionExpired(f"no access code in the library's redirect: {url}")
    return code, campaign


def _graphql(session: requests.Session, name: str, query: str, variables: dict) -> dict:
    """POST one GraphQL operation and return the parsed body.

    Headers mirror a real browser request captured from a working redemption.
    ``x-pageview-id`` and ``x-plid`` are per-pageview tracking ids the NYT
    front-end always sends; they are generated fresh per call in the same
    24-character url-safe form.
    """
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "nyt-app-type": NYT_APP_TYPE,
        "nyt-app-version": NYT_APP_VERSION,
        "nyt-token": NYT_TOKEN,
        "origin": "https://www.nytimes.com",
        # The browser sends the site root here, not the activate-access URL.
        "referer": "https://www.nytimes.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "x-nyt-internal-meter-override": "undefined",
        "x-pageview-id": secrets.token_urlsafe(18),
        "x-plid": secrets.token_urlsafe(18),
    }
    payload = {"operationName": name, "variables": variables, "query": query}
    try:
        response = session.post(
            GRAPHQL_URL, json=payload, headers=headers, timeout=config.REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        raise SessionExpired(f"NYT request failed: {exc}") from exc

    if response.status_code == 403:
        raise SessionExpired(
            "NYT rejected the request with 403 (bot protection or dead session)"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise SessionExpired(
            f"NYT returned a non-JSON response ({response.status_code})"
        ) from exc


def check_access_code(
    session: requests.Session, access_code: str, surface_code: str = DEFAULT_SURFACE_CODE
) -> dict:
    """Run the CheckAccessCode query the browser always issues before redeeming.

    Returns ``{"status", "expiration_date", "subscription_name"}``. Besides
    whatever server-side effect it has, ``status`` is a genuine health signal
    for the library's certificate: ``READY_FOR_REDEMPTION`` means the code
    itself is fine, so a subsequent refusal is about the account, not the code.
    """
    body = _graphql(
        session,
        "CheckAccessCode",
        CHECK_QUERY,
        {"accessCode": access_code, "surfaceCode": surface_code},
    )
    errors = body.get("errors") or []
    if errors:
        messages = "; ".join(str(e.get("message", e)) for e in errors)
        raise RedemptionRefused(f"NYT rejected the access code check: {messages}")

    details = (body.get("data") or {}).get("accessCode") or {}
    return {
        "status": details.get("status") or "",
        "expiration_date": details.get("expirationDate") or "",
        "subscription_name": details.get("subscriptionName") or "",
    }


def redeem(session: requests.Session, access_code: str, campaign_id: str) -> dict:
    """Run the redeemAccessCode mutation.

    Returns ``{"already_active": True}`` when NYT reports the code is already
    redeemed, which means the pass is still live.
    """
    body = _graphql(
        session,
        "redeemAccessCode",
        REDEEM_MUTATION,
        {"accessCode": access_code, "campaignId": campaign_id},
    )

    errors = body.get("errors") or []
    if errors:
        messages = [str(e.get("message", e)) for e in errors]
        if any(ALREADY_REDEEMED in m for m in messages):
            return {"already_active": True}
        if any(REDEMPTION_ERROR in m for m in messages):
            raise RedemptionRefused(
                "NYT refused the code (access_code_redemption_error). The session "
                "is valid and the request was understood, so this is NYT declining "
                "rather than anything wrong on our side. Redeeming by hand in a "
                "browser has been seen to work while this was happening."
            )
        raise SessionExpired("; ".join(messages))

    result = ((body.get("data") or {}).get("redeemAccessCode")) or {}
    if not result.get("success"):
        raise SessionExpired(f"NYT did not confirm the redemption: {body}")

    return {
        "already_active": False,
        "expires_at": result.get("subscriptionEndDate"),
        "duration_days": result.get("subscriptionDurationDays"),
        "subscription_name": result.get("subscriptionName"),
        "products": result.get("subscriptionProducts") or [],
    }
