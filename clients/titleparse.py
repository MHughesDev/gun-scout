"""Best-effort extraction of caliber / barrel length / capacity from listing
titles like 'Glock G19 Gen5 9mm 4.02in Black Pistol - 15+1 Rounds'."""
import re

_CALIBERS = [
    r"9\s?mm", r"10\s?mm", r"5\.7x28", r"7\.62x39", r"7\.62x51", r"5\.56(?:\s?nato)?",
    r"\.?223(?:\s?rem)?", r"\.?308(?:\s?win)?",
    r"\.?300\s?(?:blk|blackout|win\s?mag|prc|wsm|wby)",
    r"6\.5\s?(?:creedmoor|cm|grendel|prc)", r"6\s?mm(?:\s?(?:arc|creedmoor))?",
    r"7\s?mm(?:\s?(?:prc|rem\s?mag|-?08))?", r"\.?45\s?(?:acp|auto|colt|gap)",
    r"\.?40\s?(?:s&w|sw)?\b", r"\.?380(?:\s?acp|\s?auto)?", r"\.?357(?:\s?mag(?:num)?|\s?sig)?",
    r"\.?38\s?(?:spl|special)", r"\.?44\s?mag(?:num)?", r"\.?22\s?(?:lr|wmr|mag|hornet|-250)?",
    r"\.?17\s?hmr", r"12\s?(?:ga(?:uge)?|gauge)", r"20\s?(?:ga(?:uge)?|gauge)",
    r"16\s?(?:ga(?:uge)?|gauge)", r"28\s?(?:ga(?:uge)?|gauge)",
    r"410(?:\s?bore|\s?ga)?", r"\.?30-06", r"\.?30-30", r"\.?25-06", r"\.?35\s?whelen",
    r"\.?270\s?(?:win|wsm)?", r"\.?243\s?win?", r"6\.8(?:\s?spc)?", r"\.?25\s?acp",
    r"\.?32\s?acp", r"\.?338(?:\s?(?:lapua|win\s?mag))?", r"\.?350\s?legend",
    r"\.?450\s?bushmaster", r"\.?458\s?socom", r"\.?50\s?(?:ae|bmg|beowulf)",
]
_CAL_RE = re.compile("|".join(f"(?:{c})" for c in _CALIBERS), re.I)
_BARREL_RE = re.compile(r'(\d{1,2}(?:\.\d{1,3})?)\s*(?:"|”|in\b|inch|-inch)', re.I)
# capacity forms: '15+1', '15 rounds', '15rd', '6-shot', 'capacity: 10',
# '10 round magazine'. Guards: '23rd Anniversary'/'3rd Gen' ordinals, and
# '500 rounds of ammo' (checked by value: no firearm holds >101).
_CAP_RE = re.compile(
    r"(\d{1,3})\s*\+\s*1\b"
    r"|(\d{1,3})[ -]?(?:rounds?\b(?!\s*of\b)|rds?\b(?!\s*(?:gen\b|generation\b|anniversary\b)))"
    r"|(\d{1,3})[ -]?shots?\b"
    r"|capacity[:\s]+(\d{1,3})\b",
    re.I)


def caliber(title: str) -> str:
    m = _CAL_RE.search(title or "")
    return m.group(0).strip() if m else ""


def barrel_length(title: str):
    m = _BARREL_RE.search(title or "")
    if not m:
        return None
    try:
        v = float(m.group(1))
        return v if 1 <= v <= 60 else None
    except ValueError:
        return None


# Hard PART signals: bare receivers, frames, uppers/lowers, BCGs and other
# build components. Legally many of these ARE firearms (a stripped lower is
# serialized), so sites file them under gun categories — which is exactly why
# these must override even a site's own kind='firearm' flag (see classify()).
# No firearm-noun rescue applies: gun.deals appends ', Rifle' to a stripped
# lower's title, and 'Faxon Firearms' contains 'Firearms'.
_HARD_PARTS_RE = re.compile(
    # 'Polymer Frame' is a part — but 'Polymer Frame Pistol' is a pistol whose
    # frame is polymer (dealer-feed boilerplate), so the parts noun must not be
    # immediately followed by a firearm noun
    r"(?:stripped|complete|assembled|billet|forged|80\s?%|polymer|raw)\s+"
    r"(?:pistol\s+|rifle\s+)?(?:lowers?|uppers?|frames?|receivers?)\b"
    r"(?!\s+(?:pistols?|handguns?|rifles?|shotguns?|revolvers?|carbines?)\b)|"
    # 'PSA AR15 Complete MOE EPT SBA5 Lower' — bounded gap, never across a
    # comma or a 'with'/'w/' (a rifle 'complete with upper rail' is a rifle)
    r"\bcomplete\b(?:(?!\bwith\b|\bw/)[^,|]){0,40}?\b(?:lowers?|uppers?)\b|"
    r"\b(?:lowers?|uppers?)\s+receivers?\b|"
    r"receivers?\s+(?:set|only|blank)s?\b|"
    r"\bbolt\s+carrier(?:\s+group)?\b|\bbcg\b|"
    r"\bbarreled\s+(?:action|upper)s?\b|"
    r"\bfire\s+control\s+(?:unit|group)\b|\bfcu\b|"
    r"\bbuild\s+kits?\b|\b80%\s?jigs?\b|"
    r"\bpf(?:940|45|s9|9ss)\b|"          # Polymer80 frame SKUs
    r"\bframe\s+(?:only|kit)s?\b|\bgrip\s+modules?\b|"
    r"\bslide\s+(?:assembl|kit|only|milling)|\bcomplete\s+slides?\b|"
    r"\bupper\s+(?:build|assembl)\w*\b|"
    r"\bparts\s+kits?\b|\bconversion\s+kits?\b",
    re.I)

