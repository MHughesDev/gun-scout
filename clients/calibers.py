"""Canonical caliber names + alias resolution.

Sites write the same chambering many ways ('.300 Win Mag', '300 WM',
'9MM LUGER (9X19 PARA)'...). One table maps them all to a canonical name so
filtering matches across sites and analytics can group listings by
caliber_canon. Matching is done on normalized text (lowercase alphanumerics)
with longest-alias-first, so '9mm makarov' wins over the bare '9mm' inside it.
"""

# canonical name -> aliases (any casing/punctuation; compared normalized).
# The canonical name itself is always also an alias, so an entry only needs the
# OTHER spellings a site/user might use. Keep new aliases distinctive (avoid
# bare 2-3 digit numbers) — canonical() matches by substring, longest-first, so
# a caliber's own full form disambiguates it from shorter fragments it contains.
ALIASES: dict[str, list[str]] = {
    # ---- rimfire ----
    ".22 LR":          ["22 long rifle", "22lr", "22 caliber"],
    ".22 Short":       ["22 short"],
    ".22 Long":        ["22 long"],
    ".22 WMR":         ["22 mag", "22 magnum", "22 wmr", "22 win mag",
                        "22 winchester magnum"],
    ".22 WRF":         ["22 wrf", "22 rem special"],
    ".17 HMR":         ["17hmr", "17 hmr"],
    ".17 Mach 2":      ["17 mach 2", "17 hm2", "17hm2"],
    ".17 WSM":         ["17 wsm", "17win super mag"],
    ".5mm Rem Mag":    ["5mm rem mag", "5mm remington"],
    # ---- handgun ----
    ".25 ACP":         ["25 auto", "25 acp", "6.35mm"],
    ".32 ACP":         ["32 auto", "32 acp", "7.65mm browning"],
    ".32 S&W Long":    ["32 s&w long", "32 long colt"],
    ".32 H&R Magnum":  ["32 h&r", "32 hr mag"],
    ".327 Federal Magnum": ["327 federal", "327 fed mag"],
    ".32-20 WCF":      ["32-20", "32 wcf", "32-20 winchester"],
    ".380 ACP":        ["380 auto", "9mm kurz", "9x17", "380"],
    "9mm Luger":       ["9x19", "9mm para", "9mm nato", "9 mm", "9mm"],
    "9mm Makarov":     ["9x18", "makarov"],
    "9x21":            ["9x21"],
    "9x23 Winchester": ["9x23 win", "9x23 winchester", "9x23"],
    "7.62x25 Tokarev": ["762x25", "7.62 tokarev", "30 tokarev"],
    ".38 Special":     ["38 spl", "38 spc", "38 special", "38 s&w special",
                        "38 smith & wesson special"],
    ".38 Super":       ["38 super", "38 super auto"],
    ".38 S&W":         ["38 s&w", "38 smith wesson"],
    ".38-40 WCF":      ["38-40", "38 wcf", "38-40 winchester"],
    ".357 Magnum":     ["357 mag", "357 magnum"],
    ".357 SIG":        ["357sig", "357 sig"],
    ".357 Maximum":    ["357 max", "357 maximum"],
    ".40 S&W":         ["40 sw", "40 smith & wesson"],
    "10mm Auto":       ["10mm"],
    ".41 Magnum":      ["41 mag", "41 rem mag", "41 magnum", "41 remington magnum"],
    ".44 Special":     ["44 spl", "44 special", "44 s&w special",
                        "44 smith & wesson special"],
    ".44 Magnum":      ["44 mag", "44 rem mag", "44 magnum", "44 remington magnum"],
    ".44-40 WCF":      ["44-40", "44 wcf", "44-40 winchester"],
    ".45 ACP":         ["45 auto", "45acp"],
    ".45 GAP":         ["45 gap"],
    ".45 Colt":        ["45 long colt", "45 lc", "45 colt"],
    ".454 Casull":     ["454 casull"],
    ".460 S&W Magnum": ["460 s&w", "460 sw mag", "460 smith wesson"],
    ".475 Linebaugh":  ["475 linebaugh"],
    ".480 Ruger":      ["480 ruger"],
    ".500 S&W Magnum": ["500 s&w", "500 sw mag", "500 smith wesson"],
    ".50 AE":          ["50 action express", "50ae"],
    ".50 GI":          ["50 gi"],
    ".30 Super Carry": ["30 sc", "30 super carry"],
    ".30 Carbine":     ["30 carbine", "30 m1 carbine", "m1 carbine"],
    "5.7x28mm":        ["5.7x28", "57x28", "5.7"],
    "4.6x30mm":        ["4.6x30"],
    # ---- rifle: varmint / small bore ----
    ".17 Remington":   ["17 rem", "17 remington"],
    ".17 Hornet":      ["17 hornet"],
    ".204 Ruger":      ["204 ruger", "204ruger"],
    ".22 Hornet":      ["22 hornet"],
    ".218 Bee":        ["218 bee"],
    ".220 Swift":      ["220 swift"],
    ".221 Fireball":   ["221 fireball"],
    ".222 Remington":  ["222 rem", "222 remington"],
    ".223 Remington":  ["223 rem", "223"],
    "5.56 NATO":       ["5.56x45", "556 nato", "5.56", "223 wylde"],
    ".224 Valkyrie":   ["224 valkyrie", "224 val"],
    ".22-250":         ["22-250 rem", "22-250 remington"],
    ".243 Winchester": ["243 win", "243"],
    ".243 WSSM":       ["243 wssm"],
    ".240 Weatherby":  ["240 wby", "240 weatherby"],
    "6mm Remington":   ["6mm rem", "244 remington"],
    "6mm Creedmoor":   ["6mm creedmoor", "6 creedmoor"],
    "6mm ARC":         ["6mm arc", "6 arc"],
    "6mm BR":          ["6mm br", "6mm benchrest"],
    ".25-20 WCF":      ["25-20", "25-20 winchester"],
    ".25-06 Remington": ["25-06", "25-06 rem"],
    ".250 Savage":     ["250 savage", "250-3000"],
    ".257 Roberts":    ["257 roberts", "257 bob"],
    ".257 Weatherby":  ["257 wby", "257 weatherby"],
    ".25 WSSM":        ["25 wssm"],
    # ---- rifle: 6.5 / 6.8 ----
    "6.5 Creedmoor":   ["65 creedmoor", "6.5 cm", "6.5 creed"],
    "6.5 Grendel":     ["65 grendel", "6.5 grendel"],
    "6.5 PRC":         ["65prc", "6.5 prc"],
    "6.5x55 Swedish":  ["6.5x55", "6.5 swede", "6.5 swedish"],
    "6.5x284 Norma":   ["6.5x284", "6.5-284"],
    ".260 Remington":  ["260 rem", "260 remington"],
    ".264 Win Mag":    ["264 win mag", "264 winchester magnum"],
    "6.8 SPC":         ["68spc", "6.8 spc", "6.8 rem spc"],
    "6.8 Western":     ["6.8 western"],
    # ---- rifle: .270 / 7mm ----
    ".270 Winchester": ["270 win", "270"],
    ".270 WSM":        ["270 wsm"],
    ".270 Weatherby":  ["270 wby", "270 weatherby"],
    ".277 Fury":       ["277 fury", "277 sig fury", "277 sig"],
    ".280 Remington":  ["280 rem", "280 remington"],
    ".280 Ackley Improved": ["280 ackley", "280 ai"],
    "7x57 Mauser":     ["7x57", "7mm mauser", "275 rigby"],
    "7x64 Brenneke":   ["7x64"],
    "7mm-08":          ["7mm08", "7mm-08 rem"],
    "7mm Rem Mag":     ["7mm remington magnum", "7mm rem mag", "7mm rm"],
    "7mm PRC":         ["7prc", "7mm prc"],
    "7mm Backcountry": ["7mm bc", "7 backcountry"],
    "7mm STW":         ["7mm stw", "7 stw"],
    "7mm Weatherby":   ["7mm wby", "7mm weatherby"],
    "7mm RUM":         ["7mm rum", "7mm rem ultra"],
    ".284 Winchester": ["284 win", "284 winchester"],
    # ---- rifle: .30 cal ----
    ".30-06 Springfield": ["30-06", "3006"],
    ".30-30 Winchester": ["30-30", "3030 win"],
    ".30-40 Krag":     ["30-40 krag", "30-40", "3040 krag"],
    ".300 Savage":     ["300 savage", "300 sav"],
    ".308 Winchester": ["308 win", "7.62x51", "762 nato", "308"],
    ".308 Marlin Express": ["308 marlin", "308 mx"],
    ".300 Win Mag":    ["300 winchester magnum", "300 win mag", "300 wm", "300wm"],
    ".300 WSM":        ["300 wsm"],
    ".300 Blackout":   ["300 blk", "300 aac", "300 blackout"],
    "8.6 Blackout":    ["8.6 blk", "86 blackout"],
    ".300 HAM'R":      ["300 hamr", "300 ham'r"],
    ".300 PRC":        ["300prc", "300 prc"],
    ".300 RUM":        ["300 rum", "300 rem ultra"],
    ".300 H&H Magnum": ["300 h&h", "300 hh mag"],
    ".300 Weatherby":  ["300 wby", "300 weatherby"],
    ".303 British":    ["303 british", "303 brit", "303"],
    ".303 Savage":     ["303 savage"],
    ".307 Winchester": ["307 win", "307 winchester"],
    "7.62x39":         ["762x39"],
    "7.62x54R":        ["7.62x54r", "762x54r", "7.62x54", "7.62 russian"],
    "7.5x55 Swiss":    ["7.5x55", "7.5 swiss", "gp11"],
    "7.5x54 French":   ["7.5x54", "7.5 french"],
    "6.5x52 Carcano":  ["6.5 carcano", "6.5x52"],
    "6.5x50 Japanese": ["6.5 jap", "6.5x50"],
    "7.7 Japanese":    ["7.7x58", "7.7 jap"],
    "5.45x39":         ["545x39"],
    # ---- rifle: 8mm / .32 / .33 / .35 ----
    "8mm Mauser":      ["8mm mauser", "8x57", "7.92x57", "8mm"],
    ".32 Winchester Special": ["32 win special", "32 win spl", "32 special"],
    ".32-40 Winchester": ["32-40"],
    ".325 WSM":        ["325 wsm"],
    ".26 Nosler":      ["26 nosler"],
    ".27 Nosler":      ["27 nosler"],
    ".28 Nosler":      ["28 nosler"],
    ".30 Nosler":      ["30 nosler"],
    ".33 Nosler":      ["33 nosler"],
    ".338 Lapua":      ["338 lapua magnum", "338 lapua", "338 lm"],
    ".338 Win Mag":    ["338 win mag", "338 winchester magnum"],
    ".338 Federal":    ["338 federal", "338 fed"],
    ".338 RUM":        ["338 rum", "338 rem ultra"],
    ".338-06":         ["338-06"],
    ".340 Weatherby":  ["340 wby", "340 weatherby"],
    ".35 Remington":   ["35 rem", "35 remington"],
    ".35 Whelen":      ["35 whelen"],
    ".350 Legend":     ["350legend", "350 legend"],
    ".400 Legend":     ["400 legend", "400 lgnd"],
    ".350 Rem Mag":    ["350 rem mag", "350 remington magnum"],
    ".356 Winchester": ["356 win"],
    ".358 Winchester": ["358 win", "358 winchester"],
    ".360 Buckhammer": ["360 buckhammer", "360 bhmr"],
    # ---- rifle: .375+ / big bore ----
    ".375 H&H Magnum": ["375 h&h", "375 hh mag"],
    ".375 Ruger":      ["375 ruger"],
    ".375 Winchester": ["375 win"],
    ".378 Weatherby":  ["378 wby", "378 weatherby"],
    ".38-55 Winchester": ["38-55"],
    ".404 Jeffery":    ["404 jeffery"],
    ".405 Winchester": ["405 win"],
    ".416 Rigby":      ["416 rigby"],
    ".416 Rem Mag":    ["416 rem mag", "416 remington magnum"],
    ".416 Barrett":    ["416 barrett"],
    ".444 Marlin":     ["444 marlin"],
    ".45-70 Government": ["45-70", "45-70 govt", "4570"],
    ".45-90":          ["45-90"],
    ".450 Bushmaster": ["450 bm", "450 bush", "450 bushmaster"],
    ".450 Marlin":     ["450 marlin"],
    ".458 SOCOM":      ["458socom", "458 socom"],
    ".458 Win Mag":    ["458 win mag", "458 winchester magnum"],
    ".458 Lott":       ["458 lott"],
    ".460 Weatherby":  ["460 wby", "460 weatherby"],
    ".470 Nitro Express": ["470 nitro", "470 ne"],
    "9.3x62":          ["9.3x62"],
    "9.3x74R":         ["9.3x74r", "9.3x74"],
    ".50 Beowulf":     ["50 beowulf", "500 beowulf"],
    ".50 BMG":         ["50bmg", "50 bmg", "12.7x99"],
    # ---- shotgun ----
    "10 Gauge":        ["10 ga", "10ga"],
    "12 Gauge":        ["12 ga", "12ga"],
    "16 Gauge":        ["16 ga", "16ga"],
    "20 Gauge":        ["20 ga", "20ga"],
    "24 Gauge":        ["24 ga", "24 gauge"],
    "28 Gauge":        ["28 ga", "28ga"],
    "32 Gauge":        ["32 ga", "32 gauge"],
    ".410 Bore":       ["410 ga", "410 gauge", "410"],
}

