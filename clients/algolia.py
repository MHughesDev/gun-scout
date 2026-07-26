"""Shared helper for Magento + Algolia storefronts.

A large slice of the gun-retail world runs Magento with the Algolia search
extension (ammo.com, Lucky Gunner, BulkAmmo, Academy, GunMagWarehouse, …). Every
such page embeds `algoliaConfig` with the search-only credentials, and Algolia
lives on `*.algolia.net` — which is NOT behind the storefront's Cloudflare — so
plain `requests` queries it directly. This is the same trick guns.com uses
(clients/gunscom.py); this module generalizes it so each site client is just a
few lines of config + field mapping.

Exhaustive retrieval: one Algolia query only pages through `nbHits<=1000`, so we
walk the `<prefix>_products_price_default_asc` replica with a price cursor
(numericFilters price>=cursor, dedup by objectID) — the same idea as gunscom's
filterPrice band walk, single-sided.
"""
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from urllib.parse import quote_plus

import requests

from fetcher import UA, CURL
from .base import ClientBlocked, StructureError

log = logging.getLogger("gun_scout")


@dataclass
class AlgoliaCreds:
    app_id: str
    api_key: str
    index_prefix: str      # Magento index prefix; products index = <prefix>_products


_APPID_RE = re.compile(r'"applicationI[dD]":"([A-Za-z0-9]{8,})"')
_KEY_RE = re.compile(r'"apiKey":"([A-Za-z0-9+/=]{20,})"')
_INDEX_RE = re.compile(r'"indexName":"([a-z0-9_]+)"')


class _KeyExpired(Exception):
    """Algolia returned 403 — the (often time-limited) search key needs a refresh."""


class _IndexMissing(Exception):
    """A replica/index doesn't exist (404) — fall back to another retrieval path."""


def _raw_get(url: str, timeout: int = 25) -> str:
    """GET returning the body even on a non-200 — Cloudflare challenge pages
    still embed the inline algoliaConfig we need to scrape."""
    if CURL:
        cmd = [CURL, "-s", "-L", "--compressed", "--max-time", str(timeout), "-A", UA, url]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 10,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode == 0:
            return proc.stdout.decode("utf-8", errors="replace")
    return requests.get(url, headers={"User-Agent": UA}, timeout=timeout).text


def scrape_creds(homepage_url: str) -> AlgoliaCreds:
    body = _raw_get(homepage_url)
    app, key, idx = _APPID_RE.search(body), _KEY_RE.search(body), _INDEX_RE.search(body)
    if not (app and key and idx):
        raise StructureError(
            f"Algolia config (applicationId/apiKey/indexName) not found on "
            f"{homepage_url} — the storefront changed or blocked us.")
    return AlgoliaCreds(app.group(1), key.group(1), idx.group(1))


def _price(hit: dict):
    """Magento Algolia price is `price.USD.default` (a number)."""
    p = hit.get("price")
    if isinstance(p, dict):
        usd = p.get("USD") or {}
        v = usd.get("default")
    else:
        v = p
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _params(query, hits_per_page, page=0, numeric=None, facets=None):
    parts = [f"query={quote_plus(query or '')}",
             f"hitsPerPage={hits_per_page}", f"page={page}"]
    if numeric:
        parts.append("numericFilters=" + quote_plus(json.dumps(numeric)))
    if facets:
        parts.append("facetFilters=" + quote_plus(json.dumps(facets)))
    return "&".join(parts)


def _post(creds: AlgoliaCreds, index: str, params: str, timeout: int = 30) -> dict:
    url = f"https://{creds.app_id}-dsn.algolia.net/1/indexes/{index}/query"
    headers = {"x-algolia-application-id": creds.app_id,
               "x-algolia-api-key": creds.api_key,
               "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, data=json.dumps({"params": params}),
                      timeout=timeout)
    if r.status_code == 403:
        raise _KeyExpired(r.text[:140])
    if r.status_code == 404 and "does not exist" in r.text:
        raise _IndexMissing(index)
    if r.status_code >= 400:
        raise StructureError(f"Algolia {index} HTTP {r.status_code}: {r.text[:140]}")
    return r.json()


