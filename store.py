"""Process-memory storage for in-flight searches, client status and health.

Nothing in this module ever touches disk — the only durable artifact Gun
Scout keeps is the market-stats fact store (statstore.py). Search state
exists purely so the UI can poll results in while its search runs; results
are transient by design (search history lives in each visitor's browser, and
only as input values). Finished searches are dropped wholesale after a short
TTL, and the total is capped, so a public multi-user deployment can't grow
RAM without bound.
"""
import threading
import time

import statstore

MAX_SEARCHES = 60        # simultaneous searches held in RAM across all users
RESULT_TTL_S = 15 * 60   # a finished search (and its results) evaporates after this

_lock = threading.RLock()
_searches: dict[int, dict] = {}   # insertion-ordered: oldest first
_next_id = 0
_health: dict[str, dict] = {}     # site -> latest health record


def _int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def _row_from_listing(listing) -> dict:
    """The display row the UI renders — same shape the old DB rows had."""
    from clients import calibers, titleparse
    from verticals import get as get_vertical
    vert = get_vertical(getattr(listing, "vertical", "guns") or "guns")
    attrs = dict(getattr(listing, "attributes", None) or {})
    price = listing.price
    return {
        "vertical": vert.id,
        "site": listing.site,
        "url": listing.url,
        "title": listing.title,
        "manufacturer": listing.manufacturer,
        "model": listing.model,
        "caliber": listing.caliber,
        "caliber_canon": calibers.canonical(listing.caliber)
                         or calibers.canonical(listing.title),
        "action": listing.action,
        "gun_type": getattr(listing, "gun_type", "") or "",
        "kind": vert.classify_kind(listing),
        "barrel_length": listing.barrel_length,
        "capacity": listing.capacity,
        "capacity_rounds": titleparse.capacity_rounds(listing.capacity)
                           or _int(attrs.get("capacity_rounds")),
        "condition": listing.condition,
        "condition_grade": listing.condition_grade,
        "trade_in": int(listing.trade_in or titleparse.is_trade_in(listing.title)),
        "is_bundle": int(titleparse.looks_like_bundle(listing.title)),
        "grain": _int(attrs.get("grain")),
        "bullet_type": attrs.get("bullet_type") or "",
        "round_count": _int(attrs.get("round_count")),
        "price_per_round": statstore._cpr(price, attrs),
        "case_material": attrs.get("case_material") or "",
        "part_category": attrs.get("part_category") or "",
        "fitment_canon": attrs.get("fitment_canon") or "",
        "mpn": attrs.get("mpn") or "",
        "in_stock": statstore._stock(attrs.get("in_stock")),
        "upc": listing.upc,
        "listing_type": listing.listing_type,
        "price": price,
        "current_bid": listing.current_bid,
        "bid_count": listing.bid_count,
        "ends_at": listing.ends_at,
        "posted_at": listing.posted_at,
        "image": listing.image,
        "attributes": attrs,
        "extra": dict(listing.extra or {}),
    }


# ---- searches -------------------------------------------------------------

def create_search(criteria: dict, sites: list[str]) -> int:
    global _next_id
    with _lock:
        _next_id += 1
        sid = _next_id
        _searches[sid] = {
            "id": sid,
            "created_at": time.time(),
            "vertical": criteria.get("vertical") or "guns",
            "criteria": criteria,
            "status": "running",
            "clients": {s: {"site": s, "status": "queued", "message": "", "found": 0}
                        for s in sites},
            "rows": [],
            "seen_urls": set(),
            "done_at": None,
            # NEW badges are meaningless when the stats engine has never seen
            # anything (a fresh install's first search would be 100% "new")
            "suppress_new": statstore.ENGINE.total_facts() == 0,
        }
        _evict()
        return sid


def _evict():
    """Expire finished searches past their TTL, then enforce the hard cap
    (dropping oldest finished first, oldest of all if every slot is running)."""
    now = time.time()
    for sid in [sid for sid, s in _searches.items()
                if s["status"] == "done" and now - s["done_at"] > RESULT_TTL_S]:
        _searches.pop(sid, None)
    while len(_searches) > MAX_SEARCHES:
        done = next((sid for sid, s in _searches.items() if s["status"] == "done"),
                    None)
        victim = done if done is not None else next(iter(_searches))
        _searches.pop(victim, None)


def record_listing(search_id: int, listing, fact_is_new: bool) -> bool:
    """Append one result row to a search. Returns False on duplicate URL
    within the search or if the search was deleted mid-run."""
    row = _row_from_listing(listing)
    with _lock:
        s = _searches.get(search_id)
        if s is None or listing.url in s["seen_urls"]:
            return False
        s["seen_urls"].add(listing.url)
        row["id"] = len(s["rows"]) + 1   # the UI's incremental poll cursor
        row["is_new"] = bool(fact_is_new and not s["suppress_new"])
        s["rows"].append(row)
        c = s["clients"].get(listing.site)
        if c:
            c["found"] += 1
        return True


def set_client_status(search_id: int, site: str, status: str, message: str = ""):
    with _lock:
        s = _searches.get(search_id)
        if s and site in s["clients"]:
            s["clients"][site]["status"] = status
            s["clients"][site]["message"] = message


def finish_search_if_done(search_id: int):
    with _lock:
        s = _searches.get(search_id)
        if s and not any(c["status"] in ("queued", "running")
                         for c in s["clients"].values()):
            s["status"] = "done"
            s["done_at"] = time.time()
            s["seen_urls"] = set()   # dedupe set only matters while running


def get_search_state(search_id: int, after_id: int = 0) -> dict | None:
    with _lock:
        _evict()
        s = _searches.get(search_id)
        if s is None:
            return None
        # row ids are 1..n in list order, so the incremental slice is direct
        return {
            "id": s["id"],
            "status": s["status"],
            "criteria": s["criteria"],
            "clients": [dict(c) for c in s["clients"].values()],
            "listings": list(s["rows"][after_id:]),
        }


# ---- health ---------------------------------------------------------------

def log_health(site: str, status: str, message: str, found: int, elapsed_ms: int,
               vertical: str = "guns"):
    with _lock:
        _health[site] = {"site": site, "vertical": vertical,
                         "checked_at": time.time(), "status": status,
                         "message": message, "found": found,
                         "elapsed_ms": elapsed_ms}


def latest_health() -> list[dict]:
    with _lock:
        return [dict(_health[site]) for site in sorted(_health)]
