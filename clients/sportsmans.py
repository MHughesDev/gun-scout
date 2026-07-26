"""Sportsman's Warehouse client (retailer — new guns).

Search pages are classic server-rendered HTML (SAP Hybris). Each product card
has an `<a class="name" href="...">` with `data-product-name` / `data-brand`
attributes, followed by a `displayed-price` div. Specs (caliber, barrel,
capacity) are baked into the product name, so we parse them from the title.

Pagination gotcha (probed 2026-07): the simple `?text=<q>&page=N` URL IGNORES
the page param and always returns the first ~46 cards, so paging that way just
re-fetches page 1 forever. The real pager the site itself uses is
`?q=<q>:relevance&page=N` (0-indexed) — that advances correctly (page 0 and
page 1 share zero products). We use the `q=` form and dedup by URL defensively.

Accessory filtering (2026-07): they're a broad outdoors retailer and their
full-text search matches loosely, so a '30-06' query surfaces insect spray,
30-liter dry bags, deer supplements ('Whitetail Institute 30-06')... The title
heuristic can't enumerate every non-gun product, so for guns-only searches we
instead run the query INSIDE the site's firearm category pages
(`/c/<catId>?q=...`) — the site's own categorization is the filter, same
approach as guns.com's product.category and GunsAmerica's onlyGuns. Probed:
plain '30-06' search = 784 products (269 junk); Rifles-category '30-06' = 103,
all rifles. Category-scoped listings get extra.kind='firearm' so base.passes()
trusts the category, not the title.
"""
import itertools
import re
import time
from typing import Callable
from urllib.parse import quote

from fetcher import fetch, FetchError
from models import SearchCriteria, Listing
from .base import SiteClient, ClientBlocked, StructureError, register, page_limit_reached
from . import titleparse

BASE = "https://www.sportsmans.com"

# The site's firearm categories (mapped 2026-07 from the "Guns" nav tree,
# parent cat128000). Guns-only searches run inside these instead of /search.
# Third field: the gun_type the category implies (Handguns stays '' at the
# category level only where ambiguous — title enrichment refines revolvers).
_GUN_CATEGORIES = [
    ("cat100003", "Rifles", "rifle"),
    ("cat139633", "Handguns", "pistol"),
    ("cat100004", "Shotguns", "shotgun"),
    ("cat100008", "Muzzleloaders", "muzzleloader"),
    ("cat100006", "Black Powder Handguns", "muzzleloader"),
]

CARD_RE = re.compile(
    r'<a\s+class="name"\s+href="(?P<href>[^"]+)"'
    r'(?P<attrs>[^>]*)>',
    re.S)
ATTR_RE = re.compile(r'data-(product-name|brand)="([^"]*)"')
PRICE_RE = re.compile(r'class="displayed-price">\s*\$?([\d,]+\.?\d*)')
# each card's product photo precedes its name anchor:
# <img class="js-primary-image" src="/medias/....jpg?context=...">
IMG_RE = re.compile(r'<img[^>]*class="[^"]*js-primary-image[^"]*"[^>]*\ssrc="([^"]+)"')
NO_RESULTS_RE = re.compile(r"no (?:search )?results|0 results|nothing (?:was )?found", re.I)