# Hard accessory signals: the item is made FOR a gun, it never IS one.
# These are terminal — the firearm-word rescue below does not apply
# ('Kydex Holster for Glock 19 Pistol' must stay an accessory even though
# 'Pistol' appears in the title).
_HARD_ACCESSORY_RE = re.compile(
    r"holsters?\b|\bfits\b|"
    # optics that never appear in gun titles ('Co-Witness', 'Night Sight Set';
    # bundles say 'w/ night sights', not the Set form — still guarded)
    r"\bco-?witness\b|(?<!w/\s)(?<!with\s)sights?\s+sets?\b|"
    # airgun family: a 'CO2 BB Pistol' IS a bb gun — the firearm-noun rescue
    # must not save it, so these live in the hard tier
    r"\bco2\b|\bbb\s?(?:guns?|pistols?|rifles?)\b|\bairsoft\b|"
    r"\bair\s?(?:pistols?|rifles?|guns?)\b|"
    # laser/dry-fire trainers are ranges gear, never firearms
    r"laser\s+train(?:er|ing)s?\b|train(?:er|ing)\s+pistols?\b|"
    # a grain weight next to an ammo word is a box of ammunition even when the
    # title names the platform ('Fiocchi Rifle .30-06 165 gr PSP Ammo, 20rds')
    r"\b\d{2,3}\s?gr(?:ain)?s?\b.{0,40}\b(?:ammo|ammunition)\b|"
    r"\b(?:ammo|ammunition)\b.{0,20}\b\d+\s?(?:rds?|rounds?)\b|"
    # reloading bench tools name the cartridge they service ('.30-06 Rifle
    # Taper Crimp Die'), so the platform word must not rescue them
    r"\b(?:siz(?:ing|er)|crimp|trim|seat(?:ing|er)|decapping|expander|body|"
    r"bushing|full-?length)\s+dies?\b|\bdie\s+sets?\b|\b\d-die\b|"
    r"\bneck\s+(?:sizing|expanding)\b|\bshell\s?(?:plates?|holders?)\b|"
    r"\b(?:case|length|chamber|headspace|cartridge)[\s/]*gauges?\b|"
    r"\bcase\s+length\b|\block-?n-?load\b|\blnl\b|\bchamber\s+hones?\b|"
    r"\b(?:reconditioned|reloading|unprimed)\s+brass\b|\bonce[- ]fired\b|"
    r"\bbrass\b[^|]{0,14}\b\d+\s?(?:rds?|rounds?|ct)\b|"
    # reloading-bench / spare-mag brands that never make firearms, anchored
    # at title start
    r"^(?:lee\s+precision|rcbs|redding|frankford\s+arsenal|j\.?\s?dewey|"
    r"mtm\s+case|birchwood\s+casey|tipton|caldwell|wheeler|triple\s?k)\b|"
    # a title ENDING in Receiver is a receiver, even when the model name
    # upstream says Pistol ('GGP Combat Pistol Glock 19 Receiver')
    r"receivers?\s*-?\s*$|"
    # up to two qualifier tokens ride between 'for' and the gun it fits
    # ('Night Sights for Select Glock Pistols', 'Barrel ... for Gen 3-5 Glock')
    r"\bfor\s+(?:[\w.-]+\s+){0,2}(?:glock|sig\b|s&w|smith\s*&\s*wesson|ruger|springfield|taurus|"
    r"cz\b|beretta|walther|kimber|colt\b|hk\b|h&k|mossberg|remington|savage|"
    r"tikka|1911|ar-?15|ar-?10|ak-?47|"
    # 'Sling for Rifle', 'Case for Pistol' — made FOR a gun, never IS one
    r"rifles?\b|pistols?\b|handguns?\b|shotguns?\b|revolvers?\b|carbines?\b)|"
    # bore-cleaning gear names the gun it services ('Rifle/Handgun Bore Rope'),
    # so the firearm-noun rescue must not apply
    r"bore\s?sights?\b|"
    r"\b(?:bore|chamber)\s+(?:mops?|brush(?:es)?|ropes?|guides?|snakes?)\b",
    re.I)

