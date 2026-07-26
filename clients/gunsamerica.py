"""GunsAmerica.com client (marketplace — new and used listings).

Primary path (2026-07): their Angular app talks to a public JSON search service
at `POST https://gunsamerica.com/listings/search` (apex host — the www subdomain
301-redirects it). It accepts every filter we care about *server-side*:
multi-word `keyword`, `manufacturer`, `caliber`, `condition` (0=new, 1=used),
`minPrice`/`maxPrice`, and `onlyGuns` ("true" drops ammo/accessories). It returns
up to 250 results per page (`numberPerPage`, capped there) and pages via
`pageNumber` up to `maxBrowsablePages` (~10,000 results). This replaces the old
approach of scraping a single server-rendered page.

Why the change: the Angular app only server-renders results for SINGLE-word
keywords into a `<script id="ng-state">` transfer-state blob, with no
pagination — so the old client sent one token, saw ~12 result groups, and
filtered everything else locally, missing most of the catalog. The JSON service
the app itself calls has none of those limits.

Cloudflare fronts the site and 403s Python's `requests` TLS fingerprint, so both
paths go through fetcher.fetch() (curl.exe). The item shape returned by the JSON
service is identical to the ng-state `results` items, so _to_listing() is shared.

Fallback path: the legacy ng-state scrape, used only if the JSON service fails
before we emit anything (so a backend change degrades instead of breaking).
"""
import json
import re
from typing import Callable
from urllib.parse import quote_plus

from fetcher import fetch, FetchError
from models import SearchCriteria, Listing
from .base import SiteClient, ClientBlocked, StructureError, register, page_limit_reached
from . import titleparse

BASE = "https://www.gunsamerica.com"
# API lives on the apex host; www.gunsamerica.com/listings/search 301s to it and
# curl -L would replay the POST as a GET, so target the apex directly.
API_SEARCH = "https://gunsamerica.com/listings/search"
PER_PAGE = 250  # server caps numberPerPage here regardless of what we ask
NG_STATE_RE = re.compile(
    r'<script id="ng-state" type="application/json">(.*?)</script>', re.S)

# Every ng-state result carries a numeric category `family`. Probed 2026-07 by
# cross-referencing per-item values against the familySummary facet names
# (keyword searches whose summary has one dominant family reveal each id):
# the map below is the full picture. '2' is the seller-chosen "Everything
# Else" bin, which in practice holds pouches/lights/barrels, so it counts as
# non-firearm. Firearm families encode the platform AND the action — this is
# the site's own categorization, so it fills action for the ~98% of items
# whose title never states it.
_NON_FIREARM_FAMILIES = {
    "2",   # everything else
    "19",  # ammunition
    "20",  # accessories
    "21",  # magazines
    "22",  # suppressors
    "24",  # optics & mounts
}

# family id -> (gun_type, action); '' where the family doesn't pin it down
_FAMILY_INFO = {
    "1":  ("muzzleloader", "muzzleloader"),   # blackPowderMuzzleloaders
    "3":  ("pistol", ""),                     # otherPistols
    "4":  ("pistol", "semi auto"),            # semiAutoPistols
    "5":  ("revolver", "revolver"),           # revolvers
    "6":  ("pistol", "single shot"),          # singleShotPistols
    "7":  ("rifle", "pump"),                  # pumpActionRifles
    "8":  ("rifle", "single shot"),           # singleShotRifles
    "9":  ("rifle", "semi auto"),             # semiAutoRifles
    "10": ("rifle", "bolt"),                  # boltActionRifles
    "11": ("rifle", "lever"),                 # leverActionRifles
    "12": ("rifle", ""),                      # otherRifles
    "13": ("shotgun", "pump"),                # pumpActionShotguns
    "14": ("shotgun", "semi auto"),           # semiAutoShotguns
    "15": ("shotgun", "side by side"),        # sideBySideShotguns
    "16": ("shotgun", ""),                    # otherShotguns
    "17": ("shotgun", "single shot"),         # singleShotShotguns
    "18": ("shotgun", "over/under"),          # overUnderShotguns
    "23": ("other", ""),                      # nfa
}


