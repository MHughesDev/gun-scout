"""Best-effort extraction of parts/accessory fields from listing titles like
'Magpul PMAG 30 AR/M4 GEN M3 5.56 Magazine' or
'Holosun HE507C-GR X2 Green Dot Optic'.

Two parts-specific axes on top of the shared caliber table:
  - part_category: the primary grouping (optic, magazine, trigger, barrel…)
  - fitment_canon: the platform it fits (AR-15, Glock, 1911, AK, Rem 700…)

Relevance (is this a PART?) is the inverse of the guns rule: keep things the
guns engine calls 'accessory' (which includes stripped lowers / BCGs via
titleparse's hard-parts tier), but drop complete firearms AND loaded ammo.

NOTE: vocab/guards are a first pass — tune against live provider data once
parts clients exist.
"""
import re

from . import calibers, titleparse, ammoparse

# ---- part category (ordered: specific first, first match wins) ------------
_CATEGORIES = [
    ("red-dot", r"red\s*dot|reflex\s*sight|holosun|trijicon\s*rmr|\brmr\b|\bsro\b|"
                r"romeo\d|aimpoint|\bmro\b|micro\s*red\s*dot|open\s*reflex|green\s*dot"),
    ("magnifier", r"magnifier"),
    ("scope", r"\bscope\b|\blpvo\b|rifle\s*scope|prism\s*scope|\bacog\b|"
              r"first\s*focal|\bffp\b|\bsfp\b|\d+-\d+x\d+"),
    ("optic-mount", r"scope\s*mount|scope\s*rings?|optic\s*mount|cantilever|\briser\b|"
                    r"picatinny\s*mount|\b30mm\s*mount"),
    ("magazine", r"\bmagazines?\b|\bpmag\b|\bmag\b(?!\s*(?:num|na|azine\b))|drum\s*mag|"
                 r"\b\d+\s*round\s*mag|magpul\s*pmag"),
    ("trigger", r"\btriggers?\b|fire\s*control\s*group|\bfcg\b|flat\s*trigger|"
                r"drop-?in\s*trigger|trigger\s*(?:kit|group|assembly)"),
    ("bcg", r"bolt\s*carrier\s*group|\bbcg\b|carrier\s*group|bolt\s*carrier"),
    ("charging-handle", r"charging\s*handle"),
    ("barrel", r"\bbarrels?\b|\bbbl\b|barreled\s*action"),
    ("handguard", r"hand\s*guards?|\bhandguards?\b|\brail\b|m-?lok|key-?mod|"
                  r"quad\s*rail|free\s*float|\bff\s*rail"),
    ("muzzle-device", r"muzzle\s*(?:brake|device)|compensator|flash\s*(?:hider|"
                      r"suppressor)|\bcomp\b|linear\s*comp"),
    ("suppressor", r"suppressor|silencer"),
    ("stock", r"\bstocks?\b|\bbrace\b|buffer\s*tube|\bchassis\b|butt\s*stock|"
              r"collapsible\s*stock|pistol\s*brace"),
    ("grip", r"\bgrips?\b|foregrip|pistol\s*grip|vertical\s*grip|angled\s*grip|"
             r"grip\s*module"),
    ("sights", r"\bsights?\b|front\s*sight|rear\s*sight|iron\s*sights?|night\s*sights?|"
               r"\bbuis\b|flip-?up\s*sights?"),
    ("lower", r"lower\s*receiver|lower\s*parts?\s*kit|\blpk\b|stripped\s*lower|"
              r"80%?\s*lower|complete\s*lower"),
    ("upper", r"upper\s*receiver|complete\s*upper|stripped\s*upper|upper\s*assembly"),
    ("holster", r"\bholsters?\b|\biwb\b|\bowb\b|\bappendix\b"),
    ("light-laser", r"weapon\s*light|flash\s*light|\btlr-?\d|\bx300\b|\bx300u\b|"
                    r"\blasers?\b|\bipsc\b|streamlight|surefire"),
    ("sling", r"\bslings?\b|sling\s*(?:mount|swivel)"),
    ("bipod", r"\bbipods?\b|\btripods?\b|\bmonopods?\b|shooting\s*rest"),
    ("cleaning", r"cleaning\s*(?:kit|rod|mat)|bore\s*(?:brush|snake|guide)|"
                 r"gun\s*(?:oil|solvent)|patches?\b"),
    ("case-bag", r"\bhard\s*case\b|rifle\s*case|pistol\s*case|range\s*bag|"
                 r"gun\s*bag|soft\s*case"),
    ("small-parts", r"\bsprings?\b|\bpins?\b|detent|safety\s*selector|extractor|"
                    r"firing\s*pin|takedown|buffer\s*spring|gas\s*(?:block|tube|key)"),
]
_CATEGORY_RE = [(slug, re.compile(pat, re.I)) for slug, pat in _CATEGORIES]


