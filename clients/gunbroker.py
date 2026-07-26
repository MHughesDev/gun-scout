"""GunBroker.com client.

GunBroker's website sits behind Cloudflare bot protection, so plain scraping
gets HTTP 403. They do offer a free official API (https://api.gunbroker.com,
sign up at gunbroker.com > Help > API). Set the environment variable
GUNBROKER_DEV_KEY (or drop the key in a `gunbroker_key.txt` next to app.py)
and this client uses the real API; otherwise it reports itself as blocked so
the UI can say why.
"""
import itertools
import os
import time
from pathlib import Path
from typing import Callable

import requests

from models import SearchCriteria, Listing
from .base import SiteClient, ClientBlocked, register, page_limit_reached

API = "https://api.gunbroker.com/v1"
KEY_FILE = Path(__file__).parent.parent / "gunbroker_key.txt"


def _dev_key() -> str:
    key = os.environ.get("GUNBROKER_DEV_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    return key


@register
class GunBrokerClient(SiteClient):
    name = "gunbroker"
    label = "GunBroker.com"
    homepage = "https://www.gunbroker.com"

    def search(self, criteria: SearchCriteria, emit: Callable[[Listing], None]):
        key = _dev_key()
        if not key:
            raise ClientBlocked(
                "No API key. GunBroker blocks scraping; get a free dev key at "
                "gunbroker.com (Help > API) and set GUNBROKER_DEV_KEY or create "
                "gunbroker_key.txt in the project folder."
            )

        session = requests.Session()
        session.headers.update({"X-DevKey": key, "Content-Type": "application/json"})

        params = {
            "Keywords": self._keywords(criteria),
            "PageSize": 100,  # GunBroker API max page size
            "PageIndex": 1,
            "Sort": 13,  # relevance
        }
        if criteria.condition == "new":
            params["Condition"] = 1
        elif criteria.condition == "used":
            params["Condition"] = 4
        if criteria.price_min is not None:
            params["MinPrice"] = criteria.price_min
        if criteria.price_max is not None:
            params["MaxPrice"] = criteria.price_max

        seen: set = set()
        for page in itertools.count(1):
            if page_limit_reached(criteria, page - 1):
                break
            params["PageIndex"] = page
            resp = session.get(f"{API}/Items", params=params, timeout=25)
            if resp.status_code in (401, 403):
                raise ClientBlocked(f"GunBroker API rejected the dev key (HTTP {resp.status_code})")
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results") or []
            if not results:
                break

            new_on_page = 0
            for item in results:
                iid = item.get("itemID")
                if iid in seen:
                    continue
                seen.add(iid)
                new_on_page += 1
                listing = self._to_listing(item)
                if listing and self.passes(criteria, listing):
                    emit(listing)

            # stop once we've pulled the whole result set, hit an empty/short
            # page, or a page adds nothing new (runaway guard)
            if new_on_page == 0:
                break
            if len(seen) >= (data.get("count") or 0):
                break
            if len(results) < params["PageSize"]:
                break
            time.sleep(0.5)

    @staticmethod
    def _keywords(c: SearchCriteria) -> str:
        from . import brands, calibers
        parts = [brands.search_term(c.manufacturer), c.keyword,
                 calibers.search_term(c.caliber)]
        return " ".join(p for p in parts if p).strip() or "firearm"

    @staticmethod
    def _to_listing(item: dict) -> Listing | None:
        item_id = item.get("itemID")
        if not item_id:
            return None
        buy_now = _num(item.get("buyPrice"))
        cur_price = _num(item.get("price"))  # current bid for auctions

        # Keep firm prices and bids strictly separate: a live auction's current
        # bid is a lower bound on an in-progress process, not an asking price,
        # and must never enter price analytics as one.
        if item.get("isFixedPrice"):
            listing_type = "fixed"
            price = buy_now if buy_now is not None else cur_price
            current_bid = bid_count = ends_at = None
        else:
            listing_type = "auction_buynow" if buy_now else "auction"
            price = buy_now  # None for pure auctions
            current_bid = cur_price
            bid_count = item.get("bidCount")
            ends_at = _parse_ts(item.get("endingDate"))

        cond = (item.get("condition") or {})
        cond_txt = cond.get("value", "") if isinstance(cond, dict) else str(cond)
        cond_norm = "new" if "new" in cond_txt.lower() else ("used" if cond_txt else "")
        return Listing(
            site="gunbroker",
            url=f"https://www.gunbroker.com/item/{item_id}",
            title=item.get("title") or "Untitled",
            manufacturer=item.get("manufacturer") or "",
            caliber=item.get("caliber") or "",
            condition=cond_norm,
            condition_grade=cond_norm,
            listing_type=listing_type,
            price=price,
            current_bid=current_bid,
            bid_count=bid_count,
            ends_at=ends_at,
            posted_at=_parse_ts(item.get("startingDate")),
            image=item.get("thumbnailURL") or "",
            extra={"itemID": item_id},  # the close poller re-fetches by itemID
        )


def _num(v) -> float | None:
    try:
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


def _parse_ts(s) -> float | None:
    """GunBroker ISO-8601 timestamp ('2026-07-21T02:15:00Z') -> epoch."""
    if not s:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None
