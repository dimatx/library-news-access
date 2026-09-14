# Library News Access

Your library gives you free newspaper access, but the pass expires every day or
three and you have to re-enter your card number to get it back. This renews it
for you.

It runs as a small container, checks each newspaper on a schedule, and re-ups
the pass before it lapses. Optionally it reports status and expiry to Home
Assistant.

Built against **Memorial Hall Library** (Andover, MA), whose "Connect to…"
portal is a widely used library product — so it may work for your library with
only a URL change. See [Other libraries](#other-libraries).

## Newspapers

| Newspaper | Pass length | Status |
|---|---|---|
| **Boston Globe** | 72 hours | Automated |
| **New York Times** | 24 hours | Automated (needs a one-time session export) |
| Wall Street Journal | 3 days | Not built yet |
| Washington Post | 7 days | Not built yet |
| Eagle Tribune (NewsBank) | — | Not applicable |

Each of these was checked by replaying the flow by hand; details are in
[FINDINGS.md](FINDINGS.md).

Eagle Tribune is excluded on purpose: entering your card mints a throwaway
browsing session with no account behind it, so there is no pass to keep alive.

## What you need

- A library card for a library using the `/connect/<id>` portal
- An account with each newspaper (free to create; the library pass attaches to
  it). For the Globe, this is the account you already sign in with
- Docker

## Quick start

```bash
git clone https://github.com/dimatx/library-news-access
cd library-news-access
cp .env.example .env      # fill in your card number and newspaper login
docker compose up -d --build
```

Open <http://localhost:8781> for a status page showing each paper and when its
pass expires.

To run a check immediately instead of waiting for the scheduler:

```bash
curl -X POST http://localhost:8781/run
```

Both the Boston Globe and the New York Times are enabled by default. NYT will
report a failure until you complete the session export below; if you would
rather not use it at all, set `enabled: false` for it in `providers.json` or
set `LNA_ONLY_PROVIDERS=boston_globe`.

> The Globe's form is labelled **"Create Account"** even when you already have
> one. Submitting it with your existing email and password is how the library
> intends you to re-up access — it does not create a duplicate or reset your
> password. It is exactly what you would be doing by hand.

## New York Times setup

NYT needs one thing from you up front. Its login page is behind bot protection,
so this service **never tries to log in**. Instead you capture a browser
session once and it reuses it.

1. Sign in at [nytimes.com](https://www.nytimes.com) in your browser
2. Export cookies for `nytimes.com` using an extension such as
   *Get cookies.txt LOCALLY* (Netscape format)
3. Copy the file into the container's data volume:

```bash
docker cp nytimes.com_cookies.txt library-news-access:/data/nyt_cookies.txt
```

The session lasts roughly a year. When it expires the provider fails loudly
rather than going quiet, so you will see it on the status page and in Home
Assistant — re-export and copy it in again.

To keep the file elsewhere, point `LNA_NYT_COOKIE_FILE` at it.

## Configuration

Everything is environment variables. See [`.env.example`](.env.example).

### Required

| Variable | Purpose |
|---|---|
| `LNA_CARD_NUMBER` | Library card number, digits only |
| `LNA_FIRST_NAME`, `LNA_LAST_NAME` | Name on the newspaper account |
| `LNA_EMAIL`, `LNA_PASSWORD` | Your existing newspaper login |

Different credentials per newspaper? Prefix with the provider id, e.g.
`LNA_BOSTON_GLOBE_EMAIL`, `LNA_NEW_YORK_TIMES_EMAIL`.

### Scheduling

| Variable | Default | Purpose |
|---|---|---|
| `LNA_CHECK_MINUTES` | `30` | How often to check whether anything is due |
| `LNA_RENEW_MARGIN_HOURS` | `24` | Renew this long before a pass lapses |
| `LNA_RUN_ON_START` | `true` | Check once at startup |
| `LNA_ONLY_PROVIDERS` | — | Comma-separated provider ids; overrides `providers.json` |

### Home Assistant (optional)

| Variable | Default | Purpose |
|---|---|---|
| `MQTT_HOST` | — | Broker address. **Empty disables Home Assistant entirely** |
| `MQTT_PORT` | `1883` | |
| `MQTT_USERNAME`, `MQTT_PASSWORD` | — | If your broker requires auth |
| `MQTT_BASE_TOPIC` | `library/news-access` | |
| `HA_DISCOVERY_PREFIX` | `homeassistant` | Match your HA discovery prefix |

### Alerting (optional)

| Variable | Purpose |
|---|---|
| `LNA_NTFY_URL`, `LNA_NTFY_TOPIC` | [ntfy](https://ntfy.sh) alert when a renewal fails |
| `LNA_NTFY_USERNAME` / `LNA_NTFY_PASSWORD`, or `LNA_NTFY_TOKEN` | ntfy auth |
| `LNA_KUMA_PUSH_URL` | Uptime Kuma push URL, pinged only when every paper is fine |

### Other

| Variable | Default | Purpose |
|---|---|---|
| `LNA_CONNECT_BASE` | `https://mhl.org/connect` | Your library's portal |
| `LNA_NYT_COOKIE_FILE` | `/data/nyt_cookies.txt` | Where the NYT session lives |
| `LNA_PROVIDERS_FILE` | bundled | Use your own `providers.json` |
| `LNA_DATA_DIR` | `/data` | Where renewal state is stored |
| `LNA_LOG_LEVEL` | `INFO` | |
| `LNA_PORT` | `8781` | |
| `LNA_HEALTH_FAILURE_THRESHOLD` | `3` | Consecutive failures before `/health` reports unhealthy |
| `LNA_REQUEST_TIMEOUT` | `45` | Per-request timeout, seconds |
| `LNA_USER_AGENT` | Chrome | Sent to the library and publishers |
| `MQTT_CLIENT_ID` | `library-news-access` | Change if it clashes on your broker |
| `LNA_NYT_TOKEN` | bundled | NYT's public API token. Only needed if they rotate it before this repo does |

## Home Assistant

Set `MQTT_HOST` and the service publishes over MQTT discovery — one device per
newspaper, so adding a paper adds a device rather than cluttering an existing
one:

| Entity | Meaning |
|---|---|
| `sensor.<paper>_access_expires` | When the current pass runs out (timestamp) |
| `binary_sensor.<paper>_access_active` | Whether you currently have access |
| `sensor.<paper>_access_status` | `ok` or `failed`, with the last message and failure count as attributes |

A good automation: alert when `binary_sensor.*_access_active` has been `off`
for an hour. That means several renewals failed in a row, which usually points
at an expired library card or a dead NYT session — both need you, not a retry.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /` | Status page |
| `GET /health` | `200` when healthy, `503` with a `problems` list otherwise |
| `POST /run` | Force an immediate check |

`/health` is deliberately tolerant of blips. A single failed poll — the library
being briefly unreachable, say — is reported under `warnings` while still
returning `200`, because the retry backoff already handles it and restarting
the container would not help. It returns `503` only when something actually
needs you: configuration is missing, a pass has genuinely lapsed, or a provider
has failed `LNA_HEALTH_FAILURE_THRESHOLD` times in a row (default 3).

## How it works

Each newspaper is an entry in [`providers.json`](providers.json) with a mode:

- **`ez_register`** — the library hands you to a publisher form that grants a
  timed pass; re-submitting it renews. Used by the Globe.
- **`nyt_redeem`** — the library hands you an access code, redeemed through
  NYT's API against the browser session you exported.
- **`link_only`** — resolves the current redemption link and shows it on the
  status page, for papers that need a manual step.

Renewal is driven by each pass's real expiry rather than a fixed timer, so
restarting the container does not trigger a pointless renewal, and a pass you
redeemed by hand is noticed rather than duplicated. State lives in
`/data/state.json`.

When a renewal fails the next attempt backs off — 5 min, 15 min, 1 h, 4 h,
12 h, then daily. A transient blip recovers quickly, while a publisher that is
genuinely refusing gets asked once a day rather than every half hour.

No browser or JavaScript engine is involved at runtime, so the image stays
small and there is no headless Chrome to babysit.

## Adding a newspaper

Add an entry to `providers.json` and mount it over the bundled one (there is a
commented-out volume in `compose.yaml`) — no rebuild needed.

To find a newspaper's id, look at the `/connect/<id>` link on your library's
database list. Submit your card by hand once to see what you get: a publisher
form granting a timed pass means `ez_register`; a static redemption link means
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

`field_map` maps a logical field to the publisher's HTML input names; listing
two names (as with the password) fills both. Hidden fields the form already
carries — including ASP.NET's `__VIEWSTATE` — are echoed back automatically.

The WSJ and Washington Post are both rolling passes and look automatable;
contributions welcome.

## Other libraries

The `/connect/<id>` portal is a library product rather than something specific
to one library, so this may work elsewhere with two changes:

1. Point `LNA_CONNECT_BASE` at your library's portal
2. Replace the `db_id` values in `providers.json` with your library's

The newspapers behave the same way regardless of which library sent you, so the
provider logic should carry over.

## Troubleshooting

**"Not a valid library card number."** The library rejected the card — expired
or mistyped. Nothing the service can do.

**"the publisher's form no longer has the expected fields"** The publisher
changed their form. Compare the live input names against `field_map`. This is
deliberately a loud failure rather than a silent pretend-success.

**NYT: "session is no longer logged in"** The exported cookies have expired.
Re-export and copy them in again.

**Renewals succeed but I still hit a paywall.** The pass attaches to your
*newspaper account*, not to this container. Sign in to the newspaper normally
with the same email you configured.

**Home Assistant shows nothing.** `MQTT_HOST` is probably unset — the status
page reports whether MQTT is enabled.

## Security

Credentials are read from the environment at runtime and never committed. The
only things written to disk are `/data/state.json` (renewal timestamps and the
last status message) and the NYT session file you supply.

That session file is a live login to your NYT account — treat it like a
password, and prefer a named volume over a bind mount in a shared directory.

## Deployment

CI publishes a multi-arch image (`linux/amd64` and `linux/arm64`) on every push
to `main`:

```
ghcr.io/dimatx/library-news-access:latest
```

The bundled `compose.yaml` builds locally, which is what you want while
developing. To run the published image instead, swap `build: .` for
`image: ghcr.io/dimatx/library-news-access:latest` and supply the environment
from your orchestrator.

## Licence

No licence has been set yet, so default copyright applies. If you want to use
this, open an issue and one will be added.
