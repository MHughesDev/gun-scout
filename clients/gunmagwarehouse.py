"""GunMagWarehouse client (Parts engine) — Magento + Algolia.

Proven live 2026-07: index `m2_prod_default_products` returns structured
am_brand / caliber / material / in_stock / upc / price with titles we parse for
part_category + fitment ('Magpul PMAG GEN M3 AR-15 .223/5.56 30-Round
Magazine'). The storefront is Cloudflare-gated and the Algolia search key is
~24h time-limited, so algolia.scrape_creds reads the (still-embedded) config off
the challenge page and algolia.search re-scrapes on any 403.
"""
import logging
from typing import Callable

from models import SearchCriteria, Listing
from . import algolia, calibers
from .algolia import AlgoliaCreds
from .base import SiteClient, register

log = logging.getLogger("gun_scout")

BASE = "https://gunmagwarehouse.com"
# ~24h-lived key; a fallback only — algolia.search refreshes it from BASE on 403.
LAST_KNOWN = AlgoliaCreds(
    app_id="0ZJA7NXACC",
    api_key=("ZWNhYTQ0YWQzZDUxMWM4YWNjNzQwN2Q2OGU0YWFmODk1MjUyZjA0ODY5YTJlNDVi"
             "YWI0YmFhMTcxNTdiN2FkMXRhZ0ZpbHRlcnM9JnZhbGlkVW50aWw9MTc4NDg0MzAwNw=="),
    index_prefix="m2_prod_default",
)


def _first(v):
    """Algolia facet values are sometimes arrays; take the first."""
    if isinstance(v, (list, tuple)):
        return v[0] if v else ""
    return v or ""


@register
class GunMagWarehouseClient(SiteClient):
    vertical = "parts"
    name = "gunmagwarehouse"
    label = "GunMagWarehouse"
    homepage = BASE
    canary_keyword = "magazine"

    _creds: AlgoliaCreds | None = None

    def _get_creds(self) -> AlgoliaCreds:
        if GunMagWarehouseClient._creds is None:
            try:
                GunMagWarehouseClient._creds = algolia.scrape_creds(BASE + "/")
            except Exception as e:
                log.info("gunmagwarehouse cred scrape failed (%s); using last-known", e)
                GunMagWarehouseClient._creds = AlgoliaCreds(**vars(LAST_KNOWN))
        return GunMagWarehouseClient._creds

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
        # parts-specific filters that are also good free-text narrowers
        if c.f("fitment"):
            parts.append(c.f("fitment"))
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
        attrs = {"in_stock": hit.get("in_stock")}
        return Listing(
            vertical="parts", site=self.name, url=url, title=name,
            manufacturer=str(_first(hit.get("am_brand"))),
            caliber=_first(hit.get("caliber")) or "",
            condition="new",
            price=algolia._price(hit),
            image=hit.get("image_url") or hit.get("thumbnail_url") or "",
            upc=str(hit.get("upc") or ""),
            listing_type="fixed",
            attributes=attrs,
            extra={"kind": "accessory", "store": "gunmagwarehouse.com"},
        )
