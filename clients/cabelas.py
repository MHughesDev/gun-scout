"""Cabela's client (retailer — new + used "Gun Library" firearms).

Cabela's (and its parent Bass Pro) is a Next.js site fronted by Akamai Bot
Manager: curl/requests/curl_cffi all get 403 even on the homepage, because
Akamai requires the `_abck` sensor cookie that only running their JS produces.
So the HTML is unscrapeable for us.

BUT the site's product search runs on **Coveo** (hosted search-as-a-service,
like guns.com's Algolia), and Coveo lives on its OWN host —
`<org>.org.coveo.com` — which is NOT behind Cabela's Akamai. Plain `requests`
queries it fine. It returns rich structured data (caliber, barrel, condition,
firearm-class, price) and supports server-side filtering + pagination. Probed
2026-07-20: org `bassproshopsproductionl92epymr`, 1,335 guns for "30-06".

The one catch: Coveo requires a short-lived (~4h) anonymous bearer token, and
that token is minted only by `POST cabelas.com/api/v2/.../coveo/getCoveoToken`
— which IS behind Akamai, so plain HTTP can't mint it. `cabelas_token.py` mints
it automatically by driving a real (headed) browser via Playwright, caches it
with its expiry, and transparently re-mints when it goes stale — so this client
just asks for a token and never worries about freshness. If the minter can't
run (Node/Playwright not set up, or Akamai blocks it) the client reports itself
blocked, like the GunBroker client without a dev key. A manual token can still
be forced via the CABELAS_COVEO_TOKEN env var (no browser needed then).

Server-side filters (Coveo advanced query `aq`): @isgun==1 (guns only, drops
ammo/optics/gear), @isusedgun==1 / @isnew==1 (condition), @offerprice range.
Product URL: https://www.cabelas.com/shop/en/<producturlkeyword>.
"""
import json
import re
import time
from typing import Callable

import requests

import cabelas_token
from models import SearchCriteria, Listing
from .base import SiteClient, ClientBlocked, StructureError, register, page_limit_reached

PER_PAGE = 200            # bigger pages blow Coveo's 20 MB response cap
DEEP_PAGE_CAP = 5000      # Coveo won't page past firstResult=5000 per query

# only the raw fields we map — trims the response so we can use big pages.
# action/magazine_capacity/upc probed 2026-07-20: action is filled for 76% of
# new guns and 99% of used; magazine_capacity is a bare number ('17') on 80%
# of new guns; upc is 100% both. class_name carries the action for new guns
# ('SEMI-AUTO PISTOLS') while sub_class_name has the platform ('CENTERFIRE
# PISTOLS' new / 'REVOLVER' used).
_FIELDS = [
    "ec_name", "name", "title", "offerprice", "ec_price", "listprice",
    "brand", "ec_brand", "cartridge_or_gauge", "barrel_length",
    "action", "capacity", "magazine_capacity", "upc",
    "sub_class_name", "class_name", "department_name",
    "isgun", "isnew", "isusedgun", "location",
    "borecondition", "metalcondition", "woodcondition",
    "ec_images", "fullimage", "producturlkeyword", "availquantity",
]


