"""One-shot runner: renew everything now and print the outcome.

    docker compose run --rm library-news-access python cli.py
    docker compose run --rm library-news-access python cli.py --list
    docker compose run --rm library-news-access python cli.py --force
"""

import argparse
import logging
import sys

import config
import providers as providers_mod
import state


def main() -> int:
    parser = argparse.ArgumentParser(description="Renew library newspaper access")
    parser.add_argument("--list", action="store_true", help="list providers and exit")
    parser.add_argument("--force", action="store_true", help="run even if not due")
    parser.add_argument("--provider", action="append", help="limit to a provider id")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    selected = providers_mod.load_providers()
    if args.provider:
        wanted = set(args.provider)
        selected = [p for p in selected if p.id in wanted]

    if args.list:
        for provider in selected:
            print(f"{provider.id:24} {provider.mode:12} {provider.name}")
        return 0

    if not selected:
        print("No providers enabled.")
        return 1

    missing = config.missing_required(
        needs_account=any(p.needs_account for p in selected)
    )
    if missing:
        print(f"Missing required settings: {', '.join(missing)}")
        return 2

    stored = state.load()
    failures = 0

    for provider in selected:
        entry = stored.get(provider.id)
        if not args.force and not state.is_due(provider, entry):
            expires = entry.get("expires_at") if entry else None
            print(f"[skip] {provider.name}: not due yet (expires {expires})")
            continue

        result = providers_mod.run(provider)
        state.record(provider.id, result)

        mark = "ok  " if result["ok"] else "FAIL"
        print(f"[{mark}] {provider.name}: {result['message']}")
        if result.get("expires_at"):
            print(f"       expires: {result['expires_at']}")
        if result.get("url") and provider.mode == "link_only":
            print(f"       link:    {result['url']}")
        if not result["ok"]:
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
