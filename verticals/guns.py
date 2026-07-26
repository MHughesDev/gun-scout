"""Guns vertical — the original engine, restated as a descriptor.

Reproduces exactly the search form, result columns, and filter behavior the
app already had, now sourced from one place so the shared shell can render it.
"""
from clients import titleparse, calibers  # noqa: F401  (loaded before verticals)
from .base import Vertical, InputField, Column, register, first_int

# Preset dropdown values (moved here from app.py).
MANUFACTURERS = [
    "GLOCK", "Smith & Wesson", "Sig Sauer", "Ruger", "Springfield Armory",
    "CZ", "Taurus", "Beretta", "Walther", "Heckler & Koch", "FN", "Colt",
    "Kimber", "Canik", "IWI", "Kel-Tec", "SCCY", "Hi-Point", "Staccato",
    "Shadow Systems", "Dan Wesson", "Wilson Combat", "EAA", "Girsan",
    "Rock Island Armory", "Bond Arms", "Heritage", "Rossi", "Charter Arms",
    "North American Arms", "Mossberg", "Remington", "Savage", "Winchester",
    "Browning", "Benelli", "Stoeger", "Henry", "Marlin", "Tikka", "Bergara",
    "Christensen Arms", "Weatherby", "Howa", "Daniel Defense", "BCM",
    "Aero Precision", "Palmetto State Armory", "Anderson Manufacturing",
    "Diamondback", "Radical Firearms", "Zastava", "Century Arms", "Maxim Defense",
]
ACTIONS = [
    "Semi auto", "Bolt action", "Lever action", "Pump action", "Revolver",
    "Single shot", "Over/under", "Side by side", "Muzzleloader",
]

_BARREL_PRESETS = ["3", "3.5", "4", "4.5", "5", "5.5", "6", "7.5", "8", "10.5",
                   "14.5", "16", "18", "20", "22", "24", "26"]
_PRICE_PRESETS = ["200", "300", "400", "500", "600", "750", "1000", "1250",
                  "1500", "2000", "2500", "3000", "5000"]
_CAP_PRESETS = ["5", "6", "7", "8", "10", "12", "15", "17", "20", "30"]

_CONDITION = [{"value": "both", "label": "New & used"},
              {"value": "new", "label": "New only"},
              {"value": "used", "label": "Used only"}]
_LISTING_TYPE = [{"value": "any", "label": "Fixed & auction"},
                 {"value": "fixed", "label": "Fixed price only"},
                 {"value": "auction", "label": "Auctions only"}]
_MAX_AGE = [{"value": "", "label": "Any age"},
            {"value": "1", "label": "Last 24 hours"},
            {"value": "3", "label": "Last 3 days"},
            {"value": "7", "label": "Last week"},
            {"value": "14", "label": "Last 2 weeks"},
            {"value": "30", "label": "Last month"},
            {"value": "90", "label": "Last 3 months"}]


class GunsVertical(Vertical):
    id = "guns"
    label = "Guns"
    path = "/"
    relevance_kind = "firearm"
    default_sort = ("", 1)      # as received
    default_view = "table"

    inputs = [
        InputField("keyword", "Keyword / model", "text",
                   placeholder="e.g. Glock 19, CZ 75, 10/22"),
        InputField("condition", "Condition", "select", options=_CONDITION,
                   default="both"),
        InputField("listing_type", "Listing type", "select", options=_LISTING_TYPE,
                   default="any"),
        InputField("max_age_days", "Max listing age", "select", options=_MAX_AGE,
                   default="30"),
        InputField("action", "Action type", "combo", presets="actions",
                   placeholder="any"),
        InputField("manufacturer", "Manufacturer", "combo", presets="manufacturers",
                   placeholder="e.g. GLOCK, Smith & Wesson"),
        InputField("caliber", "Caliber", "combo", presets="calibers",
                   placeholder="e.g. 9mm Luger"),
        InputField("barrel", "Barrel length (in)", "range",
                   min_id="barrel_min", max_id="barrel_max", num_presets=_BARREL_PRESETS),
        InputField("price", "Price ($)", "range",
                   min_id="price_min", max_id="price_max", num_presets=_PRICE_PRESETS),
        InputField("capacity", "Capacity (rounds)", "range",
                   min_id="capacity_min", max_id="capacity_max", num_presets=_CAP_PRESETS),
    ]

    columns = [
        Column("image", "", sortable=False, render="image", grid="image"),
        Column("title", "Listing", render="title", grid="title"),
        Column("manufacturer", "Mfr"),
        Column("model", "Model"),
        Column("caliber", "Caliber"),
        Column("action", "Action"),
        Column("barrel_length", "Barrel", render="inches", suffix='"'),
        Column("capacity", "Cap."),
        Column("condition", "Cond."),
        Column("price", "Price", render="price", grid="price"),
        Column("ends_at", "Ends", render="ends", grid="meta"),
        Column("posted_at", "Listed", render="listed", grid="meta"),
        Column("site", "Site", render="site", grid="meta"),
    ]

    # ---- stats ----
    stats_default_group = "caliber"
    stats_price_col = "price"
    stats_price_label = "price"
    stats_quality_fields = (
        ("caliber_canon", "caliber"), ("manufacturer", "manufacturer"),
        ("action", "action"), ("gun_type", "gun_type"), ("model", "model"),
        ("capacity_rounds", "capacity"),
    )

    # ---- behavior ----
    def classify_kind(self, listing) -> str:
        return titleparse.classify(listing.title,
                                   (listing.extra or {}).get("kind") or "")

    def enrich(self, listing) -> None:
        titleparse.enrich(listing)

    def passes(self, c, listing) -> bool:
        if c.caliber and not calibers.match(c.caliber, listing.caliber):
            return False
        if c.action:
            want = titleparse.normalize_action(c.action) or c.action.lower()
            have = (listing.action or "").lower()
            if have and want != have and want not in have:
                return False
        if listing.barrel_length is not None:
            if c.barrel_min is not None and listing.barrel_length < c.barrel_min:
                return False
            if c.barrel_max is not None and listing.barrel_length > c.barrel_max:
                return False
        cap = first_int(listing.capacity)
        if cap is not None:
            if c.capacity_min is not None and cap < c.capacity_min:
                return False
            if c.capacity_max is not None and cap > c.capacity_max:
                return False
        return True

    def presets(self) -> dict:
        return {"calibers": calibers.CANONICAL, "manufacturers": MANUFACTURERS,
                "actions": ACTIONS}


register(GunsVertical())
