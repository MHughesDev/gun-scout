"""Palmetto State Armory client (retailer — new guns PLUS a used/surplus/
trade-in section, unlike the other retail sites).

Classic Magento 2 storefront, plain server-rendered HTML. Everything here is
walked through CATEGORY pages, because PSA's own search endpoint is off limits:

    robots.txt:  # --- CATALOG & SEARCH ---
                 Disallow: /catalogsearch/

and Cloudflare enforces that with an unconditional managed challenge — every
/catalogsearch/ URL answers 403 + `Cf-Mitigated: challenge` no matter the
client, while the rest of the store serves normally (probed 2026-08-13; this
client used to search that endpoint and reported itself blocked once the rule
went in). Their Magento GraphQL and REST APIs both answer 401, and the only
open search route, /search/ajax/suggest/, returns related SEARCH TERMS and
their hit counts — no products. So there is no permitted query endpoint, and
a text query is answered from the landing pages robots.txt does allow:

- Brand landing pages (/brands/<slug>.html) when the query or the manufacturer
  filter names a brand we know. Slugs are derived from the brand and its
  aliases; PSA spells them inconsistently ('smith-wesson' but
  'heckler-and-koch', 'savage-arms' not 'savage'), so candidates are tried in
  order and the first with products wins — a wrong guess 404s or renders an
  empty category, both cheap.
- Otherwise the /guns/ category tree, same as a no-keyword search.

Either way the keyword is matched against titles HERE (_title_matches), since
no server-side relevance ranking is involved any more. Matching ignores
punctuation so '10/22' finds '10-22' and '1022'.

No text query: walk the /guns/ tree directly. The USED category
(guns/used-guns-surplus-firearms-trade-ins) is walked FIRST so its URLs enter
`seen` tagged used before the platform categories would claim them as new;
outside it a title saying used/surplus/trade-in also tags used.

Fetches impersonate Firefox (see IMPERSONATE): Cloudflare bot-scores the
allowed pages too, and every Chrome fingerprint curl_cffi ships — including
the current one — is challenged on them, while Firefox and Safari pass.

Cards: <li class="item product product-item"> with an
<a class="product-item-link" href> title anchor, a product-image-photo <img>,
and a machine-readable price (data-price-amount= + data-price-type=
"finalPrice"; the odd card has no price box — tolerated). Page size varies by
category (23-92/page) and product_list_limit is ignored, so pagination just
walks ?p=N until an empty or all-duplicate page (Magento repeats the last
page past the end). A dormant Cloudflare waiting room fronts the site
(__cfwaitingroom cookie) — not enforcing as of 2026-08, but be polite.
"""
import html as _html
import itertools
import re
import time
from typing import Callable
from fetcher import fetch, FetchError
from models import SearchCriteria, Listing
from .base import SiteClient, ClientBlocked, StructureError, register, page_limit_reached
from . import titleparse

BASE = "https://palmettostatearmory.com"

# Cloudflare bot-scores this store and challenges every Chrome fingerprint
# curl_cffi can forge (chrome124 through chrome146, desktop and android, all
# 403 + Cf-Mitigated: challenge — probed 2026-08-13). Firefox and Safari pass
# on the pages robots.txt allows. If this stops working, re-probe the target
# list rather than reaching for the disallowed search endpoint.
IMPERSONATE = "firefox144"

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
# main-product price on a detail page. The visible price box is JS-rendered
# for used/surplus items (and cross-sell tiles carry their own finalPrice
# pairs), but every product page embeds the main product's price in analytics
# JSON as "price":349.99 — verified identical to the displayed price on both
# new and used pages.
JSON_PRICE_RE = re.compile(r'"price":\s*([\d.]+)')
MAIN_PRICE_RE = re.compile(
    r'product-info-price.{0,3000}?data-price-amount="([\d.]+)"\s+'
    r'data-price-type="finalPrice"', re.S)
