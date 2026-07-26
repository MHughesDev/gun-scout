"""GunMade.com client (aggregator — live inventory search over their ~4,000
store network, so one client covers thousands of small dealers).

Their /search/ page is a SvelteKit app; the data route behind it —
`GET /search/__data.json?<params>` — answers to plain curl with the raw
Elasticsearch response embedded in SvelteKit's devalue serialization (a flat
array where index 0 is the root object and every dict/list value is an index
into the same array; shared refs and cycles are legal, hence the memo in
_solve()).

Server-side params (probed 2026-07):
- keyword, brand, minPrice/maxPrice, page (1-based)
- category: exact strings ('Pistol', 'Rifle', 'Shotgun', 'Black Powder', ...)
- caliber: EXACT facet-bucket string ('30-06 Springfield' works,
  '.30-06 Springfield' with the dot returns 0) — the vocabulary is enumerated
  from elastic_product_attribute.caliber.buckets and matched via
  calibers.canonical(), same trick as gun.deals' dropdown scrape
- priceSort=price: server-side price-ascending sort. This matters: without it
  deep pages overlap ~50% between requests (ES score ties across replicas);
  with it pages are disjoint and stable.
- NOT honored: condition, department, any page-size param (100 store hits/page
  fixed) — condition is filtered client-side by base.passes().

Pagination clamps at ~page 99 (Elasticsearch's 10k result window; page 99 and
101 return identical content). Past it we restart the walk with a minPrice
cursor at the highest price seen — the monotone cousin of guns.com's
filterPrice band bisection.

Each hit's `_source` carries title/price/manufacturer/caliber/condition/
category/attributes{model,subcategory=action}/upc/in_stock/images/domain_name
plus BOTH a gunmade /go/ redirect (`url`) and the store's direct product URL
(`originalUrl`) — we key listings on originalUrl so the same listing dedups
against any future direct client for that store. Up to 3 pseudo-hits per page
are consolidated marketplace groups (__consolidated_marketplace =
gunsamerica/gunbroker/armslist); skipped — we index GunsAmerica/GunBroker
directly.

Armslist (private-seller classifieds) has no direct client, so we DO surface it:
every payload carries an `armslistListings` node of up to 200 query-relevant
posts (title/price/url/image/listedAgo), harvested for free from page 1 of each
walk (it doesn't paginate — a fixed top-200 relevance cap). Those emit as
site=gunmade with extra.source='armslist'.

Bandwidth: the payload is ~3.7MB decompressed but the fetcher negotiates Brotli
(~40% smaller on the wire than gzip), and we solve only the handful of devalue
keys we use — the ~2.7MB `blogs` node and the affiliate/local/marketplace
sub-searches are never parsed.
"""
import itertools
import json
import logging
import re
import time
from typing import Callable
from urllib.parse import urlencode

from fetcher import fetch, FetchError
from models import SearchCriteria, Listing
from .base import SiteClient, ClientBlocked, StructureError, register, page_limit_reached
from . import titleparse

log = logging.getLogger("gun_scout")

BASE = "https://www.gunmade.com"
DATA_URL = BASE + "/search/__data.json"
PER_PAGE = 100        # server-fixed store hits per page; no size param honored
DEPTH_WALL_PAGE = 98  # ES 10k window: pages clamp at ~99-100; cursor before it
# The only devalue root keys we read — everything else (blogs, affiliate/local
# sub-searches, gunbroker/gunsamerica bundles) is left unparsed. See _fetch_root.
_WANT_KEYS = ("elastic_product", "elastic_product_attribute", "armslistListings")

# Their category vocabulary is flat; these four are the firearm categories
# (revolvers live under Pistol, muzzleloaders under Black Powder). Guns-only
# searches loop these server-side to skip the accessory bulk — but unlike the
# store-curated sites the scope does NOT bless kind='firearm' (see
# _structured_kind below: dealer feeds put $3 bearings in Pistol).
_GUN_CATEGORIES = [
    ("Pistol", "pistol"),
    ("Rifle", "rifle"),
    ("Shotgun", "shotgun"),
    ("Black Powder", "muzzleloader"),
]

