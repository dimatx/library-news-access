"""The library side of the flow: exchange a library card for a publisher redirect.

This is the one step every provider shares. It is deliberately generic so a
different library running the same "Connect to..." connector only needs a new
``LNA_CONNECT_BASE``.
"""

import logging

import requests
from bs4 import BeautifulSoup

import config

_LOGGER = logging.getLogger(__name__)

_REDIRECT_CODES = {301, 302, 303, 307, 308}

# Selectors the connector uses to render a rejection (bad/expired card).
_ERROR_SELECTORS = ".validation-message, .nf-error-msg, .error"


class ConnectorError(Exception):
    """The library refused to hand out a pass."""


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = config.USER_AGENT
    return session


def _validation_message(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    for node in soup.select(_ERROR_SELECTORS):
        text = node.get_text(" ", strip=True)
        if text:
            return text
    return None


def post_card(
    session: requests.Session,
    db_id: str,
    card_number: str,
    follow: bool = True,
) -> tuple[str, requests.Response | None]:
    """Submit the library card and return ``(publisher_url, landing_response)``.

    ``landing_response`` is ``None`` when ``follow`` is False, which is what the
    link-only providers want: the redirect target itself is the answer and
    there is no reason to fetch the publisher's page.
    """
    url = f"{config.CONNECT_BASE}/{db_id}"

    # The connector sets a cookie on GET; skipping it works today but costs
    # nothing and keeps us closer to what a browser does.
    try:
        session.get(url, timeout=config.REQUEST_TIMEOUT)
        response = session.post(
            url,
            data={
                "mhl-connect": "",
                "db_id": db_id,
                "card_number": card_number,
            },
            timeout=config.REQUEST_TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise ConnectorError(f"could not reach the library connector: {exc}") from exc

    if response.status_code in _REDIRECT_CODES:
        location = response.headers.get("Location")
        if not location:
            raise ConnectorError("library redirected without a Location header")
        if not follow:
            return location, None
        try:
            landing = session.get(location, timeout=config.REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise ConnectorError(f"could not reach the publisher: {exc}") from exc
        return location, landing

    if response.status_code == 200:
        message = _validation_message(response.text)
        raise ConnectorError(message or "the library rejected the card number")

    raise ConnectorError(
        f"unexpected status {response.status_code} from the library connector"
    )
