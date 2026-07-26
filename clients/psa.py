"""Palmetto State Armory client (retailer — new guns PLUS a used/surplus/
trade-in section, unlike the other retail sites).

Classic Magento 2 storefront (Amasty search extension), plain server-rendered
HTML that curl reads fine. Two routes:

- Text queries go to /catalogsearch/result/?q=<query>. Amasty 302-redirects
  queries it recognizes to category landing pages ('30-06 rifle' ->
  /guns/rifles/bolt-action-rifles/30-06-rifles.html, 'glock 19' ->
  /brands/glock/glock-19.html) — their redirect IS the query understanding, so
  we keep it: fetcher.fetch follows it, and we pick the landing URL back out
  of the page's <link rel="canonical"> to paginate it with ?p=N (the redirect
  wouldn't preserve a p= param). Unmapped queries render a native grid on the
  search URL itself, paginated with &p=N. A landing under /guns/ marks the
  results kind='firearm' (their categorization is store-curated, trustworthy —
  the hard-parts title override still applies). Exact-product queries can
  302 all the way to a product detail page; that parses as a single listing.

- No text query: walk the /guns/ category tree directly. The USED category
  (guns/used-guns-surplus-firearms-trade-ins) is walked FIRST so its URLs
  enter `seen` tagged used before the platform categories would claim them as
  new; outside it a title saying used/surplus/trade-in also tags used.

Cards: <li class="item product product-item"> with an
<a class="product-item-link" href> title anchor, a product-image-photo <img>,
and a machine-readable price (data-price-amount= + data-price-type=
"finalPrice"; the odd card has no price box — tolerated). Page size varies by
category (23-92/page) and product_list_limit is ignored, so pagination just
walks ?p=N until an empty or all-duplicate page (Magento repeats the last
page past the end). A dormant Cloudflare waiting room fronts the site
(__cfwaitingroom cookie) — not enforcing as of 2026-07, but be polite.
"""
import html as _html
import itertools
import re
import time
from typing import Callable
from urllib.parse import quote_plus, urlparse

from fetcher import fetch, FetchError
from models import SearchCriteria, Listing
from .base import SiteClient, ClientBlocked, StructureError, register, page_limit_reached
from . import titleparse

BASE = "https://palmettostatearmory.com"

_USED_CAT = "guns/used-guns-surplus-firearms-trade-ins"
# (path, gun_type it implies; '' where the tree mixes platforms). The used
# category MUST stay first — see module docstring.
_GUN_CATEGORIES = [
    (_USED_CAT, ""),
    ("guns/handguns", "pistol"),
    ("guns/rifles", "rifle"),
    ("guns/shotguns", "shotgun"),
    ("guns/ar-rifles-pistols", ""),
    ("guns/ak-rifles-pistols", ""),
    ("guns/pistol-caliber-carbines", "rifle"),
]

CARD_RE = re.compile(r'<li class="item product product-item"')
LINK_RE = re.compile(
    r'<a\s+class="product-item-link"\s+href="([^"]+)"\s*>(.*?)</a>', re.S)
IMG_RE = re.compile(
    r'<img[^>]*class="[^"]*product-image-photo[^"]*"[^>]*?src="([^"]+)"', re.S)
PRICE_RE = re.compile(
    r'data-price-amount="([\d.]+)"\s+data-price-type="finalPrice"')
CANONICAL_RE = re.compile(
    r'<link[^>]*rel="canonical"[^>]*href="([^"]+)"|'
    r'<link[^>]*href="([^"]+)"[^>]*rel="canonical"')
# a product DETAIL page, per the body class — the bare string
# 'catalog-product-view' also appears inside shared CSS on every page
PRODUCT_PAGE_RE = re.compile(r'<body[^>]*class="[^"]*catalog-product-view')
# main-product price on a detail page. The visible price box is JS-rendered
# for used/surplus items (and cross-sell tiles carry their own finalPrice
# pairs), but every product page embeds the main product's price in analytics
# JSON as "price":349.99 — verified identical to the displayed price on both
# new and used pages.
JSON_PRICE_RE = re.compile(r'"price":\s*([\d.]+)')
MAIN_PRICE_RE = re.compile(
    r'product-info-price.{0,3000}?data-price-amount="([\d.]+)"\s+'
    r'data-price-type="finalPrice"', re.S)
GALLERY_IMG_RE = re.compile(r'"(?:img|full)"\s*:\s*"([^"]+)"')
TITLE_H1_RE = re.compile(
    r'data-ui-id="page-title-wrapper"[^>]*>\s*([^<]+)')
OG_IMAGE_RE = re.compile(r'<meta property="og:image" content="([^"]+)"')
NO_RESULTS_RE = re.compile(
    r"search returned no results|no products matching|"
    r"can.t find (?:any )?products", re.I)