@register
class GunsAmericaClient(SiteClient):
    name = "gunsamerica"
    label = "GunsAmerica"
    homepage = BASE

    def search(self, criteria: SearchCriteria, emit: Callable[[Listing], None]):
        emitted = 0

        def counted(listing):
            nonlocal emitted
            emitted += 1
            emit(listing)

        try:
            self._search_api(criteria, counted)
            return
        except (ClientBlocked, StructureError):
            if emitted:
                raise  # partial results already streamed; don't double-emit
            # fall through to the legacy scrape below
        except Exception:
            if emitted:
                raise

        self._search_scrape(criteria, emit)

    # ---- primary path: JSON search service --------------------------------

    def _search_api(self, criteria: SearchCriteria,
                    emit: Callable[[Listing], None]):
        import itertools
        import time
        body = self._build_request(criteria)
        seen: set[str] = set()

        for page in itertools.count(1):
            if page_limit_reached(criteria, page - 1):
                break
            body["pageNumber"] = page
            data = self._post(body)
            results = data.get("results")
            if page == 1:
                self._check_response_shape(data)
            if not results:
                break

            new_on_page = 0
            for r in results:
                # duplicate feed items = same product from other sellers; expand
                # so price filtering sees every offer
                for item in [r] + (r.get("duplicateFeedItems") or []):
                    key = str(item.get("id") or item.get("listingURL") or "")
                    if key and key in seen:
                        continue
                    if key:
                        seen.add(key)
                    new_on_page += 1
                    listing = self._to_listing(item, criteria)
                    if listing and self.passes(criteria, listing):
                        emit(listing)

            # stop when the whole set is pulled, the site's browsable-page cap is
            # hit (their 10k ceiling, not ours), or a page adds nothing new
            total = data.get("totalResults") or 0
            max_browsable = data.get("maxBrowsablePages") or 0
            if new_on_page == 0:
                break
            if len(seen) >= total:
                break
            if max_browsable and page >= max_browsable:
                break
            time.sleep(0.4)  # be polite

    def _build_request(self, c: SearchCriteria) -> dict:
        from . import brands, calibers
        req: dict = {"numberPerPage": PER_PAGE}
        if c.keyword:
            req["keyword"] = c.keyword
        if c.manufacturer:
            req["manufacturer"] = brands.search_term(c.manufacturer)
        if c.caliber:
            req["caliber"] = calibers.search_term(c.caliber)
        if c.condition == "new":
            req["condition"] = 0
        elif c.condition == "used":
            req["condition"] = 1
        if c.price_min is not None:
            req["minPrice"] = c.price_min
        if c.price_max is not None:
            req["maxPrice"] = c.price_max
        if c.hide_accessories:
            req["onlyGuns"] = "true"
        return req

    @staticmethod
    def _post(body: dict) -> dict:
        try:
            raw = fetch(API_SEARCH, timeout=30, data=json.dumps(body),
                        headers={"Content-Type": "application/json",
                                 "Accept": "application/json"})
        except FetchError as e:
            if e.status in (403, 429):
                raise ClientBlocked(
                    f"gunsamerica.com search API returned HTTP {e.status}") from e
            raise StructureError(
                f"gunsamerica.com search API returned HTTP {e.status}; "
                "their endpoint may have moved.") from e
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise StructureError(
                "gunsamerica.com /listings/search no longer returns JSON "
                f"({e}); the search API changed.") from e

    @staticmethod
    def _check_response_shape(data: dict):
        if "results" not in data or "totalResults" not in data:
            raise StructureError(
                "gunsamerica.com search API response lost 'results'/"
                f"'totalResults' (has: {', '.join(list(data)[:8])}); "
                "update _search_api().")
        results = data.get("results") or []
        if results and "listingURL" not in results[0]:
            raise StructureError(
                "gunsamerica.com search API items no longer carry 'listingURL'; "
                "update _to_listing().")

    # ---- fallback path: legacy ng-state scrape ----------------------------

    def _search_scrape(self, criteria: SearchCriteria,
                       emit: Callable[[Listing], None]):
        token = self._primary_token(criteria)
        url = f"{BASE}/search?keyword={quote_plus(token)}"
        try:
            body = fetch(url, timeout=30)
        except FetchError as e:
            if e.status in (403, 429):
                raise ClientBlocked(f"gunsamerica.com returned HTTP {e.status}") from e
            raise

        results = self._extract_results(body)
        # The SSR page only carries one page; the rest of the criteria are
        # applied locally. Expand duplicate feed items (same product, other
        # sellers) so price filtering sees every offer.
        for r in results:
            for item in [r] + (r.get("duplicateFeedItems") or []):
                listing = self._to_listing(item, criteria)
                if listing and self.passes(criteria, listing):
                    emit(listing)

    @staticmethod
    def _primary_token(c: SearchCriteria) -> str:
        # only single-word queries get server-rendered (see module docstring),
        # so always send one token; passes() filters the rest locally
        if c.manufacturer:
            from . import brands
            return brands.search_term(c.manufacturer).split()[0]
        if c.keyword:
            return c.keyword.split()[0]
        if c.caliber:
            from . import calibers
            return calibers.search_term(c.caliber).split()[0]
        return "firearm"

    @staticmethod
    def _extract_results(body: str) -> list[dict]:
        m = NG_STATE_RE.search(body)
        if not m:
            raise StructureError(
                "gunsamerica.com no longer embeds an 'ng-state' JSON script — "
                "their frontend changed; clients/gunsamerica.py needs updating.")
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            raise StructureError(
                f"gunsamerica.com ng-state is no longer valid JSON ({e}).") from e

        # The results live under a randomly-named cache key, so walk the tree.
        found = None

        def walk(obj):
            nonlocal found
            if found is not None:
                return
            if isinstance(obj, dict):
                v = obj.get("results")
                if isinstance(v, list) and v and isinstance(v[0], dict) \
                        and "listingURL" in v[0]:
                    found = obj
                    return
                for vv in obj.values():
                    walk(vv)
            elif isinstance(obj, list):
                for vv in obj:
                    walk(vv)

        walk(data)
        if found is None:
            if '"totalResults":0' in m.group(1) or '"results":[]' in m.group(1):
                return []
            raise StructureError(
                "gunsamerica.com ng-state parsed but contains no recognizable "
                "results list (objects with 'listingURL'); their search state "
                "shape changed — update _extract_results().")
        return found["results"]

    @staticmethod
    def _to_listing(item: dict, criteria: SearchCriteria) -> Listing | None:
        link = item.get("listingURL") or ""
        if not link:
            return None
        title = (item.get("title") or "").strip() or "Untitled"
        desc = item.get("description") or ""
        # If the user typed a multi-word keyword we could only send one token
        # to the site, so enforce the full phrase's words locally.
        if criteria.keyword:
            hay = f"{title} {desc}".lower()
            if not all(w in hay for w in criteria.keyword.lower().split()):
                return None
        display_price = item.get("displayPrice")
        try:
            display_price = float(display_price) if display_price is not None else None
        except (TypeError, ValueError):
            display_price = None

        # Probed 2026-07: server-rendered results carry listingType=0 (fixed
        # price). Auctions don't appear in the SSR blob, but map any nonzero
        # type defensively: displayPrice would be the current bid, not an ask.
        if item.get("listingType", 0) == 0:
            listing_type, price, current_bid, ends_at = "fixed", display_price, None, None
        else:
            listing_type, price, current_bid = "auction", None, display_price
            ends_at = _parse_ts(item.get("listingEndDate"))

        cond = {0: "new", 1: "used"}.get(item.get("condition"), "")
        blob = f"{title} {desc}"
        family = str(item.get("family") or "")
        kind = "" if not family else \
            ("accessory" if family in _NON_FIREARM_FAMILIES else "firearm")
        fam_type, fam_action = _FAMILY_INFO.get(family, ("", ""))
        # thumbnailPicture is a bare all-zeros GUID (not a URL) when the
        # listing has no photos (imageCount 0), so require a real URL
        thumb = str(item.get("thumbnailPicture") or "")
        return Listing(
            site="gunsamerica",
            url=BASE + link if link.startswith("/") else link,
            title=title,
            image=thumb if thumb.startswith("http") else "",
            manufacturer=item.get("manufacturer") or "",
            model=item.get("model") or "",
            caliber=item.get("caliber") or titleparse.caliber(blob),
            action=fam_action,
            gun_type=fam_type,
            barrel_length=titleparse.barrel_length(blob),
            capacity=titleparse.capacity(blob),
            condition=cond,
            condition_grade=cond,
            upc=str(item.get("upc") or ""),
            listing_type=listing_type,
            price=price,
            current_bid=current_bid,
            ends_at=ends_at,
            posted_at=_parse_ts(item.get("listingStartDate")),
            extra={
                "seller": item.get("displayName"),
                "quantity": item.get("quantity"),
                "family": family,
                "kind": kind,
            },
        )


def _parse_ts(s) -> float | None:
    """'2027-07-17T12:49:10.185925Z' -> epoch."""
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