@register
class CabelasClient(SiteClient):
    name = "cabelas"
    label = "Cabela's"
    homepage = "https://www.cabelas.com"

    def search(self, criteria: SearchCriteria, emit: Callable[[Listing], None]):
        q = self._build_query(criteria)
        aq = self._build_aq(criteria)
        seen: set[str] = set()
        # First query establishes the total; if it exceeds Coveo's 5,000
        # deep-paging cap, split into price bands so we still get everything.
        # (_post fetches a fresh token per request, so a long band walk that
        # outlives the current token re-mints transparently rather than dying.)
        total = self._page_through(q, aq, criteria, emit, seen)
        if criteria.max_pages is None and total > DEEP_PAGE_CAP:
            self._band_walk(q, aq, criteria, emit, seen)

    def _page_through(self, q, aq, criteria, emit, seen,
                      price_lo=None, price_hi=None) -> int:
        """Walk firstResult=0,PER_PAGE,... until the result set (or the 5,000
        cap, or an all-duplicate page) is exhausted. Returns the total count."""
        band_aq = aq
        if price_lo is not None:
            band_aq += f" @offerprice>={price_lo:g}"
        if price_hi is not None:
            band_aq += f" @offerprice<{price_hi:g}"

        total = 0
        for page in range(0, DEEP_PAGE_CAP // PER_PAGE + 1):
            if page_limit_reached(criteria, page):
                break
            first = page * PER_PAGE
            if first >= DEEP_PAGE_CAP:
                break
            body = {
                "q": q, "aq": band_aq.strip(),
                "numberOfResults": PER_PAGE, "firstResult": first,
                "fieldsToInclude": _FIELDS,
            }
            data = self._post(body)
            if page == 0:
                total = data.get("totalCount") or 0
                self._check_shape(data)
            results = data.get("results") or []
            if not results:
                break
            for res in results:
                lst = self._to_listing(res)
                if not lst or lst.url in seen:
                    continue
                seen.add(lst.url)
                if self.passes(criteria, lst):
                    emit(lst)
            # Deterministic firstResult paging: `total` (from page 0) is the ONLY
            # correct stopping point. Do NOT break on an all-duplicate page — in
            # a band walk the already-seen (most-relevant) items cluster at the
            # FRONT of each band, so page 0 can be all dupes while thousands of
            # unseen items sit deeper in the same band.
            if first + PER_PAGE >= total:
                break
            time.sleep(0.3)
        return total

    def _band_walk(self, q, aq, criteria, emit, seen):
        """>5,000 matches: the index caps deep paging at 5,000 per query, so
        recurse into price bands until each band fits under the cap."""
        stack = [(0.0, None)]
        while stack:
            lo, hi = stack.pop()
            body = {"q": q, "aq": (aq + f" @offerprice>={lo:g}"
                    + (f" @offerprice<{hi:g}" if hi is not None else "")).strip(),
                    "numberOfResults": 1, "firstResult": 0}
            n = self._post(body).get("totalCount") or 0
            if not n:
                continue
            if n <= DEEP_PAGE_CAP or (hi is not None and hi - lo < 1):
                self._page_through(q, aq, criteria, emit, seen,
                                   price_lo=lo, price_hi=hi)
            else:
                mid = (lo + hi) / 2 if hi is not None else max(lo * 2, 1024.0)
                stack.append((mid, hi))
                stack.append((lo, mid))
            time.sleep(0.3)
        # Price bands use `@offerprice>=0`, which in Coveo matches only docs that
        # HAVE the field — items with no offerprice (e.g. sold/pending Gun
        # Library pieces) fall through every band. Sweep them by relevance.
        self._page_through(f"{q}", f"{aq} (NOT @offerprice)".strip(),
                           criteria, emit, seen)

    # ---- query construction ----------------------------------------------

    @staticmethod
    def _build_query(c: SearchCriteria) -> str:
        from . import brands, calibers
        parts = [c.keyword]
        if c.manufacturer:
            parts.insert(0, brands.search_term(c.manufacturer))
        if c.caliber:
            parts.append(calibers.search_term(c.caliber))
        return " ".join(p for p in parts if p).strip()

    def _vertical_aq(self) -> list:
        """Coveo advanced-query clauses that scope the catalog to this client's
        vertical. Guns = firearms only; the ammo/parts subclasses exclude
        firearms (@isgun==0) and let ammoparse/partsparse pin the exact vertical
        via passes()."""
        return ["@isgun==1"]

    def _build_aq(self, c: SearchCriteria) -> str:
        clauses = list(self._vertical_aq())
        if self.vertical == "guns":
            # condition maps to the Gun Library's used flag (guns only)
            if c.condition == "used":
                clauses.append("@isusedgun==1")
            elif c.condition == "new":
                clauses.append("@isusedgun==0")
        if c.price_min is not None:
            clauses.append(f"@offerprice>={c.price_min:g}")
        if c.price_max is not None:
            clauses.append(f"@offerprice<={c.price_max:g}")
        return " ".join(clauses)

    @staticmethod
    def _post(body, _retried: bool = False) -> dict:
        # Fetch the token per request: normally a cheap cached read, but if it
        # has slipped within the remint buffer (e.g. during a multi-minute band
        # walk) get_valid_token() re-mints transparently, so we never send Coveo
        # a token that's about to die.
        try:
            token = cabelas_token.get_valid_token()
        except cabelas_token.MintError as e:
            raise ClientBlocked(f"Cabela's token unavailable: {e}") from e
        org, _ = cabelas_token._jwt_org_exp(token)
        if not org:
            raise ClientBlocked("Cabela's token is not a valid Coveo JWT.")
        url = f"https://{org}.org.coveo.com/rest/search/v2?organizationId={org}"
        headers = {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json"}
        try:
            resp = requests.post(url, headers=headers,
                                 data=json.dumps(body), timeout=30)
        except requests.RequestException as e:
            raise StructureError(f"Cabela's Coveo request failed: {e}") from e
        if resp.status_code in (401, 403, 419):
            # token rejected despite looking fresh (clock skew / early revoke).
            # Drop THIS token (not a newer one another thread minted) and retry
            # ONCE with a freshly-minted one, so a mid-walk rejection self-heals
            # instead of aborting the whole search with partial results.
            cabelas_token.invalidate(token)
            if not _retried:
                return CabelasClient._post(body, _retried=True)
            raise ClientBlocked(
                "Cabela's Coveo keeps rejecting freshly-minted tokens.")
        if resp.status_code >= 400:
            raise StructureError(
                f"Cabela's Coveo returned HTTP {resp.status_code}: "
                f"{resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as e:
            raise StructureError(f"Cabela's Coveo response is not JSON: {e}") from e

    @staticmethod
    def _check_shape(data: dict):
        if "results" not in data or "totalCount" not in data:
            raise StructureError(
                "Cabela's Coveo response lost 'results'/'totalCount' (has: "
                f"{', '.join(list(data)[:8])}); the search API changed.")
        results = data.get("results") or []
        if results and not (results[0].get("raw") or {}):
            raise StructureError(
                "Cabela's Coveo results carry no 'raw' fields; update _to_listing().")

    # ---- result mapping ---------------------------------------------------

    def _to_listing(self, res: dict) -> Listing | None:
        raw = res.get("raw") or {}
        key = raw.get("producturlkeyword")
        if not key:
            return None
        title = (raw.get("ec_name") or raw.get("name") or res.get("title")
                 or "Untitled").replace("~", " ").strip()

        price = None
        for f in ("offerprice", "ec_price", "listprice"):
            try:
                price = float(raw[f]); break
            except (KeyError, TypeError, ValueError):
                continue

        used = str(raw.get("isusedgun") or "0") == "1"
        condition = "used" if used else "new"
        # used "Gun Library" listings carry component grades; surface the WORST
        # of them (a gun is only as good as its roughest part), not just the
        # first present one.
        grade = _worst_grade([raw.get("metalcondition"), raw.get("borecondition"),
                              raw.get("woodcondition")]) or ("new" if not used else "")

        barrel = None
        bl = str(raw.get("barrel_length") or "")
        m = re.search(r"[\d.]+", bl)
        if m:
            try:
                barrel = float(m.group())
            except ValueError:
                barrel = None

        sub = (raw.get("sub_class_name") or "").upper()
        cls = (raw.get("class_name") or "").upper()
        is_firearm = str(raw.get("isgun") or "0") == "1"

        from . import titleparse
        # Structured action field first; new-catalog guns often leave it empty
        # but encode the action in class_name ('SEMI-AUTO PISTOLS')
        act = titleparse.normalize_action(raw.get("action")) \
            or titleparse.normalize_action(cls)

        cap = ""
        m_cap = re.search(r"\d{1,3}", str(raw.get("magazine_capacity")
                                          or raw.get("capacity") or ""))
        if m_cap and 1 <= int(m_cap.group()) <= 101:
            cap = f"{m_cap.group()} rounds"

        # ec_images can be a multi-value list; take the first, else fall back
        # to fullimage (a plain string). A raw list here would fail the
        # str/startswith guard below and silently drop the thumbnail.
        img = raw.get("ec_images")
        if isinstance(img, list):
            img = img[0] if img else None
        img = img or raw.get("fullimage") or ""
        return Listing(
            vertical=self.vertical,
            site=self.name,
            url=f"https://www.cabelas.com/shop/en/{key}",
            title=title,
            manufacturer=_clean_brand(raw.get("brand") or raw.get("ec_brand")),
            caliber=raw.get("cartridge_or_gauge") or "",
            action=act,
            gun_type=_gun_type_from_cat(sub, cls),
            barrel_length=barrel,
            capacity=cap,
            upc=str(raw.get("upc") or ""),
            condition=condition,
            condition_grade=grade,
            listing_type="fixed",
            price=price,
            image=img if isinstance(img, str) and img.startswith("http") else "",
            extra={
                "kind": "firearm" if is_firearm else "accessory",
                "category": sub or raw.get("class_name"),
                "class": raw.get("class_name"),
                "location": raw.get("location"),  # used guns ship from a store
                "in_stock": raw.get("availquantity"),
            },
        )

    def health_check(self) -> dict:
        # Only run the canary if a token is already cached — don't kick off a
        # ~1-minute browser mint just for a health check. No cached token yet =
        # report degraded with guidance rather than blocking on a mint.
        if not cabelas_token.status().get("fresh"):
            return self._health(
                "degraded",
                "no cached Coveo token yet — it will be minted on first search "
                "(run `python cabelas_token.py` to pre-mint and verify)",
                0, time.time())
        return super().health_check()


class _CabelasVerticalClient(CabelasClient):
    """Ammo/parts through the same Coveo org, scoped to non-firearms
    (@isgun==0 via _vertical_aq). The exact vertical is then pinned by
    ammoparse/partsparse through passes(). Everything else (token mint, price
    band walk, deep paging) is inherited unchanged. Subclasses set
    vertical/name/canary."""

    def _vertical_aq(self) -> list:
        return ["@isgun==0"]


@register
class CabelasAmmoClient(_CabelasVerticalClient):
    vertical = "ammo"
    name = "cabelas_ammo"       # names must be unique across verticals
    label = "Cabela's"
    canary_keyword = "9mm"


@register
class CabelasPartsClient(_CabelasVerticalClient):
    vertical = "parts"
    name = "cabelas_parts"
    label = "Cabela's"
    canary_keyword = "holster"


def _clean_brand(v) -> str:
    """Coveo sometimes returns the literal string 'None' (or None) for brand."""
    s = (v or "").strip()
    return "" if s.lower() == "none" else s


def _gun_type_from_cat(sub: str, cls: str) -> str:
    """Platform from Coveo's category names: sub_class_name is 'CENTERFIRE
    PISTOLS'/'RIFLE'/'REVOLVER'..., class_name adds 'SEMI-AUTO PISTOLS'/
    'USED GUNS'. Checked most-specific-first (a 'REVOLVER' must not fall into
    the PISTOL bucket via 'CENTERFIRE REVOLVERS' vs 'PISTOL' word order)."""
    hay = f"{sub} {cls}"
    for word, gt in (("REVOLVER", "revolver"),
                     ("MUZZLELOADER", "muzzleloader"),
                     ("BLACK POWDER", "muzzleloader"),
                     ("PISTOL", "pistol"), ("HANDGUN", "pistol"),
                     ("SHOTGUN", "shotgun"),
                     ("MODERN SPORTING", "rifle"), ("MSR", "rifle"),
                     ("RIFLE", "rifle")):
        if word in hay:
            return gt
    return ""


# Cabela's Gun Library condition grades, best -> worst. A used gun's overall
# grade should reflect its roughest component.
_GRADE_SEVERITY = {
    "new": 0, "like-new": 1, "excellent": 2, "very-good": 3,
    "good": 4, "fair": 5, "poor": 6,
}


def _norm_grade(v: str) -> str:
    s = (v or "").strip().lower()
    # match the longest known grade phrase the value starts with ('very good'
    # before 'good', 'like new' before 'new'); values carry a trailing
    # description like 'Good-some handling marks' we ignore
    for phrase in ("like new", "very good", "excellent", "good", "fair",
                   "poor", "new"):
        if s.startswith(phrase):
            return phrase.replace(" ", "-")
    return ""


def _worst_grade(values: list) -> str:
    grades = [g for g in (_norm_grade(v) for v in values) if g]
    if not grades:
        return ""
    return max(grades, key=lambda g: _GRADE_SEVERITY.get(g, 3))