CANONICAL = list(ALIASES)  # dropdown order = table order

# Best TEXT-SEARCH spelling per caliber — what to type into a site's keyword
# box for maximum recall. This is NOT the canonical display name: probed
# 2026-07 on guns.com, '300 win mag' finds 781 listings but '300 wm' finds 8,
# '9mm' finds 16679 but '9mm luger' only 335, '30-06' 1079 but
# '30-06 springfield' 43 — formal qualifiers and abbreviations both lose
# results on text-search sites. Rule of thumb encoded here: the shortest
# spelling dealers actually put in listing titles. Missing entries fall back
# to the canonical name without its leading dot.
SEARCH_TERMS: dict[str, str] = {
    "9mm Luger":          "9mm",
    "9mm Makarov":        "9x18 makarov",
    ".380 ACP":           "380 acp",
    ".45 ACP":            "45 acp",
    ".45 Colt":           "45 colt",
    ".40 S&W":            "40 s&w",
    "10mm Auto":          "10mm",
    ".357 Magnum":        "357 magnum",
    ".357 SIG":           "357 sig",
    ".38 Special":        "38 special",
    ".44 Magnum":         "44 magnum",
    ".30 Super Carry":    "30 super carry",
    "5.7x28mm":           "5.7x28",
    ".22 LR":             "22 lr",
    "5.56 NATO":          "5.56",
    ".223 Remington":     "223",
    ".308 Winchester":    "308",
    ".30-06 Springfield": "30-06",
    ".300 Win Mag":       "300 win mag",
    ".300 Blackout":      "300 blackout",
    ".30-30 Winchester":  "30-30",
    ".270 Winchester":    "270 win",
    ".243 Winchester":    "243 win",
    "7mm Rem Mag":        "7mm rem mag",
    ".338 Lapua":         "338 lapua",
    ".410 Bore":          "410 bore",
    # ---- added 2026-07 (heuristic best-recall spellings; strip the formal
    # qualifier the way the probed originals do — not individually probed) ----
    ".22 WMR":            "22 wmr",
    ".30 Carbine":        "30 carbine",
    ".25-06 Remington":   "25-06",
    ".260 Remington":     "260 rem",
    ".280 Remington":     "280 rem",
    ".257 Roberts":       "257 roberts",
    "6.5x55 Swedish":     "6.5x55",
    ".270 WSM":           "270 wsm",
    ".300 WSM":           "300 wsm",
    ".30-40 Krag":        "30-40 krag",
    ".300 Savage":        "300 savage",
    ".303 British":       "303 british",
    "7.62x54R":           "7.62x54r",
    "8mm Mauser":         "8mm mauser",
    ".32 Winchester Special": "32 win special",
    ".338 Win Mag":       "338 win mag",
    ".35 Remington":      "35 rem",
    ".35 Whelen":         "35 whelen",
    ".375 H&H Magnum":    "375 h&h",
    ".444 Marlin":        "444 marlin",
    ".45-70 Government":  "45-70",
    ".450 Bushmaster":    "450 bushmaster",
    "6.8 SPC":            "6.8 spc",
    ".300 RUM":           "300 rum",
    ".416 Rigby":         "416 rigby",
    ".45 GAP":            "45 gap",
    ".38 Super":          "38 super",
    ".327 Federal Magnum": "327 federal",
    ".41 Magnum":         "41 magnum",
    ".454 Casull":        "454 casull",
    ".500 S&W Magnum":    "500 s&w",
    ".460 S&W Magnum":    "460 s&w",
}


