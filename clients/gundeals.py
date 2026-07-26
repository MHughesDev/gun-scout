"""gun.deals client (deal aggregator — surfaces prices across dozens of
retailers, so it's great for the cheapest current price on a model).

Their Drupal search at /search/apachesolr_search/<query>?result_type=deal
renders product tiles with schema.org microdata: itemprop="name" for the
title and itemprop="price" with a machine-readable content attribute.
Everything is new-in-box retail; specs get parsed from titles.

Accessory filtering (2026-07): the Solr text search matches loosely (a '30-06'
query returns steel targets, bore-sight lasers, slings...), and the title
heuristic can't catch it all. But the site has real category pages —
/category/{hand-guns,rifles,shotguns,nfa} — that take a `?caliber=<id>` facet
(numeric ids; the id<->name table is scraped live from the category page's
filter dropdown and matched via calibers.canonical). So guns-only searches with
no free-text keyword walk the gun categories with the caliber facet applied:
the site's own categorization is the accessories filter. Probed:
/category/rifles?caliber=24 = clean .30-06 rifles; the same query through Solr
included lasers/targets/armor. Keyword searches still go through Solr (text
relevance needs it) with the title heuristic as before.
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

BASE = "https://gun.deals"

# firearm category slugs (site nav, 2026-07); NFA = SBRs etc., still guns
_GUN_CATEGORIES = ["hand-guns", "rifles", "shotguns", "nfa"]
_CATEGORY_GUN_TYPE = {"hand-guns": "pistol", "rifles": "rifle",
                      "shotguns": "shotgun", "nfa": "other"}

# The caliber filter is a <select data-query-key="caliber"> on category pages;
# its numeric option values are the ?caliber= facet ids ('30-06' -> 24). Scope
# option parsing to THAT select — the page also has Brands[]/Stores[] selects
# with numeric values, and a brand like '30-06 Outdoors' would false-match.
_CAL_SELECT_RE = re.compile(
    r'<select[^>]*data-query-key="caliber"[^>]*>(.*?)</select>', re.S)
_CAL_OPTION_RE = re.compile(r'<option[^>]*value="(\d+)"[^>]*>(.*?)</option>', re.S)

TILE_RE = re.compile(
    r'class="tile__title">\s*'
    r'<a href="(?P<href>/product/[^"]+)"[^>]*>.*?itemprop="name">(?P<name>.*?)</span>',
    re.S)
PRICE_RE = re.compile(r'itemprop="price"\s+content="([\d.]+)"')
NO_RESULTS_RE = re.compile(r"no results|did not match|0 (?:deals|results)", re.I)
# each tile's product photo precedes its title anchor (attributes span lines):
# <img class="tile__image" itemprop="image" src="https://gun.deals/cdn-cgi/...">
IMG_RE = re.compile(r'<img[^>]*class="[^"]*tile__image[^"]*"[^>]*\ssrc="([^"]+)"')
# each tile opens with '<span class="tile__updated-time"> 10 hours ago </span>'
# (relative last-updated time) just before the tile__title anchor
UPDATED_RE = re.compile(r'class="tile__updated-time">\s*([^<]*?)\s*</span>')
AGO_PART_RE = re.compile(r"(\d+)\s*(sec|min|hour|day|week|month|year)", re.I)
_AGO_SECONDS = {"sec": 1, "min": 60, "hour": 3600, "day": 86400,
                "week": 604800, "month": 2629800, "year": 31557600}


@register
class GunDealsClient(SiteClient):
    name = "gundeals"
    label = "gun.deals"
    homepage = BASE

    def search(self, criteria: SearchCriteria, emit: Callable[[Listing], None]):
        if criteria.condition == "used":
            return  # aggregates new retail deals only
        from . import brands, calibers
        text_query = " ".join(p for p in (brands.search_term(criteria.manufacturer),
                                          criteria.keyword) if p)

        seen: set[str] = set()
        if criteria.hide_accessories and not text_query:
            # no free text to rank by: start from the gun category pages, where
            # the site's own categorization filters out accessories/ammo/optics
            cal_ids = self._caliber_facet_ids(criteria.caliber) \
                if criteria.caliber else []
            if not criteria.caliber or cal_ids:
                self._search_gun_categories(criteria, emit, cal_ids, seen)
                if not criteria.caliber:
                    return  # categories already enumerate every gun deal
                # the caliber facet only sees deals the site tagged; many lack
                # tags, so ALSO run the text search — the title heuristic
                # filters those, and `seen` dedups the overlap

        # their Solr search loses results on abbreviations ('.300 WM' finds
        # half of what '300 win mag' does), so query the canonical spelling
        query = text_query \
            or calibers.search_term(criteria.caliber) or "firearm"
        self._search_solr(criteria, emit, query, seen)

    # ---- listing construction (overridden by the ammo/parts subclasses) ----

    def _make_listing(self, *, url, title, image, price, posted_at):
        """Build one gun.deals listing. Base = a guns listing (title-parsed
        caliber/barrel/capacity, new-in-box). The ammo/parts subclasses override
        this to stamp their own `vertical` so `Listing.__post_init__` runs the
        right enricher; they skip the guns-only fields."""
        return Listing(
            site=self.name, url=url, title=title, image=image,
            caliber=titleparse.caliber(title),
            barrel_length=titleparse.barrel_length(title),
            capacity=titleparse.capacity(title),
            condition="new", condition_grade="new",
            listing_type="fixed", price=price, posted_at=posted_at,
        )

    def _search_solr(self, criteria: SearchCriteria, emit, query: str,
                     seen: set):
        for page in itertools.count(0):
            if page_limit_reached(criteria, page):
                break
            url = f"{BASE}/search/apachesolr_search/{quote(query)}?result_type=deal"
            if page:
                url += f"&page={page}"
            try:
                body = fetch(url, timeout=30)
            except FetchError as e:
                if e.status in (403, 429):
                    raise ClientBlocked(f"gun.deals returned HTTP {e.status}") from e
                raise

            listings = self._parse_tiles(body, first_page=(page == 0))
            if not listings:
                break
            new_on_page = 0
            for listing in listings:
                if listing.url in seen:
                    continue
                seen.add(listing.url)
                new_on_page += 1
                if self.passes(criteria, listing):
                    emit(listing)
            # keep paging until a page repeats what we've seen (past the end,
            # gun.deals loops back to earlier results) or comes back empty
            if new_on_page == 0:
                break
            time.sleep(0.6)

    # ---- structured route: gun category pages + caliber facet -------------

    def _caliber_facet_ids(self, caliber_text: str) -> list[str]:
        """Map a caliber to the site's numeric ?caliber= facet id(s) by
        scraping the filter dropdown on a category page. Values are matched by
        canonical caliber name so '.30-06' finds their '30-06 (105)' option.
        Empty list = caliber unknown to their facet (caller falls back to
        text search)."""
        from . import calibers
        from .base import _norm_cal
        try:
            body = fetch(f"{BASE}/category/rifles", timeout=30)
        except FetchError:
            return []
        m = _CAL_SELECT_RE.search(body)
        if not m:
            return []
        want = calibers.canonical(caliber_text)
        ids = []
        for value, label in _CAL_OPTION_RE.findall(m.group(1)):
            label = _strip_tags(label)
            if want:
                if calibers.canonical(label) == want:
                    ids.append(value)
            else:
                nl, nw = _norm_cal(label), _norm_cal(caliber_text)
                if nw and (nw == nl or nw in nl or nl in nw):
                    ids.append(value)
        return list(dict.fromkeys(ids))

    def _search_gun_categories(self, criteria: SearchCriteria, emit,
                               cal_ids: list[str], seen: set):
        """Walk each firearm category page (optionally caliber-faceted) to the
        last page. Tiles get kind='firearm' — the category is the filter."""
        any_tiles = False
        for cat in _GUN_CATEGORIES:
            for cal in (cal_ids or [None]):
                for page in itertools.count(0):
                    if page_limit_reached(criteria, page):
                        break
                    params = [p for p in (f"caliber={cal}" if cal else "",
                                          f"page={page}" if page else "") if p]
                    url = f"{BASE}/category/{cat}"
                    if params:
                        url += "?" + "&".join(params)
                    try:
                        body = fetch(url, timeout=30)
                    except FetchError as e:
                        if e.status in (403, 429):
                            raise ClientBlocked(
                                f"gun.deals returned HTTP {e.status}") from e
                        raise
                    listings = self._parse_tiles(body, first_page=False)
                    if not listings:
                        break
                    any_tiles = True
                    new_on_page = 0
                    for listing in listings:
                        if listing.url in seen:
                            continue
                        seen.add(listing.url)
                        new_on_page += 1
                        listing.extra.update(kind="firearm", category=cat)
                        if not listing.gun_type:
                            listing.gun_type = _CATEGORY_GUN_TYPE.get(cat, "")
                        if self.passes(criteria, listing):
                            emit(listing)
                    if new_on_page == 0:
                        break
                    time.sleep(0.6)
        if not any_tiles:
            # nothing in any category: genuinely empty, or their markup
            # drifted? An unfiltered category page always has tiles, so parse
            # one with the structure probe on — raises StructureError on drift.
            body = fetch(f"{BASE}/category/rifles", timeout=30)
            self._parse_tiles(body, first_page=True)

    def _walk_categories(self, criteria: SearchCriteria, emit,
                         categories: list[str], kind: str, seen: set):
        """Walk each /category/<slug> page to its end, tagging tiles with
        `kind` (the site's own categorization is the relevance filter, same as
        the guns category walk). Unknown slugs 404 and are skipped, so a
        best-guess slug list is safe."""
        for cat in categories:
            for page in itertools.count(0):
                if page_limit_reached(criteria, page):
                    break
                url = f"{BASE}/category/{cat}" + (f"?page={page}" if page else "")
                try:
                    body = fetch(url, timeout=30)
                except FetchError as e:
                    if e.status in (403, 429):
                        raise ClientBlocked(
                            f"gun.deals returned HTTP {e.status}") from e
                    if e.status == 404:
                        break  # slug not a real category here — skip it
                    raise
                listings = self._parse_tiles(body, first_page=False)
                if not listings:
                    break
                new_on_page = 0
                for listing in listings:
                    if listing.url in seen:
                        continue
                    seen.add(listing.url)
                    new_on_page += 1
                    listing.extra["kind"] = kind
                    if self.passes(criteria, listing):
                        emit(listing)
                if new_on_page == 0:
                    break
                time.sleep(0.6)

    def _parse_tiles(self, body: str, first_page: bool) -> list[Listing]:
        tiles = list(TILE_RE.finditer(body))
        if not tiles:
            if first_page and not NO_RESULTS_RE.search(body):
                raise StructureError(
                    "gun.deals search page no longer contains product tiles "
                    "with itemprop=\"name\" microdata — their markup changed; "
                    "update clients/gundeals.py regexes.")
            return []

        listings, seen = [], set()
        for i, m in enumerate(tiles):
            href = m.group("href")
            if href in seen:
                continue
            seen.add(href)
            # the tile's updated-time span and photo precede its title; look
            # back only as far as the previous tile so we never grab a
            # neighbor's time or image
            window_start = tiles[i - 1].end() if i else max(0, m.start() - 2500)
            um = None
            for um in UPDATED_RE.finditer(body, window_start, m.start()):
                pass  # keep the last (closest) match before this title
            im = None
            for im in IMG_RE.finditer(body, window_start, m.start()):
                pass
            img = im.group(1) if im else ""
            name = _strip_tags(m.group("name"))
            # drop the trailing "- $664.99 (Email Price) + $6 Shipping" chatter
            # gun.deals appends to titles
            name = re.sub(r"\s*-\s*\$[\d,.]+.*$", "", name).strip()
            window_end = tiles[i + 1].start() if i + 1 < len(tiles) else len(body)
            pm = PRICE_RE.search(body, m.end(), window_end)
            price = None
            if pm:
                try:
                    price = float(pm.group(1))
                except ValueError:
                    pass
            if not name:
                continue
            listings.append(self._make_listing(
                url=BASE + href,
                title=name,
                image=BASE + img if img.startswith("/") else img,
                price=price,
                posted_at=_parse_ago(um.group(1)) if um else None,
            ))
        if first_page and listings and all(l.price is None for l in listings):
            raise StructureError(
                "gun.deals tiles parse but no itemprop=\"price\" values "
                "matched — their price microdata changed.")
        return listings


class _GunDealsVerticalClient(GunDealsClient):
    """Shared ammo/parts gun.deals behavior: walk the vertical's category pages
    (curated, so tiles are tagged with `_KIND`) then, if the user typed a
    keyword/brand/caliber, also run the Solr text search — there relevance is
    left to the vertical's title classifier (via passes()), exactly like the
    guns client does. Subclasses set vertical/name/canary + `_CATEGORIES`/`_KIND`.
    """
    _CATEGORIES: list = []
    _KIND = ""

    def _condition(self, title: str) -> str:
        return "new"

    def _make_listing(self, *, url, title, image, price, posted_at):
        cond = self._condition(title)
        return Listing(
            vertical=self.vertical, site=self.name, url=url, title=title,
            image=image, condition=cond,
            condition_grade="new" if cond == "new" else "",
            listing_type="fixed", price=price, posted_at=posted_at,
        )

    def search(self, criteria: SearchCriteria, emit: Callable[[Listing], None]):
        from . import brands, calibers
        seen: set[str] = set()
        self._walk_categories(criteria, emit, self._CATEGORIES, self._KIND, seen)
        text_query = " ".join(p for p in (
            brands.search_term(criteria.manufacturer), criteria.keyword,
            calibers.search_term(criteria.caliber)) if p)
        if text_query:
            self._search_solr(criteria, emit, text_query, seen)


@register
class GunDealsAmmoClient(_GunDealsVerticalClient):
    vertical = "ammo"
    name = "gundeals_ammo"      # names must be unique across verticals
    label = "gun.deals"
    canary_keyword = "9mm"
    _CATEGORIES = ["ammo"]      # confirm live: gun.deals ammo category slug
    _KIND = "ammo"

    def _condition(self, title: str) -> str:
        low = title.lower()
        return ("reman" if "reman" in low else
                "surplus" if "surplus" in low or "milsurp" in low else "new")


@register
class GunDealsPartsClient(_GunDealsVerticalClient):
    vertical = "parts"
    name = "gundeals_parts"
    label = "gun.deals"
    canary_keyword = "magazine"
    # best-guess slugs; wrong ones 404 and skip. Confirm the live set.
    _CATEGORIES = ["optics", "magazines", "parts", "accessories"]
    _KIND = "accessory"         # partsparse hint → guns 'accessory' bucket


def _strip_tags(s: str) -> str:
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _parse_ago(text: str) -> float | None:
    """Relative time like '10 hours ago' / '1 hour 46 min ago' -> epoch."""
    parts = AGO_PART_RE.findall(text or "")
    if not parts or "ago" not in (text or "").lower():
        return None
    seconds = sum(int(n) * _AGO_SECONDS[unit.lower()] for n, unit in parts)
    return time.time() - seconds