# an Amasty landing whose slug names an accessory family — e.g. the bare
# caliber query '30-06' 302s to /30-06-ammo.html — holds no guns at all
NONGUN_CAT_RE = re.compile(
    r"ammo|ammunition|optic|scope|magazin|holster|\bparts?\b|-parts?\b|"
    r"gear|reloading|knives|apparel|cleaning", re.I)
_USED_TITLE_RE = re.compile(
    r"\bused\b|\bsurplus\b|police\s+trade|\btrade[- ]in\b", re.I)
# out-of-stock cards render without a price box ('Notify me' instead of Add
# to Cart) — not buyable, skip like gunmade's in_stock=False
OOS_RE = re.compile(r"out.of.stock|amxnotif|stock unavailable", re.I)


@register
class PSAClient(SiteClient):
    name = "psa"
    label = "Palmetto State Armory"
    homepage = BASE

    def search(self, criteria: SearchCriteria, emit: Callable[[Listing], None]):
        from . import brands, calibers
        query = " ".join(p for p in (brands.search_term(criteria.manufacturer),
                                     criteria.keyword) if p) \
                or calibers.search_term(criteria.caliber)

        seen: set[str] = set()
        pages = [0]  # shared fetch counter so the health canary stays 1 page
        if query:
            self._search_text(criteria, emit, seen, query, pages)
            return

        # no text: walk the gun category tree (used first — see docstring)
        any_cards = False
        for cat, cat_gun_type in _GUN_CATEGORIES:
            is_used = cat == _USED_CAT
            if criteria.condition == "new" and is_used:
                continue
            if criteria.condition == "used" and not is_used:
                continue  # only the used/surplus category sells used
            got = self._walk(
                criteria, emit, seen,
                lambda p, c=cat: f"{BASE}/{c}.html" + (f"?p={p}" if p > 1 else ""),
                kind="firearm", gun_type=cat_gun_type, used=is_used,
                pages=pages)
            any_cards = any_cards or got
        if not any_cards and not page_limit_reached(criteria, pages[0]):
            # zero cards in every category: no stock at all, or markup drift?
            # The master gun category always has products — probe it.
            body = self._get(f"{BASE}/guns.html")
            self._parse_cards(body, probe=True)

    # ---- text route: Amasty search + redirect-aware pagination ------------

    def _search_text(self, criteria, emit, seen: set, query: str, pages: list,
                     requeried: bool = False):
        first_url = f"{BASE}/catalogsearch/result/?q={quote_plus(query)}"
        if page_limit_reached(criteria, pages[0]):
            return
        body = self._get(first_url)
        pages[0] += 1

        if PRODUCT_PAGE_RE.search(body):
            # exact-match query 302'd all the way to one product's page
            listing = self._parse_product_page(body)
            if listing and listing.url not in seen:
                seen.add(listing.url)
                if self.passes(criteria, listing):
                    emit(listing)
            return

        kind, used, base_url = "", False, None
        canon = self._canonical(body)
        if canon and "/catalogsearch/" not in canon:
            # Amasty redirected to a category landing page; paginate THAT (the
            # redirect drops p=), and inherit its categorization
            base_url = canon.split("#")[0].split("?")[0].rstrip("/")
            path = urlparse(canon).path
            kind = "firearm" if path.startswith("/guns/") else ""
            used = _USED_CAT.split("/")[-1] in canon
            if (criteria.hide_accessories and not kind
                    and "/brands/" not in path
                    and NONGUN_CAT_RE.search(path.rsplit("/", 1)[-1])):
                # landed in an accessory category ('30-06' -> /30-06-ammo.html:
                # their popularity mapping favors ammo for bare calibers).
                # Nothing there is a gun — re-ask with gun context instead;
                # '30-06 rifle' maps to their 30-06-rifles gun category.
                if not requeried:
                    for suffix in ("rifle", "pistol", "shotgun"):
                        self._search_text(criteria, emit, seen,
                                          f"{query} {suffix}", pages,
                                          requeried=True)
                return

        def url_for(p: int) -> str:
            if p == 1:
                return first_url
            if base_url:
                return f"{base_url}?p={p}"
            return f"{first_url}&p={p}"

        # synthetic gun-context re-queries exist only to recover the caliber's
        # guns from loose native grids — hold them to a strict caliber match
        # (the lenient unknown-caliber-keep policy stays for the user's own
        # query, whose relevance the site itself ranked)
        self._walk(criteria, emit, seen, url_for, kind=kind, gun_type="",
                   used=used, pages=pages, first_body=body,
                   require_caliber=criteria.caliber if requeried else "")

    # ---- shared page walk ---------------------------------------------------

    def _walk(self, criteria, emit, seen: set, url_for_page, kind: str,
              gun_type: str, used: bool, pages: list,
              first_body: str | None = None,
              require_caliber: str = "") -> bool:
        """Walk ?p=1,2,... until an empty or all-duplicate page (Magento
        repeats the last page for out-of-range p). With require_caliber set
        (synthetic gun-context re-queries), only title-caliber matches emit,
        and the walk stops once a relevance-ordered page stops producing any —
        the loose tail of a native grid never gets better. Returns True if
        any card parsed."""
        from . import calibers
        any_cards = False
        for page in itertools.count(1):
            if page == 1 and first_body is not None:
                body = first_body
            else:
                if page_limit_reached(criteria, pages[0]):
                    break
                body = self._get(url_for_page(page))
                pages[0] += 1
            cards = self._parse_cards(body, probe=(page == 1 and first_body is not None))
            if not cards:
                break
            any_cards = True
            new_on_page = 0
            matched_on_page = 0
            for listing in cards:
                if listing.url in seen:
                    continue
                seen.add(listing.url)
                new_on_page += 1
                if require_caliber and (
                        not listing.caliber
                        or not calibers.match(require_caliber, listing.caliber)):
                    continue
                matched_on_page += 1
                if used:
                    listing.condition = "used"
                    listing.condition_grade = ""
                if kind:
                    listing.extra["kind"] = kind
                if gun_type and not listing.gun_type:
                    listing.gun_type = gun_type
                if not self.passes(criteria, listing):
                    continue
                if listing.price is None:
                    # some category templates (used/surplus, and others on the
                    # show-more theme) render prices client-side only — the
                    # detail page always embeds the price in analytics JSON
                    listing.price = self._price_from_product_page(listing.url)
                emit(listing)
            if new_on_page == 0:
                break  # past the end (or a repeated last page)
            if require_caliber and matched_on_page == 0:
                break  # relevance ran dry for the synthetic query
            time.sleep(0.6)
        return any_cards

    # ---- fetching & parsing -------------------------------------------------

    def _get(self, url: str) -> str:
        for attempt in (1, 2):
            try:
                return fetch(url, timeout=40)
            except FetchError as e:
                if e.status in (403, 429, 503):
                    if attempt == 1:
                        # their WAF throws transient 403s under bursts; one
                        # backoff-retry rides them out
                        time.sleep(4)
                        continue
                    raise ClientBlocked(
                        f"palmettostatearmory.com returned HTTP {e.status} — "
                        "blocked or Cloudflare waiting room engaged.") from e
                raise

    @staticmethod
    def _canonical(body: str) -> str:
        m = CANONICAL_RE.search(body)
        if not m:
            return ""
        return m.group(1) or m.group(2) or ""

    def _parse_cards(self, body: str, probe: bool) -> list[Listing]:
        marks = list(CARD_RE.finditer(body))
        if not marks:
            if probe and not NO_RESULTS_RE.search(body):
                raise StructureError(
                    "palmettostatearmory.com page has no '<li class=\"item "
                    "product product-item\">' cards and doesn't say 'no "
                    "results' — their Magento markup changed; update "
                    "clients/psa.py regexes.")
            return []

        listings = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
            window = body[m.start():end]
            lm = LINK_RE.search(window)
            if not lm:
                continue
            if OOS_RE.search(window):
                continue  # not buyable; matches gunmade's in_stock skip
            href = lm.group(1)
            title = _strip_tags(lm.group(2))
            if not href or not title:
                continue
            im = IMG_RE.search(window)
            pm = PRICE_RE.search(window)
            price = None
            if pm:
                try:
                    price = float(pm.group(1))
                except ValueError:
                    pass
            condition = "used" if _USED_TITLE_RE.search(title) else "new"
            listings.append(self._make_listing(
                url=href if href.startswith("http") else BASE + href,
                title=title,
                image=(im.group(1) if im else ""),
                condition=condition,
                condition_grade="new" if condition == "new" else "",
                price=price,
            ))
        if probe and listings and all(l.price is None for l in listings):
            raise StructureError(
                "palmettostatearmory.com cards parse but no prices matched "
                "the data-price-amount/finalPrice pattern — their price "
                "markup changed.")
        return listings

    def _price_from_product_page(self, url: str) -> float | None:
        try:
            body = self._get(url)
        except (FetchError, ClientBlocked):
            return None
        time.sleep(0.3)
        for rex in (JSON_PRICE_RE, MAIN_PRICE_RE):
            m = rex.search(body)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    pass
        return None

    def _parse_product_page(self, body: str) -> Listing | None:
        tm = TITLE_H1_RE.search(body)
        title = _strip_tags(tm.group(1)) if tm else ""
        url = self._canonical(body)
        if not title or not url:
            return None
        price = None
        for rex in (JSON_PRICE_RE, MAIN_PRICE_RE):
            pm = rex.search(body)
            if pm:
                try:
                    price = float(pm.group(1))
                    break
                except ValueError:
                    pass
        om = OG_IMAGE_RE.search(body) or GALLERY_IMG_RE.search(body)
        condition = "used" if _USED_TITLE_RE.search(title) else "new"
        return self._make_listing(
            url=url,
            title=title,
            image=(om.group(1).replace("\\/", "/") if om else ""),
            condition=condition,
            condition_grade="new" if condition == "new" else "",
            price=price,
        )

    # ---- listing construction (overridden by the ammo/parts subclasses) ----

    def _make_listing(self, *, url, title, image, condition, condition_grade,
                      price):
        """Build one PSA listing. Base = a guns listing. The ammo/parts
        subclasses override this to stamp their own `vertical` (so the right
        enricher runs) and skip the guns-only fields."""
        return Listing(
            site=self.name, url=url, title=title, image=image,
            caliber=titleparse.caliber(title),
            barrel_length=titleparse.barrel_length(title),
            capacity=titleparse.capacity(title),
            condition=condition, condition_grade=condition_grade,
            listing_type="fixed", price=price,
        )