# Soft accessory signals: nouns that name a part/accessory when they are the
# item itself, but also show up in bundle titles ('Savage Axis XP Rifle
# w/ Scope'); a soft match can be rescued by _FIREARM_RESCUE_RE below.
_ACCESSORY_RE = re.compile(
    r"magazine|\bmags?\b\s+for|speed\s?loader|\bslide\b|grip\s?(module|s\b)|"
    r"barrel\s+for|conversion\s+kit|\bco2\b|air\s?(pistol|gun|soft)|\bbb\s?gun|"
    r"\bammo\b|ammunition|\bmunitions\b|(gun|rifle|pistol|shotgun)\s?cases?\b|"
    r"range\s+bag|pistol\s?grips?\b|\bsafe\b|cleaning|"
    r"\bsights?\s+for\b|"
    r"optic\s+(cut|plate)|trigger\s+(kit|assembly|shoe)|recoil\s+spring|"
    r"frame\s+(only|kit)|lower\s+parts|compensator|guide\s?rod|magwell|"
    r"mag\s?(extension|release|pouch)|base\s?(pad|plate)|firing\s?pin|"
    r"(?<!with\s)(?<!w/\s)\bdrums?\b|"
    # 'Blue Mag Glock 19' but not '…includes 3 Mags', '300 Win Mag',
    # '.300 Wby Mag', '460 S&W Mag' (fixed-width lookbehinds per caliber word)
    r"(?<!\d\s)(?<!win\s)(?<!wby\s)(?<!s&w\s)(?<!erby\s)(?<!rem\s)(?<!h&h\s)(?<!apua\s)\bmag\b(?!s)|"
    r"barrel\s*-?\s*$|"        # title ends in 'Barrel' = a barrel, not a gun
    r"\bp(?:ro)?mags?\b|parts\s+kit|(stripped|assembled)\s+(pistol\s+)?frame|"
    r"\bdies?\b|carrier\s+assembl|\bmandrels?\b|"
    r"frame\s+for|\btriggers?\b|\blumens?\b|"
    r"light\s*/?\s*laser|laser\s+(combo|sight)|"
    # optics & mounts
    r"(?<!with\s)(?<!w/\s)(?<!\+\s)\bscopes?\b|riflescopes?\b|red\s?dot(?!\s+ready)|"
    r"reflex\s+sight|magnifier|range\s?finder|binoculars?|spotting|"
    r"\bmounts?\b|\brisers?\b|"
    # ammunition & reloading components
    r"\bpowder\b|primers?\b|\bprimed\b|\bbrass\b|\bbullets\b|projectiles?\b|"
    r"reloading|\bshells\b|snap\s?caps?|\b\d{2,3}\s?gr(?:ains?)?\b|"
    r"\bfmj\b|full\s+metal\s+jacket|hollow\s+point|\bjhp\b|soft\s+point|"
    # weapon lights & electronics
    r"weapon\s?lights?|\btac\s?lights?\b|\bflashlights?\b|light\s+body|\bbatteries\b|"
    # muzzle devices & NFA accessories ('Single Port Brake 1/2x28' has no
    # 'muzzle' — bare 'brake' is soft, rescued by a firearm noun)
    r"(?<!with\s)(?<!w/\s)muzzle\s?(brakes?|devices?)|\bbrakes?\b|flash\s?hiders?|"
    r"suppressors?|silencers?|choke\s?tubes?|"
    # parts
    r"\buppers?\b|bolt\s+carrier|\bbcg\b|charging\s+handle|"
    r"buffer\s+(tube|kit|spring)|gas\s+(block|tube)|\bhandguards?\b|"
    r"\bbuttstocks?\b|stock\s+(only|kit|set)\b|"
    # shooting targets & armor (observed leaks: 'AR500 Steel Target Gong',
    # 'Level IV Ceramic Plate ... SAPI') — but NOT bare 'Target', which is a
    # common firearm model word ('Ruger Mark IV Target')
    r"(?:steel|metal|ar500|paper|splatter|reactive)\s+(?:\w+\s+)?targets?\b|"
    r"targets?\s+(?:gongs?|stands?|hangers?)|\bgongs?\b|"
    r"body\s+armor|armou?r\s+plates?|ballistic\s+(?:plates?|helmets?|shields?)|"
    r"ceramic\s+plates?|\bsapi\b|ammo\s+cans?|"
    # soft gear & misc
    r"\bpouch(es)?\b|(?<!with\s)(?<!w/\s)\bslings?\b|"
    r"(?<!with\s)(?<!w/\s)\bbipods?\b|"
    r"gun\s?(sock|cover|rack|vault|belt)|\btripods?\b|"
    r"shooting\s+(rest|bag|stick)s?|ear\s?(muffs?|plugs?|pro)\b|"
    r"hearing\s+protection|shooting\s+glasses|"
    r"\bknife\b|\bknives\b|multi-?tool|\bdecals?\b|\bpatches\b|\bstickers?\b|"
    r"\bt-?shirts?\b|\bhoodies?\b|"
    # bare build components (soft: 'AR-10 Rifle, Aluminum Receiver' is a
    # rifle whose title mentions the receiver material — the noun rescues it;
    # the unambiguous forms live in _HARD_PARTS_RE)
    r"\breceivers?\b|\blowers?\b(?!\s*price)|\buppers?\b|\bchassis\b|"
    r"\bhandstops?\b|\bforegrips?\b|\bbrace\s+only\b|"
    # bulk-ammo count forms ('case of 500', '50 round box')
    r"\bcase of \d+\b|\bbox of \d+\b|\b\d+\s?(?:rounds?|rds?)\s+(?:box|case|bulk)\b",
    re.I)