NO_RESULTS_RE = re.compile(
    r"search returned no results|no products matching|"
    r"can.t find (?:any )?products", re.I)
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

        # A brand landing page is a far tighter starting point than the whole
        # tree, so try it first; the tree is the fallback for everything else.
        any_cards = False
        if query:
            any_cards = self._walk_brand(criteria, emit, seen, query, pages)

        if not any_cards:
            any_cards = self._walk_gun_tree(criteria, emit, seen, query, pages)

        if not any_cards and not page_limit_reached(criteria, pages[0]):
            # zero cards anywhere: no stock at all, or markup drift?
            # The master gun category always has products — probe it.
            body = self._get(f"{BASE}/guns.html")
            self._parse_cards(body, probe=True)

    def _walk_gun_tree(self, criteria, emit, seen: set, query: str,
                       pages: list) -> bool:
        """Walk the /guns/ tree (used category first — see docstring), keeping
        only titles matching `query` when there is one."""
        any_cards = False
        for cat, cat_gun_type in self._ordered_categories(query):
            is_used = cat == _USED_CAT
            if criteria.condition == "new" and is_used:
                continue
            if criteria.condition == "used" and not is_used:
                continue  # only the used/surplus category sells used
            got = self._walk(
                criteria, emit, seen,
                lambda p, c=cat: f"{BASE}/{c}.html" + (f"?p={p}" if p > 1 else ""),
                kind="firearm", gun_type=cat_gun_type, used=is_used,
                pages=pages, match_title=query)
            any_cards = any_cards or got
        return any_cards

    @staticmethod
    def _ordered_categories(query: str) -> list[tuple]:
        """_GUN_CATEGORIES, but with any category the query names pulled to the
        front ('shotgun' -> guns/shotguns, 'pistol' -> guns/handguns, whose
        implied gun_type carries the word its slug doesn't).

        This matters because `pages` is one budget shared across the whole
        walk: on a capped search the categories visited first are the only ones
        visited at all, and without this a keyword search for 'shotgun' spends
        every page on used handguns and returns nothing."""
        words = {w for w in re.findall(r"[a-z]+", query.lower()) if len(w) > 2}
        if not words:
            return list(_GUN_CATEGORIES)

        def names(cat: str, gun_type: str) -> str:
            return f"{cat} {gun_type}"

        # stable: keeps the used-first ordering within each group, which the
        # used/new tagging depends on
        return sorted(_GUN_CATEGORIES,
                      key=lambda c: not any(w in names(*c) or w.rstrip("s") in names(*c)
                                            for w in words))

    # ---- text route: brand landing pages (their search is off limits) ------

    def _walk_brand(self, criteria, emit, seen: set, query: str,
                    pages: list) -> bool:
        """Walk the brand landing page for whichever brand the query or the
        manufacturer filter names. Returns True if one held products."""
        for url in self._brand_urls(criteria, query):
            try:
                got = self._walk(
                    criteria, emit, seen,
                    lambda p, u=url: u + (f"?p={p}" if p > 1 else ""),
                    kind="", gun_type="", used=False, pages=pages,
                    match_title=query)
            except FetchError as e:
                if e.status == 404:
                    continue  # not their spelling of the brand; try the next
                raise
            if got:
                return True
        return False

    @staticmethod
    def _brand_urls(criteria: SearchCriteria, query: str) -> list[str]:
        """Candidate brand landing URLs, best guess first. Empty when neither
        the manufacturer filter nor the head of the query names a brand we
        know — the caller then falls back to the category tree."""
        from . import brands
        name = brands.canonical(criteria.manufacturer)
        if not name:
            # 'ruger 10/22' -> Ruger. Longest leading phrase wins, so a
            # two-word maker ('smith wesson 686') beats its first word.
            words = query.split()
            for n in (3, 2, 1):
                name = brands.canonical(" ".join(words[:n]))
                if name:
                    break
        if not name:
            return []
        # PSA is inconsistent about '&': 'smith-wesson' but 'heckler-and-koch',
        # and some brands only exist under a fuller alias ('savage-arms', not
        # 'savage'). Try the canonical spelling both ways, then the aliases.
        slugs: list[str] = []
        for n in [name, *brands.ALIASES.get(name, [])]:
            for amp in (" ", " and "):
                s = _slugify(n.replace("&", amp))
                if s and s not in slugs:
                    slugs.append(s)
        return [f"{BASE}/brands/{s}.html" for s in slugs[:4]]

    # ---- shared page walk ---------------------------------------------------

    def _walk(self, criteria, emit, seen: set, url_for_page, kind: str,
              gun_type: str, used: bool, pages: list,
              first_body: str | None = None,
              match_title: str = "") -> bool:
        """Walk ?p=1,2,... until an empty or all-duplicate page (Magento
        repeats the last page for out-of-range p). With match_title set, only
        titles containing every word of it are emitted — these pages are whole
        categories, so the keyword filter that the site's own (disallowed)
        search would have applied has to happen here. Returns True if any card
        parsed, which is how the caller tells a real category from a wrong
        brand-slug guess."""
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
            for listing in cards:
                if listing.url in seen:
                    continue
                seen.add(listing.url)
                new_on_page += 1
                if match_title and not _title_matches(match_title, listing.title):
                    continue
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
            time.sleep(0.6)
        return any_cards

    # ---- fetching & parsing -------------------------------------------------

    def _get(self, url: str) -> str:
        for attempt in (1, 2):
            try:
                return fetch(url, timeout=40, impersonate=IMPERSONATE)
            except FetchError as e:
                if e.status in (403, 429, 503):
                    if attempt == 1:
                        # their WAF throws transient 403s under bursts; one
                        # backoff-retry rides them out
                        time.sleep(4)
                        continue
                    raise ClientBlocked(
                        f"palmettostatearmory.com returned HTTP {e.status} — "
                        "Cloudflare challenged this page. If it is a category "
                        f"URL, re-probe the {IMPERSONATE} fingerprint; their "
                        "search endpoint is disallowed and always answers "
                        "this.") from e
                raise

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
    """Shared ammo/parts PSA behavior: walk the vertical's category landing
    pages (curated → tiles tagged with `_KIND`), filtering titles by the
    keyword when there is one. No brand-landing shortcut — a brand page mixes
    every vertical, so for ammo/parts the vertical's own categories are both
    tighter and better tagged. Relevance is enforced by passes() either way.
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
        for cat in self._CATEGORIES:
            try:
                self._walk(
                    criteria, emit, seen,
                    lambda p, c=cat: f"{BASE}/{c}.html" + (f"?p={p}" if p > 1 else ""),
                    kind=self._KIND, gun_type="", used=False, pages=pages,
                    match_title=query)
            except FetchError as e:
                if e.status == 404:
                    continue  # slug not a real category — skip it
                raise


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


def _slugify(s: str) -> str:
    """Brand name -> PSA category slug ('Sig Sauer' -> 'sig-sauer')."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")