# Their category/department values are DEALER-FED and messy in one direction:
# $3 bearings and Trijicon sights sit inside the Pistol category (department
# 'Glock Oem Parts' / even 'Firearms'), but real guns almost never land in the
# accessory buckets. So structured data only ever claims 'accessory' — a hit
# in a gun category still goes through the title heuristic (kind '') instead
# of being blessed as kind='firearm' like the store-curated sites.
_ACCESSORY_CATEGORIES = {
    "Holster", "Part", "Parts", "Magazine", "Optic", "Accessory", "AR Part",
    "Reloading", "Suppressor", "Ammunition", "Apparel", "Books", "Stun Guns",
    "Pepper Ball Guns", "Airguns", "Knives", "Gear",
}
_ACCESSORY_DEPT_WORDS = (
    "PART", "ACCESSOR", "OPTIC", "SIGHT", "AMMO", "AMMUNITION", "GEAR",
    "STORAGE", "KNIFE", "KNIVES", "APPAREL", "BOOK", "RELOAD", "HOLSTER",
    "MAGAZINE", "OEM", "CLEANING", "TOOL", "SAFE",
)
_CATEGORY_GUN_TYPE = dict(_GUN_CATEGORIES)


def _structured_kind(category: str, department: str) -> str:
    if category in _ACCESSORY_CATEGORIES:
        return "accessory"
    dept = (department or "").upper()
    if any(w in dept for w in _ACCESSORY_DEPT_WORDS):
        return "accessory"
    return ""

# devalue negative indices encode JS specials
_SPECIALS = {-1: None, -2: None, -3: float("nan"),
             -4: float("inf"), -5: float("-inf"), -6: -0.0}


