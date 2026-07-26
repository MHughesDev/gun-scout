"""ammo.com client (Ammo engine) — Magento + Algolia.

Proven live 2026-07: index `prod_ammonet_products` answers a plain POST to
algolia.net (no Cloudflare, non-expiring search key) with structured caliber /
jacket_type / in_stock / price and titles we parse for grain + round count
('Winchester 9mm Ammo - 50 Rounds of 147 Grain FMJ'). Cost-per-round (the Ammo
engine's headline sort) = price / round_count, computed in enrichment / statstore.

All acquisition logic lives in clients/algolia.py; this file is just the site's
credentials + the hit -> Listing field mapping.
"""
import logging
from typing import Callable

from models import SearchCriteria, Listing
from . import algolia, ammoparse, calibers
from .algolia import AlgoliaCreds
from .base import SiteClient, register

log = logging.getLogger("gun_scout")

BASE = "https://ammo.com"
# Scraped 2026-07; the search key here is scoped (visibility filter) but NOT
# time-limited, so it's a safe fallback. algolia.search re-scrapes on any 403.
LAST_KNOWN = AlgoliaCreds(
    app_id="KHD88LQOBC",
    api_key=("NmZlMzdlNjhhNWU5OTBkNjdjMzMyY2I3OGUyNGQ5YWNkYzY4NWRiZDgyNDFkOTY3"
             "ODUyYjY5MjczNTc4MWQ5MWZpbHRlcnM9Jm51bWVyaWNGaWx0ZXJzPXZpc2liaWxp"
             "dHlfc2VhcmNoJTNEMQ=="),
    index_prefix="prod_ammonet",
)


@register
class AmmoComClient(SiteClient):
    vertical = "ammo"
    name = "ammo_com"
    label = "Ammo.com"
    homepage = BASE
    canary_keyword = "9mm"

    _creds: AlgoliaCreds | None = None

    def _get_creds(self) -> AlgoliaCreds:
        if AmmoComClient._creds is None:
            try:
                AmmoComClient._creds = algolia.scrape_creds(BASE + "/")
            except Exception as e:
                log.info("ammo.com cred scrape failed (%s); using last-known", e)
                # copy so a later in-place refresh doesn't mutate the constant
                AmmoComClient._creds = AlgoliaCreds(**vars(LAST_KNOWN))
        return AmmoComClient._creds

    def search(self, criteria: SearchCriteria, emit: Callable[[Listing], None]):
        creds = self._get_creds()
        query = self._query(criteria)
        numeric = ["in_stock=1"] if criteria.in_stock_only else None
        for hit in algolia.search(creds, BASE + "/", query, criteria, numeric=numeric):
            listing = self._to_listing(hit)
            if listing and self.passes(criteria, listing):
                emit(listing)

    @staticmethod
    def _query(c: SearchCriteria) -> str:
        parts = [c.keyword]
        if c.manufacturer:
            parts.append(c.manufacturer)
        if c.caliber:
            parts.append(calibers.search_term(c.caliber))
        return " ".join(p for p in parts if p).strip()

    def _to_listing(self, hit: dict) -> Listing | None:
        name = (hit.get("name") or "").strip()
        url = (hit.get("url") or "").strip()
        if not name or not url:
            return None
        if url.startswith("/"):
            url = BASE + url
        title = name
        attrs = {"in_stock": hit.get("in_stock")}
        # structured jacket_type -> canonical bullet type ('Full Metal Jacket'->FMJ)
        jacket = hit.get("jacket_type") or ""
        bt = ammoparse.bullet_type(jacket) or ammoparse.bullet_type(title)
        if bt:
            attrs["bullet_type"] = bt
        low = title.lower()
        condition = ("reman" if "reman" in low else
                     "surplus" if "surplus" in low or "milsurp" in low else "new")
        return Listing(
            vertical="ammo", site=self.name, url=url, title=title,
            caliber=hit.get("caliber") or "",       # structured; enrich keeps it
            condition=condition,
            price=algolia._price(hit),
            image=hit.get("image_url") or hit.get("thumbnail_url") or "",
            upc=str(hit.get("manufacturer_sku") or ""),
            listing_type="fixed",
            attributes=attrs,
            extra={"kind": "ammo", "store": "ammo.com"},
        )
