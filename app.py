"""Flask status page + background renewal loop.

The loop wakes every LNA_CHECK_MINUTES and renews any provider that is due.
"Due" means either renew_every_hours has elapsed since the last success, or
access is within LNA_RENEW_MARGIN_HOURS of lapsing -- so a transient failure
still leaves many retries before the user ever notices.
"""

import logging
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string

import config
import mqtt_publisher
import notify
import providers as providers_mod
import state

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_LOGGER = logging.getLogger("library-news-access")

app = Flask(__name__)

VERSION = "2026-09-14"

STATE = {
    "last_cycle": None,
    "last_success": None,
    "last_error": None,
    "cycles": 0,
    "results": {},
}
_lock = threading.Lock()
_cycle_lock = threading.Lock()


def _providers() -> list:
    try:
        return providers_mod.load_providers()
    except Exception as exc:  # noqa: BLE001
        _LOGGER.error("Could not load providers: %s", exc)
        return []


def run_cycle(force: bool = False) -> dict:
    """Renew everything due. Serialised so overlapping triggers can't collide."""
    with _cycle_lock:
        selected = _providers()
        with _lock:
            STATE["last_cycle"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            STATE["cycles"] += 1

        missing = config.missing_required(
            needs_account=any(p.needs_account for p in selected)
        )
        if missing:
            message = f"Missing required settings: {', '.join(missing)}"
            _LOGGER.error(message)
            with _lock:
                STATE["last_error"] = message
            return {"ok": False, "message": message, "results": []}

        stored = state.load()
        results = []
        failures = []

        for provider in selected:
            entry = stored.get(provider.id)
            if not force and not state.is_due(provider, entry):
                results.append({
                    "id": provider.id,
                    "name": provider.name,
                    "mode": provider.mode,
                    "skipped": True,
                    "ok": True,
                    "message": "not due yet",
                    "expires_at": (entry or {}).get("expires_at"),
                    "url": (entry or {}).get("url"),
                })
                continue

            result = providers_mod.run(provider, entry=entry)
            state.record(provider.id, result)
            result["skipped"] = False
            results.append(result)
            if not result["ok"]:
                failures.append(result)

        stored = state.load()

        try:
            mqtt_publisher.publish(selected, stored)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("MQTT publish failed: %s", exc)

        notify.notify_failures(failures)

        ok = not failures
        summary = (
            "all providers current"
            if ok else f"{len(failures)} provider(s) failed"
        )
        notify.heartbeat(ok, summary)

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _lock:
            STATE["results"] = {r["id"]: r for r in results}
            STATE["last_error"] = None if ok else "; ".join(
                f"{f['name']}: {f['message']}" for f in failures
            )
            if ok:
                STATE["last_success"] = now

        _LOGGER.info("Cycle complete: %s", summary)
        return {"ok": ok, "message": summary, "results": results}


def _loop() -> None:
    interval = max(60, config.CHECK_MINUTES * 60)
    time.sleep(5)  # let the web server finish booting
    if config.RUN_ON_START:
        try:
            run_cycle()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Startup cycle failed")
    while True:
        time.sleep(interval)
        try:
            run_cycle()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Scheduled cycle failed")


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Library News Access</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 760px; margin: 2rem auto; padding: 0 1rem; color: #1f2937; }
    h1 { font-size: 1.4rem; }
    .card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 1rem 1.25rem; margin: 1rem 0; }
    .ok { color: #15803d; } .err { color: #b91c1c; } .muted { color: #6b7280; font-size: .85rem; }
    table { width: 100%; border-collapse: collapse; }
    th { text-align: left; font-size: .8rem; text-transform: uppercase; color: #6b7280; padding-bottom: .4rem; }
    td { padding: .35rem 0; border-top: 1px solid #f3f4f6; vertical-align: top; }
    code { background:#f3f4f6; padding:.1rem .3rem; border-radius:4px; font-size:.85rem; }
    a { color: #1d4ed8; }
  </style>
</head>
<body>
  <h1>Library News Access</h1>
  <div class="card">
    {% if missing %}
      <p class="err">&#9888; Missing config: {{ missing|join(', ') }}</p>
    {% elif state.last_error %}
      <p class="err">&#10007; {{ state.last_error }}</p>
    {% elif state.last_success %}
      <p class="ok">&#10003; All newspaper passes are current</p>
    {% else %}
      <p class="muted">Waiting for the first cycle&hellip;</p>
    {% endif %}
  </div>

  <div class="card">
    <table>
      <tr><th>Newspaper</th><th>Status</th><th>Expires</th></tr>
      {% for p in providers %}
      {% set e = stored.get(p.id, {}) %}
      <tr>
        <td>
          {{ p.name }}<br>
          <span class="muted">{{ p.mode }}</span>
          {% if p.mode == 'link_only' and e.get('url') %}
            <br><a href="{{ e['url'] }}" target="_blank" rel="noopener">redeem link</a>
          {% endif %}
        </td>
        <td class="{{ 'ok' if e.get('ok') else 'err' }}">
          {{ 'ok' if e.get('ok') else 'failed' }}
          <br><span class="muted">{{ e.get('message') or '&mdash;'|safe }}</span>
        </td>
        <td>{{ e.get('expires_at') or '&mdash;'|safe }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>

  <div class="card muted">
    <table>
      <tr><td>Last cycle</td><td>{{ state.last_cycle or '&mdash;'|safe }}</td></tr>
      <tr><td>Last full success</td><td>{{ state.last_success or '&mdash;'|safe }}</td></tr>
      <tr><td>Cycles run</td><td>{{ state.cycles }}</td></tr>
      <tr><td>Check interval</td><td>{{ check }} min</td></tr>
      <tr><td>MQTT</td><td>{{ mqtt or 'disabled' }}</td></tr>
    </table>
  </div>
  <p class="muted">POST <code>/run</code> to renew now &middot; {{ version }}</p>
</body>
</html>"""


@app.route("/")
def index():
    with _lock:
        snapshot = dict(STATE)
    selected = _providers()
    return render_template_string(
        _PAGE,
        state=snapshot,
        providers=selected,
        stored=state.load(),
        missing=config.missing_required(
            needs_account=any(p.needs_account for p in selected)
        ),
        check=config.CHECK_MINUTES,
        mqtt=f"{config.MQTT_HOST}:{config.MQTT_PORT}" if mqtt_publisher.enabled() else "",
        version=VERSION,
    )


@app.route("/health")
def health():
    """Liveness for an orchestrator, not a report card on the publishers.

    A single failed poll is normal: the library or a newspaper can blip, and
    the retry backoff already handles it. Reporting unhealthy for that pages
    someone for a third-party hiccup that needs no action, and invites a
    restart that cannot possibly fix it.

    So this is unhealthy only when something genuinely needs attention:
    missing configuration, a pass that has actually lapsed, or a provider that
    has failed enough times in a row to look like a real fault rather than a
    blip. Transient failures are still reported, under `warnings`.
    """
    selected = _providers()
    stored = state.load()
    missing = config.missing_required(
        needs_account=any(p.needs_account for p in selected)
    )

    now = datetime.now(timezone.utc)
    problems = []
    warnings = []
    for provider in selected:
        entry = stored.get(provider.id) or {}
        if not entry:
            # Nothing tried yet. The startup cycle will see to it.
            warnings.append(f"{provider.name}: not run yet")
            continue

        failures = int(entry.get("consecutive_failures", 0))
        expires = state.parse_ts(entry.get("expires_at"))
        lapsed = expires is not None and expires <= now

        if lapsed:
            problems.append(f"{provider.name}: access lapsed at {entry['expires_at']}")
        elif failures >= config.HEALTH_FAILURE_THRESHOLD:
            problems.append(
                f"{provider.name}: {failures} consecutive failures "
                f"({entry.get('message')})"
            )
        elif not entry.get("ok"):
            warnings.append(
                f"{provider.name}: last attempt failed, retrying "
                f"({entry.get('message')})"
            )

    healthy = not problems and not missing
    with _lock:
        body = {
            "healthy": healthy,
            "problems": problems,
            "warnings": warnings,
            "missing_config": missing,
            "last_cycle": STATE["last_cycle"],
            "last_success": STATE["last_success"],
            "cycles": STATE["cycles"],
            "providers": stored,
        }
    return jsonify(body), (200 if healthy else 503)


@app.route("/run", methods=["POST"])
def run_now():
    result = run_cycle(force=True)
    return jsonify(result), (200 if result["ok"] else 500)


def _start_background() -> None:
    threading.Thread(target=_loop, daemon=True, name="renew-loop").start()


_start_background()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT)
