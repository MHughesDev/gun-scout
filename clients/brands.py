"""Manufacturer name aliases.

Sites and users write the same maker many ways ('S&W', 'Smith and Wesson',
'SMITH & WESSON INC'). One table maps them to a canonical brand so the
manufacturer filter matches across sites and queries can use the spelling
with the best recall (the full name).
"""
import re

# canonical brand -> aliases (compared normalized: lowercase alphanumerics)
ALIASES: dict[str, list[str]] = {
    "Smith & Wesson":    ["s&w", "sw", "smith and wesson", "smith wesson"],
    "Sig Sauer":         ["sig", "sig-sauer"],
    "Heckler & Koch":    ["hk", "h&k", "heckler and koch"],
    "Springfield Armory": ["springfield"],
    "GLOCK":             [],
    "CZ":                ["cz-usa", "cz usa", "ceska zbrojovka"],
    "FN":                ["fn america", "fn herstal", "fnh", "fabrique nationale"],
    "IWI":               ["iwi us", "israel weapon industries"],
    "Ruger":             ["sturm ruger", "sturm, ruger & co"],
    "Colt":              ["colts", "colt's manufacturing"],
    "Century Arms":      ["century international arms"],
    "Taurus":            ["taurus international"],
    "Remington":         ["remarms", "rem arms"],
    "Winchester":        ["winchester repeating arms"],
    "Browning":          [],
    "Mossberg":          ["o.f. mossberg", "of mossberg"],
    "Savage":            ["savage arms"],
    "Beretta":           ["beretta usa"],
    "Kimber":            ["kimber mfg"],
    "Walther":           ["walther arms", "carl walther"],
}


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


# exact normalized lookup: 'sw' -> 'Smith & Wesson'
_CANON: dict[str, str] = {}
for canon, aliases in ALIASES.items():
    for a in [canon, *aliases]:
        _CANON[_norm(a)] = canon


def canonical(text: str) -> str:
    """Resolve a user-entered manufacturer to a canonical brand ('' if unknown)."""
    return _CANON.get(_norm(text), "")


def search_term(text: str) -> str:
    """Spelling to put in a site's search box: the full brand name (best
    recall — every site matches 'smith & wesson'; not all match 's&w')."""
    return canonical(text) or (text or "").strip()


def matches(wanted: str, manufacturer: str, title: str = "") -> bool:
    """Does a listing satisfy the user's manufacturer filter? Compares against
    the structured manufacturer field when the site provides one, falling back
    to spotting the brand in the title (some sites never fill the field —
    without the fallback those listings would all be dropped)."""
    w = _norm(wanted)
    if not w:
        return True
    canon = _CANON.get(w)
    variants = [_norm(v) for v in ([canon, *ALIASES[canon]] if canon else [wanted])]
    hay = _norm(manufacturer)
    if hay:
        return any(v in hay or hay in v for v in variants)
    # no structured manufacturer: look for the brand in the title. Substring
    # match only for variants long enough not to fire inside random words;
    # short forms like 'HK'/'CZ' match on word boundaries in the raw text.
    t = _norm(title)
    if any(v in t for v in variants if len(v) >= 4):
        return True
    return bool(re.search(rf"\b{re.escape(wanted.strip())}\b", title or "", re.I))