@register
class SportsmansClient(SiteClient):
    name = "sportsmans"
    label = "Sportsman's Warehouse"
    homepage = BASE

    def search(self, criteria: SearchCriteria, emit: Callable[[Listing], None]):
        if criteria.condition == "used":
            return  # retailer sells new only; skip instead of returning noise
        from . import brands, calibers
        query = " ".join(p for p in (brands.search_term(criteria.manufacturer),
                                     criteria.keyword) if p) \
                or calibers.search_term(criteria.caliber)

        seen: set[str] = set()
        if not criteria.hide_accessories:
            # everything mode: plain sitewide search
            q = quote(f"{query or 'firearm'}:relevance", safe="")
            self._walk_pages(criteria, emit, seen,
                             lambda p: f"{BASE}/search?q={q}&page={p}",
                             probe_structure=True)
            return

        # guns only: run the query inside each firearm category page — the
        # site's own categorization replaces the title heuristic
        qpart = f"q={quote(f'{query}:relevance', safe='')}&" if query else ""
        any_cards = False
        for cat_id, cat_label, cat_gun_type in _GUN_CATEGORIES:
            got = self._walk_pages(
                criteria, emit, seen,
                lambda p, c=cat_id: f"{BASE}/c/{c}?{qpart}page={p}",
                category=cat_label, category_gun_type=cat_gun_type)
            any_cards = any_cards or got
        if not any_cards:
            # zero cards in every gun category: either genuinely no matches or
            # their markup drifted. One plain-search page tells them apart —
            # _parse_cards raises StructureError if the card markup is gone.
            q = quote(f"{query or 'rifle'}:relevance", safe="")
            body = self._get(f"{BASE}/search?q={q}")
            self._parse_cards(body, first_page=True)

    def _get(self, url: str) -> str:
        """Fetch, riding out transient WAF hiccups. Their edge throws sporadic
        403s under a burst of distinct page requests; a single backoff-retry
        clears them (verified: three clean full runs after one stray 403). Only
        give up — and show the BLOCKED pill — if it 403s twice in a row."""
        for attempt in (1, 2):
            try:
                # sportsmans.com sits behind a Cloudflare JS challenge that the
                # Windows Schannel curl 403s; a genuine Chrome TLS fingerprint
                # (curl_cffi) passes it. Verified 2026-07: chrome124 -> 200.
                return fetch(url, timeout=30, impersonate="chrome124")
            except FetchError as e:
                if e.status in (403, 429, 503):
                    if attempt == 1:
                        time.sleep(4)
                        continue
                    raise ClientBlocked(
                        f"sportsmans.com returned HTTP {e.status}") from e
                raise

    def _walk_pages(self, criteria: SearchCriteria, emit, seen: set,
                    url_for_page, category: str = "",
                    category_gun_type: str = "",
                    probe_structure: bool = False) -> bool:
        """Walk ?page=0,1,2... until an empty or all-duplicate page. Returns
        True if any product card was parsed on any page."""
        any_cards = False
        for page in itertools.count(0):
            if page_limit_reached(criteria, page):
                break
            body = self._get(url_for_page(page))

            cards = self._parse_cards(
                body, first_page=(probe_structure and page == 0))
            if not cards:
                break
            any_cards = True
            new_on_page = 0
            for listing in cards:
                if listing.url in seen:
                    continue
                seen.add(listing.url)
                new_on_page += 1
                if category:
                    # came from a firearm category page: structured truth, so
                    # base.passes() won't second-guess it from the title
                    listing.extra.update(kind="firearm", category=category)
                    # the category names the platform too; title enrichment
                    # already refined revolvers, so only fill the gap
                    if category_gun_type and not listing.gun_type:
                        listing.gun_type = category_gun_type
                if self.passes(criteria, listing):
                    emit(listing)
            # an all-duplicate page is how the site signals "past the end"
            if new_on_page == 0:
                break
            time.sleep(0.6)
        return any_cards

    def _parse_cards(self, body: str, first_page: bool) -> list[Listing]:
        matches = list(CARD_RE.finditer(body))
        if not matches:
            if first_page and not NO_RESULTS_RE.search(body):
                raise StructureError(
                    "sportsmans.com search page no longer contains "
                    "'<a class=\"name\" ...>' product cards and doesn't say "
                    "'no results' — their markup changed; update "
                    "clients/sportsmans.py regexes.")
            return []

        listings = []
        for i, m in enumerate(matches):
            attrs = dict(ATTR_RE.findall(m.group("attrs")))
            name = _unescape(attrs.get("product-name", ""))
            brand = _unescape(attrs.get("brand", ""))
            # photo precedes the name anchor; scope to this card by starting at
            # the previous card's anchor and keeping the closest match
            img_start = matches[i - 1].end() if i else max(0, m.start() - 4000)
            im = None
            for im in IMG_RE.finditer(body, img_start, m.start()):
                pass
            img = im.group(1) if im else ""
            # price sits between this card's anchor and the next card
            window_end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            pm = PRICE_RE.search(body, m.end(), window_end)
            price = None
            if pm:
                try:
                    price = float(pm.group(1).replace(",", ""))
                except ValueError:
                    pass
            href = m.group("href")
            if not name or not href:
                continue
            listings.append(Listing(
                site="sportsmans",
                url=BASE + href if href.startswith("/") else href,
                title=name,
                image=BASE + img if img.startswith("/") else img,
                manufacturer=brand,
                caliber=titleparse.caliber(name),
                barrel_length=titleparse.barrel_length(name),
                capacity=titleparse.capacity(name),
                condition="new",
                condition_grade="new",
                listing_type="fixed",
                price=price,
            ))
        if first_page and listings and all(l.price is None for l in listings):
            raise StructureError(
                "sportsmans.com product cards parse but no prices matched the "
                "'displayed-price' pattern — their price markup changed.")
        return listings


def _unescape(s: str) -> str:
    import html
    # attribute values can carry search-highlight markup like <em>…</em>
    return re.sub(r"<[^>]+>", "", html.unescape(s or "")).strip()