# Negative lookahead shared by the noun rescue and gun_type(): a firearm noun
# that is the front half of an accessory compound ('Rifle Scope', 'Pistol
# Brace') names the accessory, not a gun.
_ACCESSORY_TAIL = (
    r"(?!\s*(?:scopes?|powder|brass|primers?|bullets?|ammo|ammunition|shells?|"
    r"cases?|bags?|slings?|bipods?|lights?|lasers?|braces?|grips?|mags?\b|"
    r"magazines?|holsters?|cleaning|rests?|safes?|vaults?|racks?|socks?|"
    r"covers?|stands?|displays?|parts?|kits?|dies?|tools?|stocks?|barrels?|"
    r"mounts?|rings?|targets?|suppressors?|silencers?|chokes?|springs?|bore\b|"
    r"pins?|triggers?|conversions?|uppers?|lowers?|frames?|slides?|"
    r"guides?|weapons?)\b)")

# A standalone firearm noun rescues a soft accessory match — a rifle sold
# with a scope is still a rifle — unless the noun is itself the front half of
# an accessory compound. NOTE: 'firearm(s)' is NOT a rescue noun — it shows up
# inside brand names ('Faxon Firearms Stripped Upper Receiver' is a part).
_FIREARM_RESCUE_RE = re.compile(
    r"\b(?:pistols?|handguns?|revolvers?|shotguns?|rifles?|carbines?|"
    r"derringers?|muzzleloaders?|sbr)\b" + _ACCESSORY_TAIL,
    re.I)


_BARREL_WORD_RE = re.compile(r"\bbarrels?\b|\bbbl\b", re.I)


def looks_like_accessory(title: str) -> bool:
    """True when a listing title reads like a part/accessory, not a firearm."""
    t = title or ""
    if _HARD_PARTS_RE.search(t) or _HARD_ACCESSORY_RE.search(t):
        return True
    if _ACCESSORY_RE.search(t):
        if _FIREARM_RESCUE_RE.search(t):
            return False
        # Spec rescue: 'Glenfield Model A, .30-06, 20" Barrel, 1-4 Rd Magazine'
        # names no firearm noun, but a ≥6" barrel spec (with the word barrel —
        # a target's 7"x12" dimensions don't count) only appears on gun titles.
        bl = barrel_length(t)
        return not (bl is not None and bl >= 6 and _BARREL_WORD_RE.search(t))
    return False


def classify(title: str, kind_hint: str = "") -> str:
    """'firearm' or 'accessory'. A hard part/accessory signal in the title
    overrides even the site's own category (kind_hint): stores file stripped
    lowers, complete uppers and serialized frames under their gun categories
    because legally they ARE firearms — for search and stats they're parts."""
    t = title or ""
    if _HARD_PARTS_RE.search(t) or _HARD_ACCESSORY_RE.search(t):
        return "accessory"
    if kind_hint in ("firearm", "accessory"):
        return kind_hint
    return "accessory" if looks_like_accessory(t) else "firearm"


_BUNDLE_RE = re.compile(
    # a gun sold WITH extras (optic/light/extra mags) — still a gun, but its
    # price is skewed, so analytics should be able to exclude it
    r"\bbundle\b|\bpackage\b|range\s?kit|"
    r"w/\s?(?:optic|red\s?dot|dot|scope|holosun|vortex|romeo|rmr|sro|acro|"
    r"reflex|sight|light|laser)|"
    r"\+\s?(?:holosun|vortex|romeo|swampfox|burris|crimson\s?trace|"
    r"streamlight|olight|trijicon)|"
    r"includes?\s+\d+\s+mag|with\s+\d+\s+mag|\d+\s+mags?\s+included",
    re.I)

_TRADE_IN_RE = re.compile(r"trade[\s-]?in", re.I)


def looks_like_bundle(title: str) -> bool:
    """True when a firearm listing includes extras that skew its price."""
    return bool(_BUNDLE_RE.search(title or ""))


def is_trade_in(title: str) -> bool:
    """LE/police/etc. trade-ins are their own price tier."""
    return bool(_TRADE_IN_RE.search(title or ""))


def capacity(title: str) -> str:
    for m in _CAP_RE.finditer(title or ""):
        n = next((g for g in m.groups() if g), None)
        if n and 1 <= int(n) <= 101:   # >101 = an ammo count, not a magazine
            return f"{n} rounds"
    return ""


def capacity_rounds(text: str):
    """First plausible round count in a capacity string ('15+1 rounds' -> 15,
    '' / '500 rounds of ammo' -> None). For the numeric stats column."""
    m = re.search(r"\d{1,3}", text or "")
    if not m:
        return None
    n = int(m.group())
    return n if 1 <= n <= 101 else None


# ---- field enrichment from the title ------------------------------------
# Sites that don't hand us structured manufacturer/model/action fields still
# put them in the listing title ('Browning BBR .30-06', 'Mossberg Patriot 30-06
# Springfield Bolt Action Rifle'). These fill the gaps; enrich() below applies
# them to any Listing whose fields came back empty.