@register
class GunMadeClient(SiteClient):
    name = "gunmade"
    label = "GunMade"
    homepage = BASE

    def search(self, criteria: SearchCriteria, emit: Callable[[Listing], None]):
        from . import brands, calibers
        text = " ".join(p for p in (brands.search_term(criteria.manufacturer),
                                    criteria.keyword) if p)
        base_params: dict = {"priceSort": "price"}
        if text:
            base_params["keyword"] = text
        if criteria.price_min is not None:
            base_params["minPrice"] = f"{criteria.price_min:g}"
        if criteria.price_max is not None:
            base_params["maxPrice"] = f"{criteria.price_max:g}"

        # caliber -> the site's exact facet spellings ('30-06 Springfield');
        # no facet match -> best-recall text term instead (gunscom fallback)
        cal_values: list = [None]
        if criteria.caliber:
            matched = self._matching_caliber_facets(criteria.caliber, base_params)
            if matched:
                cal_values = matched
            else:
                term = calibers.search_term(criteria.caliber)
                base_params["keyword"] = f"{text} {term}".strip()

        seen: set[str] = set()
        pages = [0]  # shared across walks so the health canary stays 1 fetch
        # Armslist private-seller posts: one dedicated keyword fetch (the
        # category+caliber facets that scope the store walk zero out the
        # armslist sub-search, but it responds well to a keyword). Gated to
        # real searches — the 1-page health canary skips it. Best-effort.
        if criteria.max_pages is None:
            self._harvest_armslist(criteria, text, emit, seen)
        cats = _GUN_CATEGORIES if criteria.hide_accessories else [("", "")]
        for cat, cat_gun_type in cats:
            for cal in cal_values:
                params = dict(base_params)
                if cat:
                    params["category"] = cat
                if cal:
                    params["caliber"] = cal
                self._walk(params, criteria, emit, seen, cat, cat_gun_type, pages)

    # ---- page walk with the depth-wall price cursor ------------------------

    def _walk(self, params: dict, criteria, emit, seen: set,
              category: str, cat_gun_type: str, pages: list):
        """Walk page=1.. for one (category, caliber) combo. priceSort=price
        keeps pages disjoint; a short page (<100 store hits) is the site's own
        end-of-results signal. At the ES depth wall we restart with
        minPrice=<highest price seen> — inclusive, so the boundary price group
        is re-fetched and deduped rather than skipped."""
        cursor = None
        while True:  # one iteration per cursor restart
            highest = None
            new_in_band = 0
            for page in itertools.count(1):
                if page_limit_reached(criteria, pages[0]):
                    return
                p = dict(params)
                if cursor is not None:
                    p["minPrice"] = f"{cursor:g}"
                if page > 1:
                    p["page"] = page
                root = self._fetch_root(p)
                pages[0] += 1
                hits = self._store_hits(root)
                for src in hits:
                    price = src.get("price")
                    if isinstance(price, (int, float)):
                        highest = price if highest is None else max(highest, price)
                    listing = self._to_listing(src, category, cat_gun_type)
                    if listing is None or listing.url in seen:
                        continue
                    seen.add(listing.url)
                    new_in_band += 1
                    if self.passes(criteria, listing):
                        emit(listing)
                if len(hits) < PER_PAGE:
                    return  # short page: this combo is exhausted
                if page >= DEPTH_WALL_PAGE:
                    break  # deeper pages would repeat; move the price cursor
                time.sleep(0.5)
            if highest is None or new_in_band == 0 or \
                    (cursor is not None and highest <= cursor):
                # the whole 10k window sits on one price point (or yielded
                # nothing new) — can't advance; surface the shortfall
                log.warning(
                    "gunmade: results under %r exceed the site's 10k-deep page "
                    "window and the price cursor can't advance past $%s; "
                    "some listings are unreachable.", params, cursor)
                return
            cursor = highest
            log.info("gunmade: price cursor -> $%s for %r", cursor, params)

    # ---- __data.json fetch + devalue decoding ------------------------------

    def _fetch_root(self, params: dict) -> dict:
        url = f"{DATA_URL}?{urlencode(params)}"
        try:
            body = fetch(url, timeout=60)
        except FetchError as e:
            if e.status in (403, 429):
                raise ClientBlocked(f"gunmade.com returned HTTP {e.status}") from e
            raise
        # Cloudflare fronts the site and, under bursts, answers 200 with an
        # HTML challenge page instead of JSON. Treat that as a block (back off),
        # not as a structure change — the markup is fine, we're just throttled.
        head = body.lstrip()[:600]
        if head[:1] == "<" or "Attention Required" in head or "cf-browser" in head:
            raise ClientBlocked(
                "gunmade.com served a Cloudflare challenge instead of JSON — "
                "rate-limited; slow down or retry later.")
        try:
            raw = json.loads(body)
        except json.JSONDecodeError as e:
            raise StructureError(
                f"gunmade.com __data.json is no longer JSON ({e}) — their "
                "SvelteKit data route changed; update clients/gunmade.py.") from e
        node = next((n for n in raw.get("nodes") or []
                     if isinstance(n, dict) and n.get("type") == "data"), None)
        if node is None or not isinstance(node.get("data"), list):
            raise StructureError(
                "gunmade.com __data.json has no devalue data node (keys: "
                f"{', '.join(list(raw)[:6])}) — their payload format changed.")
        arr = node["data"]
        top = arr[0]
        if not isinstance(top, dict):
            raise StructureError(
                "gunmade.com devalue root is not an object — the payload "
                "format changed; update _solve() in clients/gunmade.py.")
        # LIGHTER: the payload bundles a ~2.7MB `blogs` node (73% of it) plus
        # affiliate/local/gunbroker/gunsamerica sub-searches we don't use.
        # Solve ONLY the keys we read, so those big subtrees are never walked
        # into Python objects. (The bytes still download — no server-side field
        # selection exists — but the brotli negotiation in fetcher trims that.)
        memo: dict = {}
        root = {k: _solve(arr, top[k], memo) for k in _WANT_KEYS if k in top}
        if "elastic_product" not in root:
            raise StructureError(
                "gunmade.com payload lost its elastic_product node (top keys: "
                f"{', '.join(list(top)[:8])}); their search backend changed.")
        return root

    # ---- armslist private-seller listings ---------------------------------

    def _harvest_armslist(self, criteria, text: str, emit, seen: set):
        """One keyword fetch whose `armslistListings` node we mine for
        private-seller posts. Uses the caliber as a keyword (not a facet) when
        there's no other text, since the facet zeroes the armslist sub-search.
        Best-effort: Armslist is a bonus source, so a fetch/parse failure here
        never sinks the main store walk."""
        from . import calibers
        q = text or (calibers.search_term(criteria.caliber)
                     if criteria.caliber else "")
        params: dict = {"priceSort": "price"}
        if q:
            params["keyword"] = q
        if criteria.price_min is not None:
            params["minPrice"] = f"{criteria.price_min:g}"
        if criteria.price_max is not None:
            params["maxPrice"] = f"{criteria.price_max:g}"
        try:
            root = self._fetch_root(params)
        except Exception as e:  # noqa: BLE001 — bonus source, degrade quietly
            log.info("gunmade/armslist harvest skipped (%s: %s)",
                     type(e).__name__, e)
            return
        self._emit_armslist(root, criteria, emit, seen)

    def _emit_armslist(self, root: dict, criteria, emit, seen: set):
        """Every __data.json response also carries `armslistListings` — up to
        200 individual Armslist (private-seller) posts relevant to the query.
        We index GunsAmerica/GunBroker directly, but nothing else covers
        Armslist, so harvest it here for free (no extra request) and dedup by
        URL. It's a fixed top-200 that doesn't paginate, so a full result set
        is capped there — logged when we hit it."""
        items = root.get("armslistListings")
        if not isinstance(items, list):
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            url = (it.get("url") or "").strip()
            title = (it.get("title") or "").strip()
            if not url or not title or url in seen:
                continue
            seen.add(url)
            price = it.get("price")
            if not isinstance(price, (int, float)):
                price = None
            img = it.get("imageUrl") or ""
            tl = title.lower()
            cond = "used" if "used" in tl else ("new" if "new" in tl else "")
            listing = Listing(
                site="gunmade",
                url=url,
                title=title,
                condition=cond,
                condition_grade="new" if cond == "new" else "",
                listing_type="fixed",  # Armslist is fixed-price classifieds
                price=price,
                image=img if img.startswith("http") else "",
                posted_at=_parse_listed_ago(it.get("listedAgo")),
                extra={"kind": "", "source": "armslist", "store": "armslist.com"},
            )
            if self.passes(criteria, listing):
                emit(listing)
        if len(items) >= 200:
            log.info("gunmade/armslist: at the 200-post relevance cap for %r; "
                     "deeper private-seller results aren't paginable.",
                     criteria.keyword or criteria.caliber or "all")

    @staticmethod
    def _store_hits(root: dict) -> list[dict]:
        """The real store listings on a page: elastic hits minus the
        consolidated marketplace pseudo-hits (gunsamerica/gunbroker/armslist
        groups — we index those marketplaces directly)."""
        ep = root.get("elastic_product")
        hits = ((ep or {}).get("hits") or {}).get("hits")
        if not isinstance(hits, list):
            raise StructureError(
                "gunmade.com response lost elastic_product.hits.hits (root "
                f"keys: {', '.join(list(root)[:8])}); their search backend "
                "changed — update clients/gunmade.py.")
        out = []
        for h in hits:
            src = h.get("_source") if isinstance(h, dict) else None
            if isinstance(src, dict) and not src.get("__consolidated_marketplace"):
                out.append(src)
        return out

    def _matching_caliber_facets(self, wanted: str, base_params: dict) -> list[str]:
        """Their caliber URL param is an exact-match facet string; enumerate
        the vocabulary from page 1's facet aggregation and keep every spelling
        that resolves to the requested canonical caliber."""
        from . import calibers
        from .base import _norm_cal
        try:
            root = self._fetch_root(dict(base_params))
        except (ClientBlocked, StructureError):
            raise
        except Exception:
            return []
        buckets = (((root.get("elastic_product_attribute") or {})
                    .get("caliber") or {}).get("buckets")) or []
        want = calibers.canonical(wanted)
        out = []
        for b in buckets:
            key = b.get("key") if isinstance(b, dict) else None
            if not key or not isinstance(key, str):
                continue
            if want:
                if calibers.canonical(key) == want:
                    out.append(key)
            else:
                nk, nw = _norm_cal(key), _norm_cal(wanted)
                if nw and (nw == nk or nw in nk or nk in nw):
                    out.append(key)
        return list(dict.fromkeys(out))

    # ---- hit -> Listing -----------------------------------------------------

    @staticmethod
    def _to_listing(src: dict, category: str, cat_gun_type: str) -> Listing | None:
        if src.get("in_stock") is False:
            return None  # not buyable; other sites only list live inventory
        title = (src.get("title") or "").strip()
        url = src.get("originalUrl") or ""
        if not url:
            go = src.get("url") or ""
            url = BASE + go if go.startswith("/") else go
        if not title or not url:
            return None

        price = src.get("price")
        if not isinstance(price, (int, float)):
            price = None
        attrs = src.get("attributes") or {}
        images = src.get("images") or []
        img = images[0].get("url") if images and isinstance(images[0], dict) else ""
        img = img or ""
        if img.startswith("/"):
            img = BASE + img
        condition = (src.get("condition") or "").lower()
        if condition not in ("new", "used"):
            condition = ""
        hit_cat = src.get("category") or ""
        kind = _structured_kind(hit_cat, src.get("department") or "")

        listing = Listing(
            site="gunmade",
            url=url,
            title=title,
            manufacturer=src.get("manufacturer") or "",
            model=str(attrs.get("model") or ""),
            caliber=src.get("caliber") or "",
            action=titleparse.normalize_action(str(attrs.get("subcategory") or "")),
            capacity=str(src.get("capacity") or ""),
            condition=condition,
            condition_grade="new" if condition == "new" else "",
            upc=str(src.get("upc") or ""),
            listing_type="fixed",
            price=price,
            image=img,
            extra={
                "kind": kind,
                "category": hit_cat,
                "store": src.get("domain_name") or "",
            },
        )
        # the category names the platform too, but title enrichment already
        # refined revolvers/carbines — only fill the gap
        gt = _CATEGORY_GUN_TYPE.get(category or hit_cat, "") or cat_gun_type
        if gt and not listing.gun_type:
            listing.gun_type = gt
        return listing