def search_term(text: str) -> str:
    """Best text-search spelling for a user-entered caliber: resolve to
    canonical, then use the probed search spelling. Unrecognized calibers
    pass through unchanged."""
    canon = canonical(text)
    if not canon:
        return (text or "").strip()
    return SEARCH_TERMS.get(canon, canon.lstrip("."))


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


# longest alias first so specific strings win over ones they contain
_LOOKUP: list[tuple[str, str]] = sorted(
    ((_norm(a), canon)
     for canon, aliases in ALIASES.items()
     for a in [canon, *aliases]),
    key=lambda t: -len(t[0]),
)


def canonical(text: str) -> str:
    """Resolve free text to a canonical caliber name ('' if unrecognized)."""
    n = _norm(text)
    if not n:
        return ""
    for alias, canon in _LOOKUP:
        if alias in n:
            return canon
    return ""


def match(wanted: str, actual: str) -> bool:
    """True if a listing's `actual` caliber satisfies the user's `wanted`
    caliber, across the spellings sites use ('.300 Win Mag' matches '300 WM',
    '9mm' matches '9MM LUGER (9X19 PARA)'). Unknown actual caliber = keep it
    (never hide something we can't disprove)."""
    a = _norm(actual)
    if not a:
        return True
    cw, ca = canonical(wanted), canonical(actual)
    if cw and ca:
        return cw == ca
    w = _norm(wanted)  # fall back to loose substring comparison
    return w in a or a in w


# Position-aware caliber matching: one big regex where every alias is spelled
# char-by-char with optional punctuation between characters, so '.300 WIN.
# MAG.' or '30~06' still match, but with \w guards so '308' can't fire inside
# 'Model 1308'. Longest normalized alias first, so at the same start position
# '9mm makarov' wins over the bare '9mm' it contains. Built lazily — it's a
# ~30 KB pattern used only by titleparse.model().
_SPAN_RE = None


def _span_pattern() -> "re.Pattern":
    global _SPAN_RE
    if _SPAN_RE is None:
        import re
        pats = []
        for alias, _canon in _LOOKUP:  # already longest-normalized-first
            if not alias:
                continue
            body = r"[\W_]*".join(re.escape(ch) for ch in alias)
            pats.append(rf"(?<!\w){body}(?!\w)")
        _SPAN_RE = re.compile("|".join(pats), re.I)
    return _SPAN_RE


def find_span(text: str):
    """(start, end) of the first caliber mention in `text`, or None."""
    m = _span_pattern().search(text or "")
    return (m.start(), m.end()) if m else None