def part_category(title: str) -> str:
    t = title or ""
    for slug, rx in _CATEGORY_RE:
        if rx.search(t):
            return slug
    return ""


# ---- fitment / platform ---------------------------------------------------
_FITMENTS = [
    ("AR-10", r"\bar-?10\b|\blr-?308\b|\bsr-?25\b|\.308\s*ar|dpms\s*pattern"),
    ("AR-15", r"\bar-?15\b|\bm4\b|\bm-?4\b|\bm16\b|mil-?spec\s*ar|\bar\s*platform\b|"
              r"\bar\b(?=.*(?:upper|lower|handguard|bcg|buffer|charging))"),
    ("AK", r"\bak-?47\b|\bak-?74\b|\bakm\b|\bak\b(?!\s*=)|saiga|\bfitting?\s*ak"),
    ("Glock", r"\bglock\b|\bg1[7-9]\b|\bg2[0-9]\b|\bg4[0-9]\b|\bg43x?\b|gen\s*[1-5]\s*glock"),
    ("1911", r"\b1911\b|\b2011\b|government\s*model"),
    ("SIG P320", r"\bp320\b|\bp365\b|sig\s*p3"),
    ("SIG P365", r"\bp365\b"),
    ("Rem 700", r"remington\s*700|\brem\s*700\b|\br700\b|700\s*footprint|700\s*pattern"),
    ("Rem 870", r"\b870\b|remington\s*870"),
    ("Mossberg 500", r"mossberg\s*(?:500|590|maverick)|\b500/590\b"),
    ("Ruger 10/22", r"\b10/22\b|\b1022\b|ruger\s*10-22"),
    ("CZ 75", r"\bcz-?75\b|cz\s*p-?10|cz\s*shadow"),
    ("Beretta 92", r"beretta\s*92|\bm9\b|\b92fs\b"),
    ("S&W M&P", r"\bm&p\b|shield\b|smith\s*&\s*wesson\s*m"),
]
_FITMENT_RE = [(name, re.compile(pat, re.I)) for name, pat in _FITMENTS]


def fitment_canon(title: str) -> str:
    t = title or ""
    for name, rx in _FITMENT_RE:
        if rx.search(t):
            return name
    return ""


# ---- brand (parts-maker vocab + gun-brand fallback) -----------------------
_PARTS_BRANDS = [
    "Magpul", "Holosun", "Vortex", "Trijicon", "Aimpoint", "EOTech", "Leupold",
    "Primary Arms", "Sig Sauer", "Streamlight", "SureFire", "Olight",
    "Aero Precision", "BCM", "Geissele", "Radian", "Strike Industries",
    "Midwest Industries", "Daniel Defense", "Timney", "CMC", "LaRue",
    "Nikon", "Burris", "Athlon", "Bushnell", "Sightmark", "Crimson Trace",
    "Wilson Combat", "Ergo", "B5 Systems", "Odin Works", "Faxon", "Ballistic Advantage",
    "Toolcraft", "JP Enterprises", "Rise Armament", "Hiperfire", "Fortis",
    "Blackhawk", "Safariland", "Alien Gear", "Amend2", "Lancer", "ETS",
]
_PARTS_BRAND_RE = [(re.compile(r"\b" + re.escape(b).replace(r"\ ", r"\s+") + r"\b", re.I), b)
                   for b in _PARTS_BRANDS]


def brand(title: str) -> str:
    best = None
    for rx, name in _PARTS_BRAND_RE:
        m = rx.search(title or "")
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), name)
    if best:
        return best[1]
    return titleparse.manufacturer(title or "")


def caliber_name(title: str) -> str:
    span = calibers.find_span(title or "")
    if span:
        return (title or "")[span[0]:span[1]].strip()
    return ""


# ---- relevance ------------------------------------------------------------

def is_part(title: str, hint: str = "") -> bool:
    """A part/accessory: the guns engine's 'accessory' bucket (which includes
    stripped lowers/BCGs), minus loaded ammunition (that's the Ammo engine)."""
    if ammoparse.is_ammo(title, hint):
        return False
    return titleparse.classify(title, hint if hint in ("firearm", "accessory") else "") \
        == "accessory"


def classify(title: str, hint: str = "") -> str:
    return "part" if is_part(title, hint) else "other"


# ---- enrichment (called from the Parts vertical) --------------------------

def enrich(listing) -> None:
    t = listing.title or ""
    attrs = listing.attributes
    if not attrs.get("part_category"):
        pc = part_category(t)
        if pc:
            attrs["part_category"] = pc
    if not attrs.get("fitment_canon"):
        fit = fitment_canon(t)
        if fit:
            attrs["fitment_canon"] = fit
    if not listing.caliber:
        listing.caliber = caliber_name(t)   # barrels, BCGs, magazines are caliber-specific
    if not listing.manufacturer:
        listing.manufacturer = brand(t)
