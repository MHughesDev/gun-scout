"""Best-effort extraction of ammo fields from listing titles like
'Federal American Eagle 9mm Luger 115gr FMJ 50 Rounds' or
'Winchester 12 Gauge 2-3/4" #7.5 Shot 25 Rounds'.

Mirrors clients/titleparse.py (the guns enricher): fill only blank fields,
canonical caliber via clients/calibers.py (shared across verticals). The
classifier decides whether a listing is loaded ammunition (kept in the Ammo
engine) vs. a gun / reloading component / accessory (dropped).

NOTE: precision is a first pass — tune the vocab/guards against live provider
data once ammo clients exist (same iterative path the gun titleparse took).
"""
import re

from . import calibers

# ---- bullet weight (grain) ------------------------------------------------
# '115gr', '124 grain', '55 gr.', 'Gr 150'. Sane range 15–800 gr keeps out
# round counts and prices that happen to sit near 'gr'.
_GRAIN_RE = re.compile(r"(\d{2,3})\s*(?:gr\b|grains?\b)", re.I)


def grain(title: str):
    for m in _GRAIN_RE.finditer(title or ""):
        v = int(m.group(1))
        if 15 <= v <= 800:
            return v
    return None


# ---- bullet / projectile type --------------------------------------------
# Canonical labels; order matters (longest / most specific first).
_BULLET_TYPES = [
    (r"jacketed\s*hollow\s*point|jhp|sjhp\b", "JHP"),
    (r"hollow\s*point|hp\b", "HP"),
    (r"full\s*metal\s*jacket|fmj|tmj|cmj|fmc\b", "FMJ"),
    (r"soft\s*point|jsp|sp\b", "SP"),
    (r"hpbt|boat\s*tail\s*hollow\s*point|otm|open\s*tip\s*match", "match"),
    (r"ballistic\s*tip|polymer\s*tip|v-?max|z-?max|eld|sst|accubond|interlock|gmx", "ballistic-tip"),
    (r"frangible", "frangible"),
    (r"lead\s*round\s*nose|lrn|lswc|l-?rn|lead\b", "lead"),
    (r"buck\s*shot|buckshot|00\s*buck|#\s*\d{1,2}\s*buck", "buckshot"),
    (r"bird\s*shot|birdshot|#\s*\d{1,2}\s*shot", "birdshot"),
    (r"slugs?\b", "slug"),
]
_BULLET_RE = [(re.compile(p, re.I), label) for p, label in _BULLET_TYPES]


def bullet_type(title: str) -> str:
    t = title or ""
    for rx, label in _BULLET_RE:
        if rx.search(t):
            return label
    return ""


# ---- round count ----------------------------------------------------------
# '50 Rounds', '20rds', 'Box of 20', 'Case of 1000', '1000 ct', '- 50 Pack'.
# Guards: not the caliber number, not grain. Range 1–20000.
_COUNT_RE = re.compile(
    r"(?:box\s*of|case\s*of|pack\s*of)\s*(\d{1,5})"
    r"|(\d{1,5})\s*(?:rounds?\b|rds?\b|ct\b|count\b|cartridges?\b|shells?\b|/\s*box|-?\s*pack\b)",
    re.I)


def round_count(title: str):
    for m in _COUNT_RE.finditer(title or ""):
        v = int(m.group(1) or m.group(2))
        if 1 <= v <= 20000:
            return v
    return None


# ---- case material --------------------------------------------------------
_CASE_RE = [
    (re.compile(r"\bnickel(?:[\s-]*plated)?\b", re.I), "nickel"),
    (re.compile(r"\bbrass\b", re.I), "brass"),
    (re.compile(r"\bsteel(?:[\s-]*case)?\b", re.I), "steel"),
    (re.compile(r"\balumin(?:i)?um\b", re.I), "aluminum"),
]


def case_material(title: str) -> str:
    for rx, label in _CASE_RE:
        if rx.search(title or ""):
            return label
    return ""


