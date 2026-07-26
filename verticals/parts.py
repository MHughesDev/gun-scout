"""Parts & Accessories vertical. Primary axis is part_category; a fitment /
platform chip ties a part to the guns it fits. Relevance is the inverse of the
guns rule (keep accessories & serialized parts, drop complete guns and ammo).
"""
from clients import partsparse, calibers  # noqa: F401
from .base import Vertical, InputField, Column, register, first_int

# value = stored slug (partsparse categories), label = friendly display
PART_CATEGORIES = [
    {"value": "", "label": "Any category"},
    {"value": "red-dot", "label": "Red dot / reflex"},
    {"value": "scope", "label": "Scope / LPVO"},
    {"value": "magnifier", "label": "Magnifier"},
    {"value": "optic-mount", "label": "Optic mount / rings"},
    {"value": "magazine", "label": "Magazine"},
    {"value": "trigger", "label": "Trigger"},
    {"value": "bcg", "label": "Bolt carrier group"},
    {"value": "charging-handle", "label": "Charging handle"},
    {"value": "barrel", "label": "Barrel"},
    {"value": "handguard", "label": "Handguard / rail"},
    {"value": "muzzle-device", "label": "Muzzle device"},
    {"value": "suppressor", "label": "Suppressor"},
    {"value": "stock", "label": "Stock / brace"},
    {"value": "grip", "label": "Grip"},
    {"value": "sights", "label": "Sights"},
    {"value": "lower", "label": "Lower / LPK"},
    {"value": "upper", "label": "Upper"},
    {"value": "holster", "label": "Holster"},
    {"value": "light-laser", "label": "Light / laser"},
    {"value": "sling", "label": "Sling"},
    {"value": "bipod", "label": "Bipod / rest"},
    {"value": "cleaning", "label": "Cleaning"},
    {"value": "case-bag", "label": "Case / bag"},
    {"value": "small-parts", "label": "Small parts"},
]
FITMENTS = ["AR-15", "AR-10", "AK", "Glock", "1911", "SIG P320", "SIG P365",
            "Rem 700", "Rem 870", "Mossberg 500", "Ruger 10/22", "CZ 75",
            "Beretta 92", "S&W M&P"]
_CONDITION = [{"value": "both", "label": "New & used"},
              {"value": "new", "label": "New only"},
              {"value": "used", "label": "Used only"}]
_MAX_AGE = [{"value": "", "label": "Any age"},
            {"value": "3", "label": "Last 3 days"},
            {"value": "7", "label": "Last week"},
            {"value": "30", "label": "Last month"},
            {"value": "90", "label": "Last 3 months"}]
_PRICE_PRESETS = ["25", "50", "75", "100", "150", "200", "300", "500", "1000"]
_CAP_PRESETS = ["5", "10", "15", "20", "30", "40", "50"]


class PartsVertical(Vertical):
    id = "parts"
    label = "Parts"
    path = "/parts"
    relevance_kind = "part"
    default_sort = ("", 1)
    default_view = "grid"       # parts are visual — default to cards

    inputs = [
        InputField("keyword", "Keyword / part name", "text",
                   placeholder="e.g. PMAG, LPVO, buffer tube"),
        InputField("part_category", "Part category", "select", options=PART_CATEGORIES),
        InputField("fitment", "Fits platform", "combo", presets="fitments",
                   placeholder="e.g. AR-15, Glock, 1911"),
        InputField("caliber", "Caliber (if applicable)", "combo", presets="calibers",
                   placeholder="barrels, BCGs, mags…"),
        InputField("manufacturer", "Brand", "combo", presets="manufacturers",
                   placeholder="e.g. Magpul, Holosun"),
        InputField("condition", "Condition", "select", options=_CONDITION,
                   default="both"),
        InputField("price", "Price ($)", "range",
                   min_id="price_min", max_id="price_max", num_presets=_PRICE_PRESETS),
        InputField("capacity", "Capacity (magazines)", "range",
                   min_id="capacity_min", max_id="capacity_max", num_presets=_CAP_PRESETS),
        InputField("max_age_days", "Max listing age", "select", options=_MAX_AGE),
        InputField("in_stock_only", "In stock only", "checkbox", default=False),
    ]

    columns = [
        Column("image", "", sortable=False, render="image", grid="image"),
        Column("title", "Listing", render="title", grid="title"),
        Column("manufacturer", "Brand"),
        Column("part_category", "Category", render="chip", grid="chip"),
        Column("fitment_canon", "Fits", render="chip", grid="chip"),
        Column("caliber", "Caliber"),
        Column("condition", "Cond."),
        Column("price", "Price", render="price", grid="price"),
        Column("in_stock", "Stock", render="stock", grid="badge"),
        Column("posted_at", "Listed", render="listed", grid="meta"),
        Column("site", "Site", render="site", grid="meta"),
    ]

    # ---- stats ----
    stats_default_group = "part_category"
    stats_price_col = "price"
    stats_price_label = "price"
    stats_quality_fields = (
        ("part_category", "category"), ("fitment_canon", "fitment"),
        ("manufacturer", "brand"), ("caliber_canon", "caliber"),
    )

    # ---- behavior ----
    def classify_kind(self, listing) -> str:
        return partsparse.classify(listing.title,
                                   (listing.extra or {}).get("kind") or "")

    def enrich(self, listing) -> None:
        partsparse.enrich(listing)

    def passes(self, c, listing) -> bool:
        a = listing.attributes or {}
        pc = c.f("part_category")
        if pc and a.get("part_category") and a["part_category"] != pc:
            return False
        fit, have_fit = c.f("fitment"), (a.get("fitment_canon") or "")
        if fit and have_fit and fit.lower() not in have_fit.lower():
            return False
        if c.caliber and not calibers.match(c.caliber, listing.caliber):
            return False
        cap = first_int(listing.capacity) or a.get("capacity_rounds")
        if cap is not None:
            cmin, cmax = c.capacity_min, c.capacity_max
            if cmin is not None and cap < cmin:
                return False
            if cmax is not None and cap > cmax:
                return False
        return True

    def presets(self) -> dict:
        return {"calibers": calibers.CANONICAL,
                "manufacturers": partsparse._PARTS_BRANDS,
                "fitments": FITMENTS}


register(PartsVertical())