# Brands we can spot at the front of a title. Longest first so 'Smith & Wesson'
# wins over a bare 'Smith'; canonical display name on the left, match aliases on
# the right. Broader than clients/brands.py (which is only the filter dropdown).
_BRANDS = [
    ("Smith & Wesson", ["smith & wesson", "smith and wesson", "s&w"]),
    ("Sig Sauer", ["sig sauer", "sig-sauer", "sig"]),
    ("Heckler & Koch", ["heckler & koch", "heckler and koch", "h&k", "hk"]),
    ("Springfield Armory", ["springfield armory", "springfield"]),
    ("Rock Island Armory", ["rock island armory", "rock island", "armscor"]),
    ("Sauer & Sohn", ["sauer & sohn", "j.p. sauer", "sauer und sohn", "sauer"]),
    ("FN", ["fn america", "fn herstal", "fnh", "fabrique nationale", "fn"]),
    ("Palmetto State Armory", ["palmetto state armory", "psa"]),
    ("Daniel Defense", ["daniel defense"]),
    ("Christensen Arms", ["christensen arms", "christensen"]),
    ("Anderson Manufacturing", ["anderson manufacturing", "anderson"]),
    ("Aero Precision", ["aero precision"]),
    ("Diamondback", ["diamondback"]),
    ("Radical Firearms", ["radical firearms"]),
    ("Century Arms", ["century arms", "century international"]),
    ("Maxim Defense", ["maxim defense"]),
    ("Wilson Combat", ["wilson combat"]),
    ("Dan Wesson", ["dan wesson"]),
    ("Shadow Systems", ["shadow systems"]),
    ("North American Arms", ["north american arms", "naa"]),
    ("Charter Arms", ["charter arms"]),
    ("Bond Arms", ["bond arms"]),
    ("Weatherby", ["weatherby"]),
    ("Winchester", ["winchester"]),
    ("Remington", ["remington", "remarms"]),
    ("Mossberg", ["mossberg", "o.f. mossberg"]),
    ("Browning", ["browning"]),
    ("Benelli", ["benelli"]),
    ("Stoeger", ["stoeger"]),
    ("Beretta", ["beretta"]),
    ("Savage", ["savage arms", "savage"]),
    ("Marlin", ["marlin"]),
    ("Henry", ["henry"]),
    ("Ruger", ["sturm ruger", "ruger"]),
    ("Tikka", ["tikka"]),
    ("Sako", ["sako"]),
    ("Bergara", ["bergara"]),
    ("Howa", ["howa"]),
    ("CZ", ["cz-usa", "cz usa", "cz"]),
    ("Zastava", ["zastava"]),
    ("IWI", ["iwi", "israel weapon"]),
    ("Kel-Tec", ["kel-tec", "keltec"]),
    ("Kimber", ["kimber"]),
    ("Walther", ["walther"]),
    ("Taurus", ["taurus"]),
    ("Canik", ["canik"]),
    ("GLOCK", ["glock"]),
    ("Colt", ["colt's", "colt"]),
    ("Thompson/Center", ["thompson/center", "thompson center", "t/c", "tc arms"]),
    ("Nosler", ["nosler"]),
    ("Franchi", ["franchi"]),
    ("Steyr", ["steyr"]),
    ("Merkel", ["merkel"]),
    ("Uberti", ["uberti"]),
    ("Cimarron", ["cimarron"]),
    ("Chiappa", ["chiappa"]),
    ("EAA", ["eaa", "european american"]),
    ("Girsan", ["girsan"]),
    ("Hi-Point", ["hi-point", "hi point"]),
    ("SCCY", ["sccy"]),
    ("Staccato", ["staccato"]),
    ("Bushmaster", ["bushmaster"]),
    ("DPMS", ["dpms"]),
    ("Del-Ton", ["del-ton", "delton"]),
    ("Barrett", ["barrett"]),
    ("Noreen", ["noreen"]),
    ("Nighthawk", ["nighthawk"]),
    ("Magnum Research", ["magnum research", "desert eagle"]),
    ("Inland", ["inland"]),
    ("Auto-Ordnance", ["auto-ordnance", "auto ordnance"]),
    ("Rossi", ["rossi"]),
    ("Heritage", ["heritage"]),
    ("Bear Creek", ["bear creek"]),
    ("PTR", ["ptr industries", "ptr"]),
    ("Smith-Corona", ["smith-corona", "smith corona"]),
    ("Tisas", ["tisas"]),
    ("CMMG", ["cmmg"]),
    ("B&T", ["b&t", "brugger & thomet"]),
    ("Grand Power", ["grand power"]),
    ("BCM", ["bravo company", "bcm"]),
    ("Charles Daly", ["charles daly"]),
    ("TriStar", ["tristar", "tri-star"]),
    ("Bersa", ["bersa"]),
    ("Mauser", ["mauser"]),
    ("American Tactical", ["american tactical", "ati"]),
    ("SDS Imports", ["sds imports"]),
    ("Citadel", ["citadel"]),
    ("Escort", ["escort"]),
    ("Hatsan", ["hatsan"]),
    ("Pointer", ["pointer"]),
    ("Dickinson", ["dickinson"]),
    ("Iver Johnson", ["iver johnson"]),
    ("Harrington & Richardson", ["harrington & richardson", "h&r 1871",
                                 "new england firearms", "nef"]),
    ("Traditions", ["traditions"]),
    ("CVA", ["cva", "connecticut valley"]),
    ("Pedersoli", ["pedersoli"]),
    ("Pietta", ["pietta"]),
    ("Taylor's & Co", ["taylor's & co", "taylors & co", "taylor's", "taylors"]),
    ("EMF", ["emf"]),
    ("LWRC", ["lwrc"]),
    ("POF", ["pof-usa", "patriot ordnance", "pof"]),
    ("Sons of Liberty", ["sons of liberty"]),
    ("Stag Arms", ["stag arms", "stag-15"]),
    ("Rock River Arms", ["rock river"]),
    ("Windham Weaponry", ["windham weaponry", "windham"]),
    ("Armalite", ["armalite"]),
    ("Franklin Armory", ["franklin armory"]),
    ("Noveske", ["noveske"]),
    ("Geissele", ["geissele"]),
    ("LMT", ["lmt", "lewis machine"]),
    ("ZEV", ["zev technologies", "zev"]),
    ("Faxon", ["faxon"]),
    ("FMK", ["fmk"]),
    ("Great Lakes Firearms", ["great lakes firearms", "great lakes", "glfa"]),
    ("GForce Arms", ["gforce", "g-force"]),
    ("Kalashnikov USA", ["kalashnikov usa", "kalashnikov"]),
    ("Arsenal", ["arsenal"]),
    ("Riley Defense", ["riley defense"]),
    ("Blaser", ["blaser"]),
    ("Krieghoff", ["krieghoff"]),
    ("Perazzi", ["perazzi"]),
    ("Caesar Guerini", ["caesar guerini"]),
    ("Rizzini", ["rizzini"]),
    ("Fabarm", ["fabarm"]),
    ("Yildiz", ["yildiz"]),
    ("ATA Arms", ["ata arms"]),
    ("Retay", ["retay"]),
    ("Akkar", ["akkar"]),
    ("Legacy Sports", ["legacy sports"]),
    ("Fierce", ["fierce firearms", "fierce"]),
    ("Seekins", ["seekins"]),
    ("Cooper", ["cooper firearms", "cooper arms"]),
    ("Polymer80", ["polymer80", "polymer 80"]),
    ("High Standard", ["high standard"]),
    ("Interarms", ["interarms"]),
    ("Norinco", ["norinco"]),
    ("Arex", ["arex"]),
    ("Sarsilmaz", ["sarsilmaz", "sar usa", "sar arms"]),
    ("Kriss", ["kriss"]),
    ("Angstadt", ["angstadt"]),
    ("Extar", ["extar"]),
    ("Phoenix Arms", ["phoenix arms"]),
    ("Black Rain Ordnance", ["black rain ordnance", "black rain"]),
    ("Ithaca", ["ithaca"]),
    ("Stevens", ["stevens"]),
    ("Maverick", ["maverick arms"]),
    ("Standard Manufacturing", ["standard manufacturing", "standard mfg"]),
    ("Ohio Ordnance", ["ohio ordnance"]),
    ("Sabatti", ["sabatti"]),
    ("Chapuis", ["chapuis"]),
    ("JTS", ["jts"]),
    ("Silver Eagle", ["silver eagle"]),
    ("Landor Arms", ["landor arms"]),
    ("Garaysar", ["garaysar"]),
    ("Tokarev Shotguns", ["tokarev usa", "tokarev shotgun"]),
    ("Big Horn Armory", ["big horn armory"]),
    ("Wildey", ["wildey"]),
    ("Coharie Arms", ["coharie"]),
    ("Vezir Arms", ["vezir"]),
]
# regex per brand, word-boundary anchored, tolerant of the '~' guns.com puts
# between title words; longest alias first inside each brand
_BRAND_RES = [
    (canon, re.compile(
        r"\b(?:" + "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
        + r")\b", re.I))
    for canon, aliases in _BRANDS
]

