"""Shared data structures for search criteria and listings.

These are vertical-agnostic: the common fields live here, and anything specific
to one engine (ammo grain/bullet type, part category/fitment) rides in
`SearchCriteria.filters` and `Listing.attributes`. The gun-specific fields
predate the multi-vertical split and stay as first-class attributes because the
8 gun clients read them directly; they are simply unused by the other verticals.
See search_engines_spec.md.
"""
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class SearchCriteria:
    vertical: str = "guns"           # guns | parts | ammo
    keyword: str = ""
    condition: str = "both"          # "new" | "used" | "both" (ammo adds reman/surplus via filters)
    action: str = ""                 # guns: "semi auto", "bolt", "lever"…
    manufacturer: str = ""           # brand — universal
    caliber: str = ""                # guns + ammo + caliber-specific parts
    barrel_min: Optional[float] = None   # guns, inches
    barrel_max: Optional[float] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    capacity_min: Optional[int] = None   # guns + parts (magazines)
    capacity_max: Optional[int] = None
    listing_type: str = "any"        # "any" | "fixed" | "auction"
    max_age_days: Optional[float] = None  # drop listings posted longer ago; None = any
    in_stock_only: bool = False      # ammo/parts: hide out-of-stock
    sites: list = field(default_factory=list)   # empty = all registered sites for the vertical
    # Page ceiling. None (the default) = EXHAUSTIVE: every client loops until the
    # site has no more results. Only the health-check canary sets it (to 1).
    max_pages: Optional[int] = None
    # Vertical-specific filter values (ammo: grain_min/max, bullet_type, cpr_max…;
    # parts: part_category, fitment…). Kept generic so the 3 engines share one
    # criteria object; each vertical reads what it needs via f()/f_num().
    filters: dict = field(default_factory=dict)

    # Keys that map to first-class attributes above; everything else the frontend
    # sends flat gets routed into `filters` by from_dict.
    _KNOWN = {
        "vertical", "keyword", "condition", "action", "manufacturer", "caliber",
        "barrel_min", "barrel_max", "price_min", "price_max", "capacity_min",
        "capacity_max", "listing_type", "max_age_days", "in_stock_only", "sites",
        "max_pages", "filters",
    }

    @property
    def hide_accessories(self) -> bool:
        """Guns clients scope their queries to firearm-only categories to stay
        junk-free. Relevance is always enforced now (no user toggle), so this is
        just "am I the guns engine?". See spec §5.3."""
        return self.vertical == "guns"

    def f(self, key: str, default=None):
        """Read a vertical-specific string filter value."""
        v = self.filters.get(key)
        return default if v in (None, "") else v

    def f_num(self, key: str, cast=float):
        """Read a vertical-specific numeric filter value (None if absent/bad)."""
        v = self.filters.get(key)
        if v in (None, ""):
            return None
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None

    @classmethod
    def from_dict(cls, d: dict) -> "SearchCriteria":
        def num(v, t):
            if v is None or v == "":
                return None
            try:
                return t(v)
            except (TypeError, ValueError):
                return None

        # anything the frontend sends that isn't a known common field is a
        # vertical-specific filter (grain_min, bullet_type, part_category…)
        filters = dict(d.get("filters") or {})
        for k, v in d.items():
            if k not in cls._KNOWN and k != "hide_accessories":
                filters[k] = v

        return cls(
            vertical=(d.get("vertical") or "guns").strip().lower(),
            keyword=(d.get("keyword") or "").strip(),
            condition=(d.get("condition") or "both").lower(),
            action=(d.get("action") or "").strip(),
            manufacturer=(d.get("manufacturer") or "").strip(),
            caliber=(d.get("caliber") or "").strip(),
            barrel_min=num(d.get("barrel_min"), float),
            barrel_max=num(d.get("barrel_max"), float),
            price_min=num(d.get("price_min"), float),
            price_max=num(d.get("price_max"), float),
            capacity_min=num(d.get("capacity_min"), int),
            capacity_max=num(d.get("capacity_max"), int),
            listing_type=(d.get("listing_type") or "any").lower(),
            max_age_days=num(d.get("max_age_days"), float),
            in_stock_only=bool(d.get("in_stock_only", False)),
            sites=d.get("sites") or [],
            max_pages=num(d.get("max_pages"), int),
            filters=filters,
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Listing:
    site: str
    url: str
    title: str
    vertical: str = "guns"               # guns | parts | ammo
    manufacturer: str = ""
    model: str = ""
    caliber: str = ""
    action: str = ""                     # guns canonical: semi auto|bolt|lever|pump|revolver|…
    gun_type: str = ""                   # pistol|revolver|rifle|shotgun|muzzleloader|other
    barrel_length: Optional[float] = None
    capacity: str = ""
    condition: str = ""                  # "new" | "used" | "reman" | "surplus" | ""
    condition_grade: str = ""            # new|like-new|excellent|very-good|good|fair|""
    trade_in: bool = False               # LE/police trade-in
    upc: str = ""
    listing_type: str = ""               # "fixed" | "auction" | "auction_buynow" | ""
    price: Optional[float] = None        # firm / buy-now price ONLY (never a bid)
    current_bid: Optional[float] = None  # auctions only
    bid_count: Optional[int] = None
    ends_at: Optional[float] = None      # auction close time (epoch)
    posted_at: Optional[float] = None    # when the site says it was listed (epoch)
    image: str = ""
    # Vertical-specific fields the clients/parsers fill (ammo: grain, bullet_type,
    # round_count, price_per_round, case_material, in_stock; parts: part_category,
    # fitment_canon, mpn). Read by store._row_from_listing (display rows) and
    # statstore.fact_from_listing (stat facts).
    attributes: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        # Fill any descriptive fields the site left blank by parsing the title,
        # using THIS listing's vertical enricher (guns → titleparse.enrich,
        # ammo → ammoparse, parts → partsparse). Structured data the client
        # already set always wins; enrichment only fills gaps. Runs at
        # construction so both the search filters and the stored/displayed rows
        # see completed data. Deferred import avoids an import cycle.
        try:
            from verticals import get
            get(self.vertical).enrich(self)
        except Exception:
            pass  # enrichment is best-effort; never break listing creation