class _PSAVerticalClient(PSAClient):
    """Shared ammo/parts PSA behavior. No text query → walk the vertical's
    category landing pages (curated → tiles tagged with `_KIND`). Text query →
    the Amasty search route, paginated via the canonical, WITHOUT the guns-only
    accessory-category re-query (for ammo/parts, landing on an ammo/parts
    category is the goal). Relevance is enforced by passes() either way.
    Subclasses set vertical/name/canary + `_CATEGORIES`/`_KIND`.
    """
    _CATEGORIES: list = []
    _KIND = ""

    def _condition(self, title: str) -> str:
        return "new"

    def _make_listing(self, *, url, title, image, condition, condition_grade,
                      price):
        cond = self._condition(title)
        return Listing(
            vertical=self.vertical, site=self.name, url=url, title=title,
            image=image, condition=cond,
            condition_grade="new" if cond == "new" else "",
            listing_type="fixed", price=price,
        )

    def search(self, criteria: SearchCriteria, emit: Callable[[Listing], None]):
        from . import brands, calibers
        query = " ".join(p for p in (brands.search_term(criteria.manufacturer),
                                     criteria.keyword) if p) \
                or calibers.search_term(criteria.caliber)
        seen: set[str] = set()
        pages = [0]
        if query:
            self._search_text_simple(criteria, emit, seen, query, pages)
            return
        for cat in self._CATEGORIES:
            try:
                self._walk(
                    criteria, emit, seen,
                    lambda p, c=cat: f"{BASE}/{c}.html" + (f"?p={p}" if p > 1 else ""),
                    kind=self._KIND, gun_type="", used=False, pages=pages)
            except FetchError as e:
                if e.status == 404:
                    continue  # slug not a real category — skip it
                raise

    def _search_text_simple(self, criteria, emit, seen: set, query: str,
                            pages: list):
        first_url = f"{BASE}/catalogsearch/result/?q={quote_plus(query)}"
        if page_limit_reached(criteria, pages[0]):
            return
        body = self._get(first_url)
        pages[0] += 1
        if PRODUCT_PAGE_RE.search(body):
            listing = self._parse_product_page(body)
            if listing and listing.url not in seen:
                seen.add(listing.url)
                if self._KIND:
                    listing.extra["kind"] = self._KIND
                if self.passes(criteria, listing):
                    emit(listing)
            return
        base_url = None
        canon = self._canonical(body)
        if canon and "/catalogsearch/" not in canon:
            base_url = canon.split("#")[0].split("?")[0].rstrip("/")

        def url_for(p: int) -> str:
            if p == 1:
                return first_url
            return f"{base_url}?p={p}" if base_url else f"{first_url}&p={p}"

        self._walk(criteria, emit, seen, url_for, kind=self._KIND, gun_type="",
                   used=False, pages=pages, first_body=body)


@register
class PSAAmmoClient(_PSAVerticalClient):
    vertical = "ammo"
    name = "psa_ammo"           # names must be unique across verticals
    label = "Palmetto State Armory"
    canary_keyword = "9mm"
    _CATEGORIES = ["ammo"]      # confirm live: PSA ammo landing slug(s)
    _KIND = "ammo"

    def _condition(self, title: str) -> str:
        low = title.lower()
        return ("reman" if "reman" in low else
                "surplus" if "surplus" in low or "milsurp" in low else "new")


@register
class PSAPartsClient(_PSAVerticalClient):
    vertical = "parts"
    name = "psa_parts"
    label = "Palmetto State Armory"
    canary_keyword = "magazine"
    # best-guess category slugs; wrong ones 404 and skip. Confirm the live set.
    _CATEGORIES = ["optics", "magazines", "parts-accessories", "parts"]
    _KIND = "accessory"


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", _html.unescape(s or "")).strip()