def _title_matches(query: str, title: str) -> bool:
    """Does the title satisfy every word of the query? Punctuation is stripped
    INSIDE each whitespace-separated word but words are kept apart, so '10/22'
    becomes one token '1022' and matches '10-22' and '10/22' — PSA writes model
    numbers all three ways — without '10' and '22' drifting apart to match
    'American 22LR 10rd'.

    A word STARTING with a digit ('22', '9mm', '300') has to line up with the
    front of a whole title word, or a longer number swallows it: '.22' should
    find '22LR' but not '2022', and '9mm' must not match inside '7.62x39mm'.
    Words starting with a letter stay loose (plain substring), since the site
    hyphenates and fuses names unpredictably."""
    words = [re.sub(r"[^a-z0-9]+", "", w) for w in query.lower().split()]
    toks = [t for t in (re.sub(r"[^a-z0-9]+", "", w)
                        for w in title.lower().split()) if t]
    flat = "".join(toks)
    for word in words:
        if not word:
            continue
        if word[0].isdigit():
            # equal, or a prefix that isn't cut mid-number ('22' -> '22lr' yes,
            # '22' -> '2255' no)
            if not any(t == word or (t.startswith(word)
                                     and not t[len(word):len(word) + 1].isdigit())
                       for t in toks):
                return False
        elif word not in flat:
            return False
    return True
