"""Guns.com client.

Primary path (2026-07): the whole site is one public Algolia index. Every page
embeds the search-only credentials in a `<header-search app-id=... app-key=...
index-name=...>` tag; we scrape those once per process and POST straight to
`<app-id>-dsn.algolia.net` with real server-side facet filters (condition,
category, caliber). This sees the site's FULL inventory — the old page-scrape
sampled 5 pages of keyword results sorted by promoted-listing weight and
missed ~98% of e.g. used .30-06 rifles (11 found vs 580 live).

Algolia quirks handled here:
- caliber facet values are messy dealer text ('.30-06 SPRG', '3006 SPRI'...),
  so we enumerate the facet and pick values via calibers.canonical();
- a single query can only page through 1,000 hits (paginationLimitedTo), so
  result sets bigger than that are split by filterPrice bands (bisection);
- plain `requests` works against algolia.net (no Akamai there — the curl-TLS
  workaround in fetcher.py is only needed for guns.com's own pages).

Fallback path: the legacy scrape of the HTML-escaped JSON in
`<product-listing-page :initial-data="...">` on /search pages (24 hits/page,
keyword-only, page=N). Used only if the Algolia path fails before emitting
anything, so a rotated key or layout change degrades instead of breaking.
"""
import html
import itertools
import json
import logging
import re
import time
from typing import Callable
from urllib.parse import quote_plus, urlencode

import requests

from fetcher import fetch, FetchError
from models import SearchCriteria, Listing
from .base import SiteClient, ClientBlocked, StructureError, register, page_limit_reached

log = logging.getLogger("gun_scout")

DATA_RE = re.compile(r':initial-data="([^"]*)"')
HEADER_SEARCH_RE = re.compile(r"<header-search\s[^>]*>", re.S)
BASE = "https://www.guns.com"

# Search-only public key scraped 2026-07-19; used when the live scrape of
# <header-search> fails. If Algolia 403s this we re-scrape before giving up.
LAST_KNOWN_CREDS = {
    "app_id": "INPWEIBRO4",
    "app_key": "db12d54756813d49727dbe65d9d7de4d",
    "index": "items",
}

# product.category values that are guns (live 2026-07: HANDGUNS / RIFLES /
# SHOTGUNS); anything with a gear-ish word is not. Unknown categories fall
# through to the title heuristic.
_FIREARM_CATS = {
    "RIFLES", "PISTOLS", "HANDGUNS", "REVOLVERS", "SHOTGUNS", "FIREARMS",
    "MUZZLELOADERS", "BLACK POWDER", "DERRINGERS", "NFA",
}
_ACCESSORY_CAT_WORDS = (
    "AMMO", "AMMUNITION", "ACCESSOR", "OPTIC", "SCOPE", "MAGAZINE", "PART",
    "GEAR", "KNIFE", "KNIVES", "SUPPRESSOR", "SILENCER", "AIR GUN", "AIRSOFT",
    "RELOAD", "HOLSTER", "APPAREL", "CLEANING", "TOOL",
)


def _category_kind(category: str) -> str:
    cat = (category or "").upper().strip()
    if not cat:
        return ""
    if cat in _FIREARM_CATS:
        return "firearm"
    if any(w in cat for w in _ACCESSORY_CAT_WORDS):
        return "accessory"
    return ""


_CATEGORY_GUN_TYPE = {
    "RIFLES": "rifle", "PISTOLS": "pistol", "HANDGUNS": "pistol",
    "REVOLVERS": "revolver", "SHOTGUNS": "shotgun", "DERRINGERS": "pistol",
    "MUZZLELOADERS": "muzzleloader", "BLACK POWDER": "muzzleloader",
    "NFA": "other",
}