# Canonical action vocabulary — ONE label per mechanical class so stats can
# group on it. Striker-fired / DA-SA / blowback / gas-piston are trigger or
# operating details of semi-autos, not distinct actions.
ACTIONS = ["semi auto", "bolt", "lever", "pump", "revolver", "single shot",
           "over/under", "side by side", "muzzleloader"]

_ACTION_PATTERNS = [
    ("bolt", r"bolt[\s~-]?action|\bbolt\b|straight[\s~-]?pull"),
    ("semi auto", r"semi[\s~-]?auto\w*|auto[\s-]?loading|autoloader|"
                  r"self[\s-]?loading|gas[\s-]?operated|striker[\s-]?fired?"),
    ("lever", r"lever[\s~-]?action|\blever\b"),
    ("pump", r"pump[\s~-]?action|slide[\s~-]?action|\bpump\b"),
    ("over/under", r"over[\s/~-]?under|\bo/u\b|over\s?&\s?under"),
    ("side by side", r"side[\s~-]?by[\s~-]?side|\bsxs\b|\bs/s\b"),
    ("single shot", r"single[\s~-]?shot|break[\s~-]?(?:action|open)|"
                    r"hinge[\s-]?action|falling[\s-]?block|rolling[\s-]?block"),
    ("revolver", r"\brevolvers?\b"),
    ("muzzleloader", r"muzzle[\s-]?load(?:er|ing)|flint[\s-]?lock|percussion|"
                     r"\bcaplock\b|black[\s~-]?powder"),
]
_ACTION_RES = [(name, re.compile(p, re.I)) for name, p in _ACTION_PATTERNS]