# ---- manufacturer / brand -------------------------------------------------
_AMMO_BRANDS = [
    "Federal", "American Eagle", "Winchester", "Remington", "Hornady", "CCI",
    "PMC", "Blazer", "Speer", "Sellier & Bellot", "S&B", "Fiocchi", "Magtech",
    "Wolf", "Tula", "TulAmmo", "Sig Sauer", "Norma", "Nosler", "Barnes",
    "Aguila", "Armscor", "Prvi Partizan", "PPU", "Igman", "Underwood",
    "Buffalo Bore", "Black Hills", "Gold Dot", "Lake City", "Fenix",
    "Browning", "Weatherby", "Independence", "Monarch", "Estate", "Rio",
    "Freedom Munitions", "SGAmmo", "AmmoInc", "Ammo Inc", "Streak",
]
_BRAND_RE = [(re.compile(r"\b" + re.escape(b).replace(r"\ ", r"\s+") + r"\b", re.I), b)
             for b in _AMMO_BRANDS]


def manufacturer(title: str) -> str:
    best = None
    for rx, name in _BRAND_RE:
        m = rx.search(title or "")
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), name)
    return best[1] if best else ""


# ---- caliber (reuse the shared table) -------------------------------------

def caliber_name(title: str) -> str:
    span = calibers.find_span(title or "")
    if span:
        return (title or "")[span[0]:span[1]].strip()
    return ""


# ---- relevance classification ---------------------------------------------
# Drop reloading components and non-ammo goods; keep loaded cartridges/shells.
_NOT_AMMO = re.compile(
    r"\b(reloading|reload\b|projectiles?|once[\s-]*fired|primers?|"
    r"smokeless\s*powder|bullet\s*mold|unprimed|reconditioned\s*brass|"
    r"brass\s*cases?\b|magazines?\b|speed\s*loader|ammo\s*can|ammo\s*box|"
    r"ammunition\s*can|dry\s*box|ammo\s*wallet|reloader|die\s*set)\b", re.I)
# 'Bullets' as the product noun usually means reloading projectiles, not loaded
# ammo — soft signal, only decisive when no round-count/'rounds' is present.
_BULLETS_NOUN = re.compile(r"\bbullets\b", re.I)
_AMMO_POS = re.compile(
    r"\b(ammo|ammunition|cartridges?|rounds?\b|rds?\b|shells?\b|buck\s*shot|"
    r"buckshot|bird\s*shot|birdshot|fmj|jhp|jsp|tmj|hpbt|blank[s]?)\b", re.I)


def is_ammo(title: str, hint: str = "") -> bool:
    t = title or ""
    if _NOT_AMMO.search(t):
        return False
    has_caliber = bool(calibers.find_span(t))
    positive = bool(_AMMO_POS.search(t)) or round_count(t) is not None
    if _BULLETS_NOUN.search(t) and round_count(t) is None:
        return False  # 'XTP Bullets' with no count = reloading projectiles
    if hint in ("ammo", "ammunition"):
        return True
    return has_caliber and positive


def classify(title: str, hint: str = "") -> str:
    return "ammo" if is_ammo(title, hint) else "other"


# ---- enrichment (called from the Ammo vertical) ---------------------------

def enrich(listing) -> None:
    t = listing.title or ""
    attrs = listing.attributes
    if not listing.caliber:
        listing.caliber = caliber_name(t)
    if not listing.manufacturer:
        listing.manufacturer = manufacturer(t)
    if attrs.get("grain") in (None, ""):
        g = grain(t)
        if g:
            attrs["grain"] = g
    if not attrs.get("bullet_type"):
        bt = bullet_type(t)
        if bt:
            attrs["bullet_type"] = bt
    if attrs.get("round_count") in (None, ""):
        rc = round_count(t)
        if rc:
            attrs["round_count"] = rc
    if not attrs.get("case_material"):
        cm = case_material(t)
        if cm:
            attrs["case_material"] = cm
    # cost-per-round: statstore._cpr also computes this, but set it here when possible
    # so the runtime Listing (used by sorting/filters pre-store) has it too
    rc = attrs.get("round_count")
    if listing.price and rc and not attrs.get("price_per_round"):
        try:
            attrs["price_per_round"] = round(listing.price / int(rc), 4)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
