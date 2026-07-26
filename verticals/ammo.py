"""Ammo vertical. Cost-per-round is the headline metric: computed at enrichment,
promoted to a column, the default sort, and the stats price basis.
"""
from clients import ammoparse, calibers  # noqa: F401
from .base import Vertical, InputField, Column, register

_CONDITION = [{"value": "both", "label": "Any"},
              {"value": "new", "label": "New"},
              {"value": "reman", "label": "Remanufactured"},
              {"value": "surplus", "label": "Surplus"}]
_CASE = [{"value": "", "label": "Any"},
         {"value": "brass", "label": "Brass"},
         {"value": "steel", "label": "Steel"},
         {"value": "aluminum", "label": "Aluminum"},
         {"value": "nickel", "label": "Nickel"}]
_MAX_AGE = [{"value": "", "label": "Any age"},
            {"value": "1", "label": "Last 24 hours"},
            {"value": "3", "label": "Last 3 days"},
            {"value": "7", "label": "Last week"},
            {"value": "30", "label": "Last month"}]
_BULLET_TYPES = ["FMJ", "JHP", "HP", "SP", "match", "ballistic-tip",
                 "frangible", "lead", "buckshot", "birdshot", "slug"]
_GRAIN_PRESETS = ["35", "40", "55", "62", "115", "124", "147", "150", "168", "175", "230"]
_CPR_PRESETS = ["0.15", "0.20", "0.25", "0.30", "0.40", "0.50", "0.75", "1.00"]
_QTY_PRESETS = ["20", "50", "100", "200", "500", "1000"]


class AmmoVertical(Vertical):
    id = "ammo"
    label = "Ammo"
    path = "/ammo"
    relevance_kind = "ammo"
    default_sort = ("price_per_round", 1)   # cheapest per round first
    default_view = "table"

    inputs = [
        InputField("keyword", "Keyword", "text",
                   placeholder="e.g. American Eagle, XM193"),
        InputField("caliber", "Caliber / cartridge", "combo", presets="calibers",
                   placeholder="e.g. 9mm Luger, 5.56 NATO"),
        InputField("bullet_type", "Bullet type", "combo", presets="bullet_types",
                   placeholder="e.g. FMJ, JHP"),
        InputField("grain", "Grain (bullet weight)", "range",
                   min_id="grain_min", max_id="grain_max", num_presets=_GRAIN_PRESETS),
        InputField("case_material", "Case material", "select", options=_CASE),
        InputField("condition", "Condition", "select", options=_CONDITION,
                   default="both"),
        InputField("manufacturer", "Brand", "combo", presets="manufacturers",
                   placeholder="e.g. Federal, PMC"),
        InputField("price", "Price ($)", "range",
                   min_id="price_min", max_id="price_max"),
        InputField("cpr", "Cost per round ($)", "range",
                   min_id="cpr_min", max_id="cpr_max", num_presets=_CPR_PRESETS,
                   note="the headline metric — sort by it below"),
        InputField("qty", "Quantity (rounds)", "range",
                   min_id="round_min", max_id="round_max", num_presets=_QTY_PRESETS),
        InputField("max_age_days", "Max listing age", "select", options=_MAX_AGE),
        InputField("in_stock_only", "In stock only", "checkbox", default=True),
    ]

    columns = [
        Column("image", "", sortable=False, render="image", grid="image"),
        Column("title", "Listing", render="title", grid="title"),
        Column("manufacturer", "Brand"),
        Column("caliber", "Caliber"),
        Column("grain", "Grain", render="num", suffix=" gr"),
        Column("bullet_type", "Bullet"),
        Column("round_count", "Rounds", render="num"),
        Column("price", "Price", render="price"),
        Column("price_per_round", "$/round", render="pricePerRound", grid="price"),
        Column("case_material", "Case"),
        Column("in_stock", "Stock", render="stock", grid="badge"),
        Column("posted_at", "Listed", render="listed", grid="meta"),
        Column("site", "Site", render="site", grid="meta"),
    ]

    # ---- stats ----
    stats_default_group = "caliber"
    stats_price_col = "price_per_round"
    stats_price_label = "$/round"
    stats_quality_fields = (
        ("caliber_canon", "caliber"), ("grain", "grain"),
        ("bullet_type", "bullet type"), ("round_count", "round count"),
        ("manufacturer", "brand"),
    )

    # ---- behavior ----
    def classify_kind(self, listing) -> str:
        return ammoparse.classify(listing.title,
                                  (listing.extra or {}).get("kind") or "")

    def enrich(self, listing) -> None:
        ammoparse.enrich(listing)

    def passes(self, c, listing) -> bool:
        if c.caliber and not calibers.match(c.caliber, listing.caliber):
            return False
        a = listing.attributes or {}
        g = a.get("grain")
        if g is not None:
            gmin, gmax = c.f_num("grain_min", int), c.f_num("grain_max", int)
            if gmin is not None and g < gmin:
                return False
            if gmax is not None and g > gmax:
                return False
        bt, have_bt = c.f("bullet_type"), (a.get("bullet_type") or "")
        if bt and have_bt:
            if bt.lower() not in have_bt.lower() and have_bt.lower() not in bt.lower():
                return False
        cm = c.f("case_material")
        if cm and a.get("case_material") and cm.lower() != a["case_material"].lower():
            return False
        cpr = a.get("price_per_round")
        if cpr is None and listing.price and a.get("round_count"):
            try:
                cpr = listing.price / int(a["round_count"])
            except (TypeError, ValueError, ZeroDivisionError):
                cpr = None
        if cpr is not None:
            cmin, cmax = c.f_num("cpr_min"), c.f_num("cpr_max")
            if cmin is not None and cpr < cmin:
                return False
            if cmax is not None and cpr > cmax:
                return False
        rc = a.get("round_count")
        if rc is not None:
            rmin, rmax = c.f_num("round_min", int), c.f_num("round_max", int)
            if rmin is not None and rc < rmin:
                return False
            if rmax is not None and rc > rmax:
                return False
        return True

    def presets(self) -> dict:
        return {"calibers": calibers.CANONICAL,
                "manufacturers": ammoparse._AMMO_BRANDS,
                "bullet_types": _BULLET_TYPES}


register(AmmoVertical())
