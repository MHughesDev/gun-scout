"""Auction close poller.

A live auction's current bid is only a lower bound; the final hammer price is
the one true market-clearing price we ever observe. So once an auction's
ends_at passes, re-fetch the item from the GunBroker API and record what it
actually closed at (db.record_auction_close). Runs as a daemon thread; idles
quietly when no API key is configured.

NOTE: the ended-item response shape is best-effort until a dev key exists to
verify against — field fallbacks below cover the documented variants.
"""
import logging
import threading
import time

import requests

import db
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
    pending = db.auctions_needing_close_check("gunbroker")
    if not pending:
        return 0

    session = requests.Session()
    session.headers.update({"X-DevKey": key, "Content-Type": "application/json"})
    done = 0
    for listing in pending:
        item_id = (listing.get("extra") or {}).get("itemID")
        if not item_id:
            db.record_auction_close(listing["id"], None)
            done += 1
            continue
        try:
            resp = session.get(f"{API}/Items/{item_id}", timeout=25)
            if resp.status_code == 404:
                # item page is gone; the close price is unknowable now
                db.record_auction_close(listing["id"], None)
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
            db.reschedule_auction(listing["id"], new_end)
            continue

        bids = item.get("bidCount") or 0
        price = item.get("currentBid") or item.get("highBid") or item.get("price")
        try:
            price = float(price) if price else None
        except (TypeError, ValueError):
            price = None
        # no bids = ended without a sale; record checked with no final price
        db.record_auction_close(listing["id"], price if bids else None, bids or None)
        done += 1
        time.sleep(0.8)  # be polite
    return done