_AGO_RE = re.compile(r"(\d+)\s*(mo|sec|min|yr|[smhdwy])\b", re.I)
_AGO_SECONDS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "d": 86400,
                "w": 604800, "mo": 2629800, "y": 31557600, "yr": 31557600}


def _parse_listed_ago(text) -> float | None:
    """Armslist relative post age ('1w ago', '3d ago', '2mo ago') -> epoch.
    Unknown/empty -> None (kept by the age filter, same as any unknown date)."""
    s = str(text or "")
    m = _AGO_RE.search(s)
    if not m or "ago" not in s.lower():
        return None
    unit = m.group(2).lower()
    secs = _AGO_SECONDS.get(unit)
    if secs is None:
        return None
    return time.time() - int(m.group(1)) * secs


def _solve(arr: list, i, memo: dict):
    """Decode one devalue reference: index -> value. Dicts map keys to indices,
    lists are index arrays (or a tagged special when the first element is a
    string, e.g. ['Date', ...]); negative indices are JS specials. memo is
    seeded before recursing so shared refs and cycles resolve."""
    if not isinstance(i, int):
        return i  # tagged-form payloads carry literals in index position
    if i < 0:
        return _SPECIALS.get(i)
    if i in memo:
        return memo[i]
    v = arr[i]
    if isinstance(v, dict):
        out: dict = {}
        memo[i] = out
        for k, idx in v.items():
            out[k] = _solve(arr, idx, memo)
        return out
    if isinstance(v, list):
        if v and isinstance(v[0], str):  # ['Date', ref] / ['Set', ...] etc.
            memo[i] = None
            solved = [_solve(arr, x, memo) for x in v[1:]]
            memo[i] = solved[0] if len(solved) == 1 else solved
            return memo[i]
        out_l: list = []
        memo[i] = out_l
        for idx in v:
            out_l.append(_solve(arr, idx, memo))
        return out_l
    return v
