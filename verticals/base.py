"""Vertical descriptors — one per search engine (guns, parts, ammo).

A Vertical fully describes an engine: its search-form inputs, its result
columns, its relevance rule, its enrichment pipeline, its extra filter
predicates, and its stats config. The backend request path and the frontend
render path both read from the same descriptor, so form / filters / columns
can't drift apart. See search_engines_spec.md §2.
"""
from dataclasses import dataclass, field
from typing import Optional

VERTICALS: dict[str, "Vertical"] = {}
_ORDER = ["guns", "parts", "ammo"]


def register(v: "Vertical") -> "Vertical":
    VERTICALS[v.id] = v
    return v


def get(vid: str) -> "Vertical":
    """The vertical for an id, defaulting to guns for anything unknown."""
    return VERTICALS.get(vid or "guns") or VERTICALS["guns"]


def all_verticals() -> list["Vertical"]:
    ordered = [VERTICALS[k] for k in _ORDER if k in VERTICALS]
    ordered += [v for k, v in VERTICALS.items() if k not in _ORDER]
    return ordered


def first_int(s):
    """Leading integer in a string ('15+1' -> 15, '6-shot' -> 6), else None."""
    num = ""
    for ch in str(s or ""):
        if ch.isdigit():
            num += ch
        elif num:
            break
    return int(num) if num else None


# --------------------------------------------------------------------------
# UI schema primitives — serialized to /api/<vertical>/schema and consumed by
# the shared frontend form + column renderers.
# --------------------------------------------------------------------------

@dataclass
class InputField:
    id: str                        # criteria key the value is sent under
    label: str
    type: str = "text"             # text | select | combo | range | checkbox
    presets: Optional[str] = None  # key into presets() dict (combo/select)
    placeholder: str = ""
    default: object = None
    options: Optional[list] = None            # select: [{"value","label"}] or [str,…]
    min_id: Optional[str] = None              # range: the two criteria keys it writes
    max_id: Optional[str] = None
    min_placeholder: str = "min"
    max_placeholder: str = "max"
    num_presets: Optional[list] = None        # range: quick-pick values (both min & max)
    note: str = ""

    def to_dict(self) -> dict:
        d = {"id": self.id, "label": self.label, "type": self.type}
        for k in ("presets", "placeholder", "default", "options", "min_id",
                  "max_id", "num_presets", "note"):
            v = getattr(self, k)
            if v not in (None, ""):
                d[k] = v
        if self.type == "range":
            d["min_placeholder"] = self.min_placeholder
            d["max_placeholder"] = self.max_placeholder
        return d


@dataclass
class Column:
    key: str
    label: str
    sortable: bool = True
    render: str = "text"   # text|price|pricePerRound|image|ends|listed|site|badge|chip|stock|num|inches
    grid: str = ""         # role in a grid card: title|price|meta|badge|chip|image
    suffix: str = ""       # display suffix, e.g. '"' barrel, ' gr' grain

    def to_dict(self) -> dict:
        d = {"key": self.key, "label": self.label, "sortable": self.sortable,
             "render": self.render}
        if self.grid:
            d["grid"] = self.grid
        if self.suffix:
            d["suffix"] = self.suffix
        return d


# --------------------------------------------------------------------------
# Vertical base class
# --------------------------------------------------------------------------

class Vertical:
    id: str = "base"
    label: str = "Base"
    path: str = "/"
    relevance_kind: str = ""       # the `kind` value that means "belongs here"
    inputs: list = []
    columns: list = []
    default_sort: tuple = ("", 1)  # (column key, dir) — dir 1 asc / -1 desc; '' = as received
    default_view: str = "table"

    # ---- behavior (overridden per vertical) ------------------------------

    def classify_kind(self, listing) -> str:
        """Return this listing's relevance class in this vertical's vocabulary
        (guns: firearm|accessory; parts: part|other; ammo: ammo|other). Stored
        in listings.kind and used by stats + relevance()."""
        raise NotImplementedError

    def relevance(self, listing) -> bool:
        """Does this listing belong in this engine? Always enforced at search
        time (no user toggle)."""
        return self.classify_kind(listing) == self.relevance_kind

    def enrich(self, listing) -> None:
        """Fill blank descriptive fields from the title (gap-fill only)."""

    def passes(self, criteria, listing) -> bool:
        """Vertical-specific filter predicates. The universal ones
        (relevance/condition/price/age/manufacturer/stock) run in
        clients.base._passes_common before this is called."""
        return True

    def presets(self) -> dict:
        """Dropdown preset values keyed by InputField.presets."""
        return {}

    def registry(self) -> dict:
        from clients import REGISTRIES
        return REGISTRIES.get(self.id, {})

    # ---- stats config ----------------------------------------------------
    # stats.py reads these to stay vertical-agnostic.
    stats_default_group: str = "manufacturer"
    stats_price_col: str = "price"          # 'price_per_round' for ammo
    stats_price_label: str = "price"
    stats_quality_fields: tuple = ()        # (column, label) pairs for fill-rate report

    def stats_groupers(self) -> dict:
        """name -> callable(sqlite row)->group key. Provided by stats.py's
        shared groupers plus any vertical-specific ones."""
        return {}

    # ---- serialization ---------------------------------------------------

    def schema(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "path": self.path,
            "inputs": [f.to_dict() for f in self.inputs],
            "columns": [c.to_dict() for c in self.columns],
            "presets": self.presets(),
            "defaults": {
                "sort": {"key": self.default_sort[0], "dir": self.default_sort[1]},
                "view": self.default_view,
            },
        }