# site-provided action strings -> canonical label ('SEMI AUTOMATIC',
# 'STRIKER FIRED', 'Over & Under', 'BLOW BACK', 'GAS PISTON'...)
_ACTION_NORM = [
    ("semi auto", r"semi|auto[\s-]?load|self[\s-]?load|blow[\s-]?back|"
                  r"gas[\s-]?(?:piston|operated|impingement)|striker|"
                  r"\bauto(?:matic)?\b"),
    ("bolt", r"\bbolt\b|straight[\s-]?pull"),
    ("lever", r"\blever\b"),
    ("pump", r"\bpump\b|slide[\s-]?action"),
    ("revolver", r"revolver"),
    ("over/under", r"over.{0,3}under|\bo/?u\b"),
    ("side by side", r"side.{0,3}side|\bsxs\b|\bs/s\b"),
    ("single shot", r"single[\s-]?shot|\bbreak\b|hinge|falling[\s-]?block|"
                    r"rolling[\s-]?block|trapdoor"),
    ("muzzleloader", r"muzzle|flint|percussion|caplock|black[\s-]?powder|inline"),
]
_ACTION_NORM_RES = [(name, re.compile(p, re.I)) for name, p in _ACTION_NORM]
_TRIGGER_ONLY_RE = re.compile(r"single|double|da/?sa|\bsao\b|\bdao\b", re.I)


def normalize_action(raw: str, gun_type: str = "") -> str:
    """Map any site's action spelling to the canonical vocabulary ('' if the
    value is unrecognized or names only a trigger type). 'SINGLE ACTION' /
    'DOUBLE ACTION' describe the trigger, not the action class — they resolve
    to 'revolver' only when the gun is known to be one."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    for name, rex in _ACTION_NORM_RES:
        if rex.search(s):
            return name
    if _TRIGGER_ONLY_RE.search(s):
        return "revolver" if gun_type == "revolver" else ""
    return ""


# gun_type from a standalone firearm noun in the title ('Rifle w/ Scope' is a
# rifle, 'Rifle Scope' is not — same compound guard as the accessory rescue).
_GUN_TYPE_PATTERNS = [
    ("revolver", r"\brevolvers?\b"),
    ("pistol", r"\bpistols?\b|\bhandguns?\b|\bderringers?\b"),
    ("shotgun", r"\bshotguns?\b"),
    ("muzzleloader", r"\bmuzzleloaders?\b|black[\s~-]?powder|flint[\s-]?lock|"
                     r"percussion"),
    ("rifle", r"\brifles?\b|\bcarbines?\b|\bsbr\b"),
]
_GUN_TYPE_RES = [(name, re.compile(f"(?:{p}){_ACCESSORY_TAIL}", re.I))
                 for name, p in _GUN_TYPE_PATTERNS]


def gun_type(title: str) -> str:
    """pistol / revolver / rifle / shotgun / muzzleloader from the title
    ('' when no standalone firearm noun appears)."""
    for name, rex in _GUN_TYPE_RES:
        if rex.search(title or ""):
            return name
    return ""

# descriptor words that are never part of a model name (used to trim the
# brand->caliber span down to just the model)
_MODEL_STOP = re.compile(
    r"\b(?:used|new|nib|preowned|pre-owned|excellent|very|good|fair|nice|rare|"
    r"rifle|pistol|revolver|shotgun|carbine|firearm|handgun|"
    r"bolt|lever|pump|semi|auto|action|single|shot|striker|fired|"
    r"blued?|stainless|steel|walnut|synthetic|camo|matte|od|green|grey|"
    r"gray|tan|fde|cerakote|hardwood|wood|left|hand|lh|rh|combo|package|w/|with|"
    r"scope|optic|threaded|barrel|bbl|inch|rounds?|rds?|round|magazine|mag)\b",
    re.I)


# corporate-name words that cling to the front of a model span once the brand
# is stripped ('Remington [Arms Firearms] 783') — drop them
_CORP_LEAD = re.compile(
    r"^(?:arms|firearms|inc|llc|co|company|corp|international|mfg|manufacturing|"
    r"usa|repeating|&|and|of|the)\b[\s.,]*", re.I)


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").replace("~", " ")).strip(" -.,|")
    prev = None
    while s and s != prev:   # peel repeated corporate lead-ins
        prev = s
        s = _CORP_LEAD.sub("", s).strip(" -.,|")
    return s


def manufacturer(title: str) -> str:
    """Best-guess brand from a title ('' if none of the known makers appear)."""
    t = title or ""
    best = None
    for canon, rex in _BRAND_RES:
        m = rex.search(t)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), canon)
    return best[1] if best else ""


def action(title: str) -> str:
    """Action type from a title ('' if not stated)."""
    for name, rex in _ACTION_RES:
        if rex.search(title or ""):
            return name
    return ""


def caliber_name(title: str) -> str:
    """Canonical caliber from a title via the shared alias table ('' if none)."""
    from . import calibers
    return calibers.canonical(title or "")


def _brand_span(title: str, prefer: str = ""):
    """Locate a brand in the title. Returns the EARLIEST-positioned match (so a
    brand at the front beats a maker-name that appears later inside a caliber,
    e.g. '.30-06 Springfield'). If `prefer` (an already-resolved brand) is given
    and present, its position wins."""
    t = title or ""
    if prefer:
        rex = next((r for c, r in _BRAND_RES if c == prefer), None)
        m = rex.search(t) if rex else None
        if m:
            return m.start(), m.end(), prefer
    best = None
    for canon, rex in _BRAND_RES:
        m = rex.search(t)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.end(), canon)
    return best


# spec tokens that end a model span: barrel lengths (22" / 4.5in), capacities
# (15+1 / 10rd / 6-shot), price chatter, and separator punctuation
_MODEL_SPEC_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:\"|”|in\b|inch)|\b\d{1,3}\s*\+\s*1\b|"
    r"\b\d{1,3}[ -]?(?:rounds?|rds?|shots?)\b|\$\d|[|,;]|\s-\s|\bsku\b",
    re.I)


def model(title: str, known_brand: str = "") -> str:
    """Model name, taken as the text between the brand and the caliber — the
    near-universal 'Brand Model Caliber ...' title shape ('Browning BBR .30-06'
    -> 'BBR', 'Mossberg Patriot 30-06 ...' -> 'Patriot'). The caliber is located
    via the full alias table (clients/calibers.py), so '432 J-Frame 32 H&R
    Magnum' correctly stops at '32 H&R'. Conservative: returns '' when no brand
    is found or the remaining span doesn't look like a model name."""
    t = re.sub(r"\([^)]*\)", " ", (title or "").replace("~", " "))
    span = _brand_span(t, prefer=known_brand)
    if not span:
        return ""
    after = t[span[1]:]
    from . import calibers
    cal = calibers.find_span(after)
    segment = after[:cal[0]] if cal else after
    # cut at the first spec token / descriptor word so 'Patriot 30-06 Bolt' ->
    # 'Patriot' and trailing junk after the model is dropped
    spec = _MODEL_SPEC_RE.search(segment)
    if spec:
        segment = segment[:spec.start()]
    stop = _MODEL_STOP.search(segment)
    if stop:
        segment = segment[:stop.start()]
    model_str = _clean(segment)
    # a model is a short token or two, not a sentence; reject the noise cases
    if not model_str or len(model_str) > 30 or len(model_str.split()) > 4:
        return ""
    return model_str