def _post_refreshing(creds: AlgoliaCreds, homepage: str, index: str, params: str) -> dict:
    """POST, and if the key was rejected (403), re-scrape fresh creds from the
    homepage once and retry — Magento secured keys are often ~24h-lived."""
    try:
        return _post(creds, index, params)
    except _KeyExpired:
        if not homepage:
            raise ClientBlocked("Algolia key rejected and no homepage to refresh from")
        log.info("Algolia key rejected; re-scraping creds from %s", homepage)
        fresh = scrape_creds(homepage)
        creds.app_id, creds.api_key, creds.index_prefix = (
            fresh.app_id, fresh.api_key, fresh.index_prefix)
        try:
            return _post(creds, index, params)
        except _KeyExpired:
            raise ClientBlocked("Algolia key still rejected after refreshing creds")


def search(creds: AlgoliaCreds, homepage: str, query: str, criteria,
           numeric=None, facets=None):
    """Yield Algolia product hits.

    Health canary (criteria.max_pages set) -> one relevance-ranked page.
    Real search -> exhaustive price-cursor walk over the price-asc replica so we
    pull the whole matching set past Algolia's 1,000-hit-per-query window.
    """
    prod = creds.index_prefix + "_products"

    if criteria.max_pages is not None:
        data = _post_refreshing(creds, homepage, prod,
                                _params(query, 50, 0, numeric, facets))
        yield from data.get("hits", [])
        return

    # Exhaustive: prefer a price-asc replica + price cursor (walks past Algolia's
    # 1,000-hit-per-query window). Some stores don't expose price replicas, so
    # fall back to plain main-index paging (capped at the store's browsable window).
    replica = prod + "_price_default_asc"
    try:
        yield from _walk_price(creds, homepage, replica, query, numeric, facets)
    except _IndexMissing:
        log.info("no price replica for %s; paging the main index", prod)
        yield from _paginate(creds, homepage, prod, query, numeric, facets)


def _walk_price(creds, homepage, replica, query, numeric, facets):
    seen: set = set()
    cursor = None
    guard = 0
    while True:
        guard += 1
        if guard > 3000:                       # hard runaway stop
            log.warning("algolia walk guard tripped for %s", replica)
            break
        num = list(numeric or [])
        if cursor is not None:
            num.append(f"price.USD.default>={cursor}")
        data = _post_refreshing(creds, homepage, replica,
                                _params(query, 1000, 0, num, facets))
        hits = data.get("hits", [])
        if not hits:
            break
        new = 0
        page_max = cursor
        for h in hits:
            oid = h.get("objectID")
            if oid in seen:
                continue
            seen.add(oid)
            new += 1
            p = _price(h)
            if p is not None and (page_max is None or p > page_max):
                page_max = p
            yield h
        total = data.get("nbHits", 0)
        if len(seen) >= total:
            break
        if new == 0 or page_max is None or page_max == cursor:
            # a full page stuck at one price (unsplittable) — step past it; we
            # may drop items sharing that exact price beyond 1,000 (rare)
            if page_max is None:
                break
            cursor = round(page_max + 0.01, 2)
        else:
            cursor = page_max     # boundary items re-fetched next round, dedup drops them


def _paginate(creds, homepage, index, query, numeric, facets):
    """Fallback: page the main index until the store's browsable window (Algolia
    reports nbPages honoring paginationLimitedTo, so this stops cleanly)."""
    seen: set = set()
    page = 0
    while page < 30:
        data = _post_refreshing(creds, homepage, index,
                                _params(query, 1000, page, numeric, facets))
        hits = data.get("hits", [])
        if not hits:
            break
        for h in hits:
            oid = h.get("objectID")
            if oid in seen:
                continue
            seen.add(oid)
            yield h
        total = data.get("nbHits", 0)
        if len(seen) >= total or page + 1 >= data.get("nbPages", 1):
            break
        page += 1
