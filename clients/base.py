"""Base class + per-vertical registries for site clients.

To add a new site: subclass SiteClient, set `vertical` (guns|parts|ammo),
implement search(), and decorate with @register. The app picks it up
automatically under REGISTRIES[vertical].
"""
from abc import ABC, abstractmethod
from typing import Callable

from models import SearchCriteria, Listing

# One registry per vertical: REGISTRIES[vertical][site_name] = client class.
REGISTRIES: dict[str, dict[str, type["SiteClient"]]] = {
    "guns": {}, "parts": {}, "ammo": {},
}


def register(cls):
    REGISTRIES.setdefault(cls.vertical, {})[cls.name] = cls
    return cls


class SiteClient(ABC):
    vertical: str = "guns"      # guns | parts | ammo — which engine this feeds
    name: str = "base"          # short id, used in API/DB
    label: str = "Base"         # display name
    homepage: str = ""
    # A query this site is known to have plenty of results for; used by
    # health_check() to prove the client still parses the site correctly.
    canary_keyword: str = "glock"

    @abstractmethod
    def search(self, criteria: SearchCriteria, emit: Callable[[Listing], None]):
        """Run the search. Call emit(listing) for every match as soon as it is
        found so the UI populates live. Raise ClientBlocked if the site refuses
        us, StructureError if the page no longer parses; any other exception
        is reported as a generic error."""

    def health_check(self) -> dict:
        """Run the canary search on one page and report whether this client
        can still read the site. Returns {status, message, found, elapsed_ms}.
        status: ok | degraded | schema_changed | blocked | error."""
        import time as _time
        crit = SearchCriteria(keyword=self.canary_keyword,
                              vertical=self.vertical, max_pages=1)
        found = []
        t0 = _time.time()
        try:
            self.search(crit, found.append)
        except ClientBlocked as e:
            return self._health("blocked", str(e), len(found), t0)
        except StructureError as e:
            return self._health("schema_changed", str(e), len(found), t0)
        except Exception as e:
            return self._health("error", f"{type(e).__name__}: {e}", len(found), t0)
        if not found:
            return self._health(
                "degraded",
                f"Canary search '{self.canary_keyword}' parsed but returned 0 "
                "listings — the site may have changed in a way the client "
                "doesn't detect.", 0, t0)
        # sanity-check field population on what came back
        n = len(found)
        missing = [f for f, pct in {
            "price": sum(1 for l in found if l.price is not None) / n,
            "title": sum(1 for l in found if l.title and l.title != "Untitled") / n,
            "url": sum(1 for l in found if l.url) / n,
        }.items() if pct < 0.5]
        if missing:
            return self._health(
                "degraded",
                f"Listings parse but mostly lack {', '.join(missing)} — "
                "field mapping may be stale.", n, t0)
        return self._health("ok", "", n, t0)

    def _health(self, status, message, found, t0):
        import time as _time
        return {
            "site": self.name, "status": status, "message": message,
            "found": found, "elapsed_ms": int((_time.time() - t0) * 1000),
        }

    # ---- shared filter entry point ---------------------------------------

    @staticmethod
    def passes(criteria: SearchCriteria, listing: Listing) -> bool:
        """Universal filters (relevance/condition/price/age/manufacturer/stock)
        AND the active vertical's own predicates (guns: caliber/action/barrel/
        capacity; ammo: grain/bullet/cost-per-round; parts: category/fitment)."""
        from verticals import get
        v = get(getattr(criteria, "vertical", "guns"))
        return _passes_common(criteria, listing, v) and v.passes(criteria, listing)


class ClientBlocked(Exception):
    """Site actively refused us (captcha / 403 / missing API key)."""


class StructureError(Exception):
    """The page fetched fine but no longer matches the structure this client
    was written against — the site changed their markup/JSON and the client
    code needs updating. Always include what exactly went missing."""


def page_limit_reached(criteria: SearchCriteria, pages_done: int) -> bool:
    """True when the optional max_pages ceiling says stop. max_pages is None for
    real searches (exhaustive — clients loop until the SITE runs out), and set
    only by the health-check canary to fetch a single page. `pages_done` is how
    many pages have already been fetched."""
    return criteria.max_pages is not None and pages_done >= criteria.max_pages


def _passes_common(c: SearchCriteria, listing: Listing, vertical) -> bool:
    """The vertical-agnostic half of the filter. `vertical.passes` handles the
    engine-specific fields."""
    # Relevance to the active engine is ALWAYS enforced — no user toggle (§5.3).
    if not vertical.relevance(listing):
        return False
    if c.listing_type == "fixed" and listing.listing_type == "auction":
        return False  # auction_buynow still satisfies "fixed" (it has a firm price)
    if c.listing_type == "auction" and listing.listing_type not in (
            "auction", "auction_buynow"):
        return False
    # condition: new|used|reman|surplus — 'both'/'any'/'' means don't filter
    if c.condition and c.condition not in ("both", "any") and listing.condition:
        if listing.condition.lower() != c.condition.lower():
            return False
    # listing age: sites reporting a posted date get filtered on it; unknown
    # date is kept (never hide something we can't disprove)
    if c.max_age_days is not None and listing.posted_at is not None:
        import time
        if listing.posted_at < time.time() - c.max_age_days * 86400:
            return False
    if c.manufacturer:
        from . import brands
        if not brands.matches(c.manufacturer, listing.manufacturer, listing.title):
            return False
    # price range applies to the firm price, or the current bid for auctions
    effective = listing.price if listing.price is not None else listing.current_bid
    if effective is not None:
        if c.price_min is not None and effective < c.price_min:
            return False
        if c.price_max is not None and effective > c.price_max:
            return False
    if getattr(c, "in_stock_only", False):
        stock = (listing.attributes or {}).get("in_stock")
        if stock in (0, False, "0", "false", "no", "out of stock", "oos", "sold out"):
            return False
    return True