def enrich(listing) -> None:
    """Fill a Listing's empty descriptive fields from its title (and the
    model->action table). Only FILLS gaps — structured data from the client
    always wins — except `action`, which is re-spelled into the canonical
    vocabulary ('SEMI AUTOMATIC' -> 'semi auto') so stats can group on it."""
    title = listing.title or ""
    if not listing.manufacturer or listing.manufacturer.strip().lower() == "none":
        listing.manufacturer = manufacturer(title)
    if not listing.caliber:
        listing.caliber = caliber_name(title)
    if not listing.model:
        listing.model = model(title, listing.manufacturer)

    gt = (getattr(listing, "gun_type", "") or "").strip().lower()
    act = normalize_action(listing.action, gt) or action(title)
    if not act or not gt:
        from . import modelmap
        m_gt, m_act = modelmap.infer(listing.manufacturer, listing.model, title)
        gt = gt or m_gt
        act = act or m_act
    if not gt:
        gt = gun_type(title)
    if not gt:
        # a gauge chambering means shotgun even when the title never says so
        canon = caliber_name(listing.caliber or title)
        if canon.endswith("Gauge") or canon == ".410 Bore":
            gt = "shotgun"
    # site action strings like 'SINGLE ACTION' resolve only once the gun is
    # known to be a revolver — retry now that gun_type is settled
    if not act:
        act = normalize_action(listing.action, gt)
    if act == "revolver" and not gt:
        gt = "revolver"
    elif gt == "revolver" and not act:
        act = "revolver"
    if act and not gt:
        # actions that imply the platform with near-certainty (bolt/lever
        # shotguns and o-u rifles are rare exotics)
        gt = {"bolt": "rifle", "lever": "rifle", "over/under": "shotgun",
              "side by side": "shotgun", "muzzleloader": "muzzleloader"}.get(act, "")
    listing.action = act
    if hasattr(listing, "gun_type"):
        listing.gun_type = gt

    if listing.barrel_length is None:
        listing.barrel_length = barrel_length(title)
    if not listing.capacity:
        listing.capacity = capacity(title)
