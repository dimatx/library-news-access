# Library News Access

Keeps the **Boston Globe** pass that Memorial Hall Library grants you from
lapsing, without you having to re-type a library card every three days.

The library's Globe pass lasts **72 hours**. The connector page says so
outright: *"At the end of your free temporary access period, simply complete
this form again."* That is the entire problem this solves — it completes the
form again, on a timer, and tells Home Assistant how long you have left.

## What it does and does not automate

Every newspaper MHL offers was measured before any code was written (see
[FINDINGS.md](FINDINGS.md)). Only one of them is a renewal treadmill:

| Newspaper | Automated? | Why |
|---|---|---|
| **Boston Globe** | **Yes** | Rolling 72-hour entitlement on an account you own. Expires constantly. |
| New York Times | No (link only) | Static gift code, redeemed once against your NYT login. |
| Wall Street Journal | No (link only) | Static partner redemption link, needs an interactive login. |
| Washington Post | No (link only) | Static special-offer link, needs an interactive login. |
| Eagle Tribune (NewsBank) | **No** | Card entry mints a throwaway browsing session. No account, no entitlement, nothing to keep alive. |

The three `link_only` providers ship **disabled**. Enable them only if you want
the current redemption URL surfaced on the status page; they are not renewals
and re-running them achieves nothing.

## Quick start

```bash
cp .env.example .env
# fill in LNA_CARD_NUMBER and your existing Globe login
docker compose up -d --build
```

Then open <http://localhost:8781>.

Run it once by hand without waiting for the scheduler:

```bash
docker compose run --rm library-news-access python cli.py --force
docker compose run --rm library-news-access python cli.py --list
```

> The Globe's form says **"Create Account"** even though you already have one.
> Submitting it with your existing email and password is the intended way to
> re-up access — that is exactly what the manual process does.

## Deployment

CI publishes a multi-arch image (`linux/amd64` + `linux/arm64`) on every push
to `main`:

```
ghcr.io/dimatx/library-news-access:latest
```

To run the published image instead of building, swap `build: .` for
`image: ghcr.io/dimatx/library-news-access:latest` in `compose.yaml` and supply
the environment from your orchestrator (Komodo, Portainer, plain compose with
an `.env` file — anything). `compose.yaml` in this repo builds locally, which is
what you want for development.

**No credentials belong in this repo.** The card number, name, email, and
password are read from the environment at runtime only.

## How renewal is scheduled

The loop wakes every `LNA_CHECK_MINUTES` (default 30) and renews a provider if
either is true:

- `renew_every_hours` has elapsed since the last success (Globe: 24h), or
- access is within `LNA_RENEW_MARGIN_HOURS` of lapsing (default 24h).

With a 72-hour window renewed every 24 hours, roughly **two full days of
retries** are available before access could actually lapse. State lives in
`/data/state.json`, so a restart does not trigger an unnecessary renewal.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LNA_CARD_NUMBER` | — | **Required.** Library card number, no spaces. |
| `LNA_CONNECT_BASE` | `https://mhl.org/connect` | Library connector base. Change for a different library. |
| `LNA_FIRST_NAME` / `LNA_LAST_NAME` | — | Required for renewable providers. |
| `LNA_EMAIL` / `LNA_PASSWORD` | — | Your existing newspaper account. |
| `LNA_CHECK_MINUTES` | `30` | Scheduler tick. |
| `LNA_RENEW_MARGIN_HOURS` | `24` | Renew this long before access lapses. |
| `LNA_RUN_ON_START` | `true` | Run a pass at container start. |
| `LNA_ONLY_PROVIDERS` | — | Comma-separated ids; overrides `enabled` in `providers.json`. |
| `MQTT_HOST` | — | Broker address. Leave empty to disable Home Assistant publishing. |
| `LNA_NTFY_URL` / `LNA_NTFY_TOPIC` | — | ntfy alert when a renewal fails. |
| `LNA_KUMA_PUSH_URL` | — | Uptime Kuma push, pinged only when everything succeeded. |

Per-provider identity overrides use the provider id, e.g.
`LNA_BOSTON_GLOBE_EMAIL`.

## Home Assistant

With `MQTT_HOST` set, MQTT discovery creates **one device per newspaper** — so
adding a paper adds a device rather than more prefixes on a shared one:

- `sensor.boston_globe_access_status` — `ok` / `failed`, with the last message,
  failure streak, and notes as attributes
- `sensor.boston_globe_access_expires` — timestamp
- `binary_sensor.boston_globe_access_active` — `connectivity` class

A useful automation is to alert when `binary_sensor.*_access_active` has been
`off` for an hour — that means several renewal attempts failed in a row, which
usually means an expired library card.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /` | Status page: per-newspaper state, expiry, redemption links. |
| `GET /health` | `200` healthy / `503` with a `problems` array. Compose healthcheck uses this. |
| `POST /run` | Force an immediate renewal pass. |

## Adding a newspaper

Add an entry to `providers.json` and mount it (the commented-out volume in
`compose.yaml`) — no rebuild needed.

For another library the connector `db_id` is the number in the
`/connect/<db_id>` URL. To confirm a new newspaper is the renewable kind,
submit the card by hand and see whether you land on a form that grants a
*time-limited* window; if you land on a static redemption code, use
`link_only`.

```json
{
  "id": "example_paper",
  "name": "Example Paper",
  "db_id": "12345",
  "mode": "ez_register",
  "enabled": true,
  "access_hours": 72,
  "renew_every_hours": 24,
  "registrar_hosts": ["manage.example.com"],
  "success_hosts": ["example.com"],
  "field_map": {
    "first_name": ["txtFirst"],
    "last_name": ["txtLast"],
    "email": ["txtEmail"],
    "password": ["txtPassword", "txtVerifyPassword"]
  }
}
```

`field_map` maps a logical field to the publisher's input `name`s; listing two
names (as with the password) fills both. Every other field already on the form
— including ASP.NET's `__VIEWSTATE` — is echoed back automatically.

## Troubleshooting

**"Not a valid library card number."** The library rejected the card. It is
expired or mistyped; the automation cannot fix this.

**"the publisher's form no longer has the expected fields"** The publisher
changed their form. Compare the live form's input names against `field_map`.
This is deliberately a hard failure rather than a silent success.

**"no registration form found at ..."** The card was accepted but the publisher
sent us somewhere unexpected — usually a flow change on their side.

**Renewals succeed but the browser still asks me to subscribe.** The pass
attaches to the Globe *account*, not to this container's throwaway session. Log
in to bostonglobe.com normally as `LNA_EMAIL`.

## Notes

- No browser, JavaScript, or CAPTCHA is involved anywhere in the Globe chain,
  which is why this is plain `requests` and not Playwright.
- Credentials are environment-only; `.env` is gitignored. Nothing is written to
  `/data` except renewal timestamps and the last status message.
- MQTT, ntfy, and Kuma failures are logged and swallowed — they can never turn
  a successful renewal into a failed run.
