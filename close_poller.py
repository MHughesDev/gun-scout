"""Auction close poller.

A live auction's current bid is only a lower bound; the final hammer price is
the one true market-clearing price we ever observe. So once an auction's
ends_at passes, re-fetch the item from the GunBroker API and fold what it
actually closed at into that listing's stat fact
(statstore.ENGINE.record_auction_close). Pending auctions are tracked in
process memory only — the stats engine notes them as search threads observe
GunBroker auctions — so a restart simply stops tracking auctions it hasn't
re-seen. Runs as a daemon thread; idles quietly when no API key is configured.

NOTE: the ended-item response shape is best-effort until a dev key exists to
verify against — field fallbacks below cover the documented variants.
"""
import logging
import threading
import time

import requests

import statstore
from clients.gunbroker import API, _dev_key, _parse_ts

log = logging.getLogger("gun_scout.closes")

INTERVAL = 600  # seconds between cycles


def start():
    threading.Thread(target=_loop, daemon=True, name="auction-close-poller").start()


def _loop():
    while True:
        try:
            n = _check_once()
            if n:
                log.info("recorded %d auction close(s)", n)
        except Exception:
            log.exception("auction close poller cycle failed")
        time.sleep(INTERVAL)


def _check_once() -> int:
    key = _dev_key()
    if not key:
        return 0  # nothing we can do without the API; stay quiet
    pending = statstore.ENGINE.auctions_needing_close_check()
    if not pending:
        return 0

    session = requests.Session()
    session.headers.update({"X-DevKey": key, "Content-Type": "application/json"})
    done = 0
    for auction in pending:
        h, item_id = auction["url_hash"], auction["item_id"]
        try:
            resp = session.get(f"{API}/Items/{item_id}", timeout=25)
            if resp.status_code == 404:
                # item page is gone; the close price is unknowable now
                statstore.ENGINE.record_auction_close(h, None)
                done += 1
                continue
            resp.raise_for_status()
            item = resp.json()
        except requests.RequestException as e:
            log.warning("close check for item %s failed: %s", item_id, e)
            continue  # leave unchecked; retried next cycle

        new_end = _parse_ts(item.get("endingDate"))
        if new_end and new_end > time.time():
            # auction was extended / relisted — check again after the new end
            statstore.ENGINE.reschedule_auction(h, new_end)
            continue

        bids = item.get("bidCount") or 0
        price = item.get("currentBid") or item.get("highBid") or item.get("price")
        try:
            price = float(price) if price else None
        except (TypeError, ValueError):
            price = None
        # no bids = ended without a sale; mark checked with no final price
        statstore.ENGINE.record_auction_close(h, price if bids else None)
        done += 1
        time.sleep(0.8)  # be polite
    return done