@register
class GunsComClient(SiteClient):
    name = "gunscom"
    label = "Guns.com"
    homepage = BASE

    _creds: dict | None = None  # process-wide cache of scraped Algolia creds

    def search(self, criteria: SearchCriteria, emit: Callable[[Listing], None]):
        emitted = 0

        def counted(listing):
            nonlocal emitted
            emitted += 1
            emit(listing)

        try:
            self._search_algolia(criteria, counted)
            return
        except Exception as e:
            if emitted:
                raise  # partial results already streamed; don't double-emit
            log.warning("guns.com Algolia path failed (%s: %s); falling back "
                        "to legacy page scrape", type(e).__name__, e)
        self._search_scrape(criteria, emit)

    # ---- primary path: direct Algolia queries ----------------------------

    def _search_algolia(self, criteria: SearchCriteria,
                        emit: Callable[[Listing], None]):
        query_text = self._build_query_text(criteria)

        filters: list[list[str]] = []
        if criteria.condition in ("new", "used"):
            filters.append([f"condition:{criteria.condition}"])
        if criteria.hide_accessories:
            cats = self._facet_values("product.category", query_text, filters)
            keep = [c for c in cats if _category_kind(c) != "accessory"]
            if keep:
                filters.append([f"product.category:{c}" for c in keep])

        if criteria.caliber:
            cal_values = self._matching_caliber_facets(
                criteria.caliber, query_text, filters)
            if cal_values:
                filters.append(
                    [f"product.specs.Caliber:{v}" for v in cal_values])
            else:
                # caliber not in the facet vocabulary -> old text-search route
                from . import calibers
                term = calibers.search_term(criteria.caliber)
                query_text = f"{query_text} {term}".strip()

        numeric: list[str] = []
        if criteria.price_min is not None:
            numeric.append(f"filterPrice>={criteria.price_min:g}")
        if criteria.price_max is not None:
            numeric.append(f"filterPrice<={criteria.price_max:g}")

        # 1,000 is Algolia's per-query hit ceiling for this index.
        res = self._query(query_text, filters, numeric, hits_per_page=1000)
        self._check_response_shape(res)
        seen: set[str] = set()
        self._emit_hits(res.get("hits") or [], criteria, emit, seen)

        total = res.get("nbHits") or 0
        # A page ceiling (health-check canary) means one query is enough. For a
        # real search, if there are >1,000 matches the index won't page past
        # 1,000, so walk price bands to pull the ENTIRE result set.
        if criteria.max_pages is None and total > len(seen):
            self._band_walk(query_text, filters, numeric, criteria, emit, seen)

    def _band_walk(self, query_text, filters, numeric, criteria, emit,
                   seen: set):
        """Fetch a >1,000-hit result set completely by bisecting filterPrice
        ranges until every band fits in one 1,000-hit response. Runs until every
        band is exhausted — no result cap."""
        stack: list[tuple[float, float | None]] = [(0.0, None)]
        while stack:
            lo, hi = stack.pop()
            band = numeric + [f"filterPrice>={lo:g}"]
            if hi is not None:
                band.append(f"filterPrice<{hi:g}")
            res = self._query(query_text, filters, band, hits_per_page=1000)
            n = res.get("nbHits") or 0
            if not n:
                continue
            if n <= 1000:
                self._emit_hits(res.get("hits") or [], criteria, emit, seen)
            elif hi is not None and hi - lo < 0.01:
                # a single price point with >1,000 listings can't be split
                # further and the index caps a query at 1,000 — take what we can
                # and surface the shortfall rather than silently dropping it
                self._emit_hits(res.get("hits") or [], criteria, emit, seen)
                log.warning(
                    "guns.com: %d listings all priced $%.2f under this query "
                    "exceed Algolia's 1,000/query cap; %d unreachable.",
                    n, lo, n - 1000)
            else:
                # unbounded top band doubles upward; bounded band bisects
                mid = (lo + hi) / 2 if hi is not None else max(lo * 2, 512.0)
                stack.append((mid, hi))
                stack.append((lo, mid))
            time.sleep(0.2)  # be polite

    def _emit_hits(self, hits: list[dict], criteria, emit, seen: set):
        for hit in hits:
            obj_id = str(hit.get("objectID") or hit.get("listingId") or "")
            if obj_id in seen:
                continue
            seen.add(obj_id)
            listing = self._to_listing(hit)
            if listing and self.passes(criteria, listing):
                emit(listing)

    @staticmethod
    def _build_query_text(c: SearchCriteria) -> str:
        """Free-text part of the query: keyword + manufacturer. Caliber and
        condition are handled as facet filters, NOT text — text spellings
        lose recall ('30-06 springfield' finds 43 of 1,079 '30-06' hits)."""
        from . import brands
        parts = [c.keyword]
        if c.manufacturer and c.manufacturer.lower() not in c.keyword.lower():
            parts.insert(0, brands.search_term(c.manufacturer))
        return " ".join(p for p in parts if p).strip()

    def _matching_caliber_facets(self, wanted: str, query_text: str,
                                 filters: list) -> list[str]:
        """The index's caliber values are dealer-entered ('.30-06 SPRG',
        '3006 SPRI', '30-06 SPRING'...); collect every spelling that resolves
        to the same canonical caliber as the request."""
        from . import calibers
        from .calibers import _norm as _norm_cal
        values = self._facet_values("product.specs.Caliber", query_text, filters)
        want_canon = calibers.canonical(wanted)
        out = []
        for v in values:
            if want_canon:
                if calibers.canonical(v) == want_canon:
                    out.append(v)
            else:
                nv, nw = _norm_cal(v), _norm_cal(wanted)
                if nw and (nw in nv or nv in nw):
                    out.append(v)
        return out

    def _facet_values(self, facet: str, query_text: str,
                      filters: list) -> list[str]:
        res = self._query(query_text, filters, [], hits_per_page=0,
                          facets=[facet])
        return list((res.get("facets") or {}).get(facet) or {})

    def _query(self, query_text: str, facet_filters: list,
               numeric_filters: list, hits_per_page: int,
               facets: list | None = None, retry_auth: bool = True) -> dict:
        creds = self._get_creds()
        params = {"query": query_text, "hitsPerPage": hits_per_page}
        if facet_filters:
            params["facetFilters"] = json.dumps(facet_filters)
        if numeric_filters:
            params["numericFilters"] = json.dumps(numeric_filters)
        if facets:
            params["facets"] = json.dumps(facets)
            params["maxValuesPerFacet"] = 1000

        url = (f"https://{creds['app_id'].lower()}-dsn.algolia.net"
               f"/1/indexes/{creds['index']}/query")
        resp = requests.post(
            url,
            headers={"x-algolia-application-id": creds["app_id"],
                     "x-algolia-api-key": creds["app_key"],
                     "content-type": "application/json"},
            data=json.dumps({"params": urlencode(params)}),
            timeout=25,
        )
        if resp.status_code in (401, 403):
            if retry_auth:  # key may have rotated -> re-scrape it once
                self._get_creds(force=True)
                return self._query(query_text, facet_filters, numeric_filters,
                                   hits_per_page, facets, retry_auth=False)
            raise ClientBlocked(
                f"guns.com Algolia rejected our search key (HTTP "
                f"{resp.status_code}) even after re-scraping it.")
        if resp.status_code >= 400:
            raise StructureError(
                f"guns.com Algolia query returned HTTP {resp.status_code}: "
                f"{resp.text[:200]} — their index setup may have changed.")
        try:
            return resp.json()
        except ValueError as e:
            raise StructureError(
                f"guns.com Algolia response is not JSON ({e}).") from e

    @classmethod
    def _get_creds(cls, force: bool = False) -> dict:
        if cls._creds and not force:
            return cls._creds
        attrs = {}
        try:
            body = fetch(BASE, timeout=25)
            m = HEADER_SEARCH_RE.search(body)
            if m:
                attrs = dict(re.findall(r'([a-z-]+)="([^"]*)"', m.group()))
        except FetchError:
            pass  # site page blocked; Algolia itself may still answer
        if attrs.get("app-id") and attrs.get("app-key"):
            cls._creds = {"app_id": attrs["app-id"],
                          "app_key": attrs["app-key"],
                          "index": attrs.get("index-name") or "items"}
        elif not cls._creds:
            cls._creds = dict(LAST_KNOWN_CREDS)
        return cls._creds

    @staticmethod
    def _check_response_shape(res: dict):
        if "hits" not in res:
            raise StructureError(
                "guns.com Algolia response has no 'hits' key (has: "
                f"{', '.join(list(res)[:8])}); the index API changed.")
        hits = res["hits"] or []
        if hits:
            probe = hits[0]
            missing = [k for k in ("link", "product", "price", "condition")
                       if k not in probe]
            if missing:
                raise StructureError(
                    "guns.com Algolia hits lost expected field(s): "
                    f"{', '.join(missing)}; update _to_listing().")

    # ---- fallback path: legacy /search page scrape ------------------------

    def _search_scrape(self, criteria: SearchCriteria,
                       emit: Callable[[Listing], None]):
        keyword = self._build_keyword(criteria)

        seen_ids: set[str] = set()
        for page in itertools.count(1):
            if page_limit_reached(criteria, page - 1):
                break
            url = f"{BASE}/search?keyword={quote_plus(keyword)}&page={page}"
            try:
                body = fetch(url, timeout=25)
            except FetchError as e:
                if e.status in (403, 429):
                    raise ClientBlocked(f"guns.com returned HTTP {e.status}") from e
                raise

            hits = self._extract_hits(body, first_page=(page == 1))
            if not hits:
                break

            new_on_page = 0
            for hit in hits:
                obj_id = str(hit.get("objectID") or hit.get("listingId") or "")
                if obj_id in seen_ids:
                    continue
                seen_ids.add(obj_id)
                new_on_page += 1
                listing = self._to_listing(hit)
                if listing and self.passes(criteria, listing):
                    emit(listing)

            if new_on_page == 0 or len(hits) < 24:
                break  # last page (site repeats results past the end)
            time.sleep(0.8)  # be polite

    @staticmethod
    def _build_keyword(c: SearchCriteria) -> str:
        from . import brands, calibers
        parts = [c.keyword]
        if c.manufacturer and c.manufacturer.lower() not in c.keyword.lower():
            parts.insert(0, brands.search_term(c.manufacturer))
        if c.caliber:
            parts.append(calibers.search_term(c.caliber))
        if not any(parts):
            parts = ["firearm"]
        return " ".join(p for p in parts if p).strip()

    @staticmethod
    def _extract_hits(page_html: str, first_page: bool = False) -> list[dict]:
        """Parse the embedded results JSON. On the first page, anything that
        doesn't look like the structure we were written against raises
        StructureError so the UI can flag that this client needs updating."""
        m = DATA_RE.search(page_html)
        if not m:
            if first_page:
                raise StructureError(
                    "guns.com no longer embeds ':initial-data' on its search "
                    "page — they changed their frontend; clients/gunscom.py "
                    "needs a rewrite of _extract_hits().")
            return []
        try:
            data = json.loads(html.unescape(m.group(1)))
        except json.JSONDecodeError as e:
            raise StructureError(
                f"guns.com ':initial-data' attribute exists but is no longer "
                f"valid JSON ({e}); the embedding format changed.") from e
        if "firearms" not in data:
            raise StructureError(
                "guns.com results JSON no longer has a 'firearms' key "
                f"(now has: {', '.join(list(data)[:8])}); update _extract_hits().")
        hits = data["firearms"] or []
        if first_page and hits:
            probe = hits[0]
            missing = [k for k in ("link", "product", "price", "condition")
                       if k not in probe]
            if missing:
                raise StructureError(
                    "guns.com listing objects lost expected field(s): "
                    f"{', '.join(missing)}; update _to_listing().")
        return hits

    # ---- shared hit -> Listing mapping ------------------------------------

    @staticmethod
    def _to_listing(hit: dict) -> Listing | None:
        product = hit.get("product") or {}
        specs = product.get("specs") or {}
        float_specs = product.get("floatSpecs") or {}
        link = hit.get("link") or product.get("link") or ""
        if not link:
            return None

        price = None
        try:
            price = float(hit.get("filterPrice") or hit.get("price"))
        except (TypeError, ValueError):
            pass

        barrel = float_specs.get("Barrel Length")
        if barrel is None:
            m = re.search(r"[\d.]+", str(specs.get("Barrel Length") or ""))
            if m:
                try:
                    barrel = float(m.group())
                except ValueError:
                    barrel = None

        # New listings carry a clean short title in `description`; used listings
        # put the seller's multi-line sales pitch there, so fall back to the
        # catalog product name.
        desc = (hit.get("description") or "").strip()
        title = desc if desc and "\n" not in desc and len(desc) <= 90 else ""
        title = title or product.get("name") or desc.split("\n")[0][:90] or "Untitled"

        condition = (hit.get("condition") or "").lower()
        # used listings carry a grade like 'excellent' / 'very-good' / 'good'
        grade = "new" if condition == "new" else \
            (hit.get("sub_condition") or "").lower().strip().replace(" ", "-")

        return Listing(
            site="gunscom",
            url=BASE + link if link.startswith("/") else link,
            title=title,
            manufacturer=product.get("manufacturer") or "",
            model=product.get("model") or "",
            caliber=specs.get("Caliber") or "",
            action=specs.get("Action") or "",
            gun_type=_CATEGORY_GUN_TYPE.get(
                (product.get("category") or "").upper().strip(), ""),
            barrel_length=barrel,
            capacity=str(specs.get("Capacity") or ""),
            condition=condition,
            condition_grade=grade,
            listing_type="fixed",
            price=price,
            posted_at=_parse_created(hit.get("createdDate")),
            image=hit.get("img") or product.get("img") or "",
            extra={
                "availability": hit.get("availability"),
                "category": product.get("category"),
                "kind": _category_kind(product.get("category")),
            },
        )


def _parse_created(s) -> float | None:
    """Hits carry a date-only 'createdDate' like '2025-11-10' -> epoch (UTC)."""
    if not s:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.strptime(str(s)[:10], "%Y-%m-%d") \
            .replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
