"""SQLite storage. One connection per call; WAL mode so reader (UI polling)
and writers (client threads) don't block each other.

Data model (analytics-ready):
- listings        one row per unique listing URL ever seen. Lifecycle via
                  first_seen_at / last_seen_at; auctions carry current_bid /
                  ends_at and get final_price once the close poller confirms
                  the hammer price.
- price_observations  time series per listing. A row is only added when the
                  observed (price, current_bid, bid_count) differ from the
                  previous observation, so watch-mode re-runs every few
                  minutes don't bloat the table.
- search_results  which search surfaced which listing; its id doubles as the
                  incremental cursor the UI polls with.
"""
import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "gun_scout.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    vertical    TEXT NOT NULL DEFAULT 'guns',  -- guns | parts | ammo
    criteria    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS listings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    vertical         TEXT NOT NULL DEFAULT 'guns',  -- guns | parts | ammo
    site             TEXT NOT NULL,
    url              TEXT NOT NULL UNIQUE,
    title            TEXT NOT NULL,
    manufacturer     TEXT DEFAULT '',   -- brand / product line / part maker (all verticals)
    model            TEXT DEFAULT '',   -- model / line / part name (all verticals)
    caliber          TEXT DEFAULT '',   -- raw text as the site wrote it (guns, ammo, some parts)
    caliber_canon    TEXT DEFAULT '',   -- canonical name (clients/calibers.py)
    -- ---- guns-specific ----
    action           TEXT DEFAULT '',   -- canonical: semi auto|bolt|lever|pump|revolver|...
    gun_type         TEXT DEFAULT '',   -- pistol|revolver|rifle|shotgun|muzzleloader|other
    kind             TEXT DEFAULT '',   -- relevance class for the vertical: firearm|accessory|part|ammo
    barrel_length    REAL,
    capacity         TEXT DEFAULT '',
    capacity_rounds  INTEGER,           -- numeric magazine capacity (guns + parts magazines)
    condition_grade  TEXT DEFAULT '',   -- new|like-new|excellent|very-good|good|fair|''
    trade_in         INTEGER DEFAULT 0, -- LE/police trade-in: its own price tier
    is_bundle        INTEGER DEFAULT 0, -- gun + optic/mags package; skewed price
    -- ---- ammo-specific ----
    grain            INTEGER,           -- bullet weight
    bullet_type      TEXT DEFAULT '',   -- FMJ|JHP|SP|HP|match|frangible|lead|shot-##…
    round_count      INTEGER,           -- rounds in the listing (box or bulk case)
    price_per_round  REAL,              -- DERIVED: price / round_count (ammo headline metric)
    case_material    TEXT DEFAULT '',   -- brass|steel|aluminum|nickel
    -- ---- parts-specific ----
    part_category    TEXT DEFAULT '',   -- optic|magazine|trigger|barrel|bcg|handguard|... (primary axis)
    fitment_canon    TEXT DEFAULT '',   -- normalized platform: AR-15|Glock|1911|AK|Rem700|…
    mpn              TEXT DEFAULT '',   -- manufacturer part number
    -- ---- universal ----
    in_stock         INTEGER,           -- NULL=unknown; 0=out of stock; 1=in stock
    condition        TEXT DEFAULT '',   -- 'new' | 'used' | 'reman' | 'surplus' | ''
    upc              TEXT DEFAULT '',
    listing_type     TEXT DEFAULT '',   -- 'fixed' | 'auction' | 'auction_buynow' | ''
    price            REAL,              -- firm / buy-now price ONLY (never a bid)
    current_bid      REAL,              -- auctions only
    bid_count        INTEGER,
    ends_at          REAL,              -- auction close time (epoch)
    posted_at        REAL,              -- when the site says it was listed (epoch)
    final_price      REAL,              -- hammer price once the auction closed
    close_checked_at REAL,              -- close poller bookkeeping
    image            TEXT DEFAULT '',
    attributes       TEXT DEFAULT '{}', -- vertical-specific display-only long tail (JSON)
    extra            TEXT DEFAULT '{}',
    first_seen_at    REAL NOT NULL,
    last_seen_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS price_observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id  INTEGER NOT NULL REFERENCES listings(id),
    observed_at REAL NOT NULL,
    price       REAL,
    current_bid REAL,
    bid_count   INTEGER
);

CREATE TABLE IF NOT EXISTS search_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id   INTEGER NOT NULL REFERENCES searches(id),
    listing_id  INTEGER NOT NULL REFERENCES listings(id),
    is_new      INTEGER DEFAULT 0,     -- never seen in any earlier search
    UNIQUE(search_id, listing_id)
);

CREATE TABLE IF NOT EXISTS client_status (
    search_id  INTEGER NOT NULL REFERENCES searches(id),
    site       TEXT NOT NULL,
    status     TEXT NOT NULL,      -- queued | running | done | error | blocked
    message    TEXT DEFAULT '',
    found      INTEGER DEFAULT 0,
    UNIQUE(search_id, site)
);

CREATE TABLE IF NOT EXISTS health_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    site       TEXT NOT NULL,
    vertical   TEXT DEFAULT 'guns',
    checked_at REAL NOT NULL,
    status     TEXT NOT NULL,      -- ok | degraded | schema_changed | blocked | error
    message    TEXT DEFAULT '',
    found      INTEGER DEFAULT 0,
    elapsed_ms INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_obs_listing ON price_observations(listing_id, id);
CREATE INDEX IF NOT EXISTS idx_sr_search ON search_results(search_id, id);
CREATE INDEX IF NOT EXISTS idx_listings_ends ON listings(site, ends_at);
CREATE INDEX IF NOT EXISTS idx_health_site ON health_log(site, checked_at);
"""

# Listing fields refreshed on every re-observation of a known URL. `vertical`
# is deliberately absent — a URL never changes which engine it belongs to.
_MUTABLE = ("title", "manufacturer", "model", "caliber", "caliber_canon", "action",
            "gun_type", "kind", "barrel_length", "capacity", "capacity_rounds",
            "condition", "condition_grade", "trade_in", "is_bundle",
            "grain", "bullet_type", "round_count", "price_per_round", "case_material",
            "part_category", "fitment_canon", "mpn", "in_stock",
            "upc", "listing_type",
            "price", "current_bid", "bid_count", "ends_at", "posted_at",
            "image", "attributes", "extra")


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


def _stock(v):
    """Normalize a client's stock signal to 1 (in) / 0 (out) / None (unknown)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return 1 if v else 0
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "in stock", "instock", "available"):
        return 1
    if s in ("0", "false", "no", "out of stock", "oos", "backorder", "sold out"):
        return 0
    return None


def _cpr(price, attrs):
    """Ammo cost-per-round: an explicit parser value wins, else price/round_count."""
    v = attrs.get("price_per_round")
    if v not in (None, ""):
        try:
            return round(float(v), 4)
        except (TypeError, ValueError):
            pass
    rc = _int(attrs.get("round_count"))
    if price and rc:
        return round(price / rc, 4)
    return None


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        # migrate databases created before newer columns existed (additive only)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)")}
        for name, decl in (("posted_at", "REAL"),
                           ("gun_type", "TEXT DEFAULT ''"),
                           ("kind", "TEXT DEFAULT ''"),
                           ("capacity_rounds", "INTEGER"),
                           # multi-vertical additions
                           ("vertical", "TEXT NOT NULL DEFAULT 'guns'"),
                           ("grain", "INTEGER"),
                           ("bullet_type", "TEXT DEFAULT ''"),
                           ("round_count", "INTEGER"),
                           ("price_per_round", "REAL"),
                           ("case_material", "TEXT DEFAULT ''"),
                           ("part_category", "TEXT DEFAULT ''"),
                           ("fitment_canon", "TEXT DEFAULT ''"),
                           ("mpn", "TEXT DEFAULT ''"),
                           ("in_stock", "INTEGER"),
                           ("attributes", "TEXT DEFAULT '{}'")):
            if name not in cols:
                conn.execute(f"ALTER TABLE listings ADD COLUMN {name} {decl}")
        if "vertical" not in {r[1] for r in conn.execute("PRAGMA table_info(searches)")}:
            conn.execute("ALTER TABLE searches ADD COLUMN vertical "
                         "TEXT NOT NULL DEFAULT 'guns'")
        if "vertical" not in {r[1] for r in conn.execute("PRAGMA table_info(health_log)")}:
            conn.execute("ALTER TABLE health_log ADD COLUMN vertical TEXT DEFAULT 'guns'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_stats "
                     "ON listings(vertical, kind, caliber_canon, manufacturer)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_ammo "
                     "ON listings(vertical, caliber_canon, price_per_round)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_parts "
                     "ON listings(vertical, part_category, fitment_canon)")


def create_search(criteria: dict, sites: list[str]) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO searches (created_at, vertical, criteria) VALUES (?, ?, ?)",
            (time.time(), criteria.get("vertical") or "guns", json.dumps(criteria)),
        )
        search_id = cur.lastrowid
        for site in sites:
            conn.execute(
                "INSERT INTO client_status (search_id, site, status) VALUES (?, ?, 'queued')",
                (search_id, site),
            )
        return search_id


def set_client_status(search_id: int, site: str, status: str, message: str = ""):
    with connect() as conn:
        conn.execute(
            "UPDATE client_status SET status=?, message=? WHERE search_id=? AND site=?",
            (status, message, search_id, site),
        )


def finish_search_if_done(search_id: int):
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM client_status "
            "WHERE search_id=? AND status IN ('queued','running')",
            (search_id,),
        ).fetchone()
        if row["n"] == 0:
            conn.execute("UPDATE searches SET status='done' WHERE id=?", (search_id,))


def _listing_values(listing) -> dict:
    from clients import calibers, titleparse  # deferred: clients imports models, not db
    from verticals import get as get_vertical
    vert = get_vertical(getattr(listing, "vertical", "guns") or "guns")
    attrs = dict(getattr(listing, "attributes", None) or {})
    return {
        "title": listing.title,
        "manufacturer": listing.manufacturer,
        "model": listing.model,
        "caliber": listing.caliber,
        "caliber_canon": calibers.canonical(listing.caliber)
                         or calibers.canonical(listing.title),
        "action": listing.action,
        "gun_type": getattr(listing, "gun_type", "") or "",
        # relevance class for this listing's vertical: firearm|accessory|part|ammo
        "kind": vert.classify_kind(listing),
        "barrel_length": listing.barrel_length,
        "capacity": listing.capacity,
        "capacity_rounds": titleparse.capacity_rounds(listing.capacity)
                           or _int(attrs.get("capacity_rounds")),
        "condition": listing.condition,
        "condition_grade": listing.condition_grade,
        "trade_in": int(listing.trade_in or titleparse.is_trade_in(listing.title)),
        "is_bundle": int(titleparse.looks_like_bundle(listing.title)),
        # ammo columns — parsers write these into listing.attributes
        "grain": _int(attrs.get("grain")),
        "bullet_type": attrs.get("bullet_type") or "",
        "round_count": _int(attrs.get("round_count")),
        "price_per_round": _cpr(listing.price, attrs),
        "case_material": attrs.get("case_material") or "",
        # parts columns
        "part_category": attrs.get("part_category") or "",
        "fitment_canon": attrs.get("fitment_canon") or "",
        "mpn": attrs.get("mpn") or "",
        # universal
        "in_stock": _stock(attrs.get("in_stock")),
        "upc": listing.upc,
        "listing_type": listing.listing_type,
        "price": listing.price,
        "current_bid": listing.current_bid,
        "bid_count": listing.bid_count,
        "ends_at": listing.ends_at,
        "posted_at": listing.posted_at,
        "image": listing.image,
        "attributes": json.dumps(attrs),
        "extra": json.dumps(listing.extra),
    }


def record_listing(search_id: int, listing) -> bool:
    """Upsert a listing observation; returns True if it was new for this search."""
    now = time.time()
    vals = _listing_values(listing)
    with connect() as conn:
        row = conn.execute(
            "SELECT id, price, current_bid, bid_count FROM listings WHERE url=?",
            (listing.url,),
        ).fetchone()
        if row is None:
            # "NEW" badge = never seen in any earlier search; suppressed on the
            # very first search since everything would trivially be new.
            has_history = conn.execute(
                "SELECT 1 FROM searches WHERE id != ? LIMIT 1", (search_id,)
            ).fetchone()
            is_new = has_history is not None
            cols = ["vertical", "site", "url", *vals.keys(),
                    "first_seen_at", "last_seen_at"]
            cur = conn.execute(
                f"INSERT INTO listings ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                (getattr(listing, "vertical", "guns") or "guns",
                 listing.site, listing.url, *vals.values(), now, now),
            )
            listing_id = cur.lastrowid
            values_changed = True  # first observation always recorded
        else:
            listing_id = row["id"]
            is_new = False
            values_changed = (
                (row["price"], row["current_bid"], row["bid_count"])
                != (vals["price"], vals["current_bid"], vals["bid_count"])
            )
            sets = ", ".join(f"{c}=?" for c in _MUTABLE)
            conn.execute(
                f"UPDATE listings SET {sets}, last_seen_at=? WHERE id=?",
                (*[vals[c] for c in _MUTABLE], now, listing_id),
            )

        cur = conn.execute(
            "INSERT OR IGNORE INTO search_results (search_id, listing_id, is_new) "
            "VALUES (?,?,?)",
            (search_id, listing_id, int(is_new)),
        )
        if cur.rowcount == 0:
            return False  # duplicate within this search

        if values_changed:
            conn.execute(
                "INSERT INTO price_observations (listing_id, observed_at, price, "
                "current_bid, bid_count) VALUES (?,?,?,?,?)",
                (listing_id, now, vals["price"], vals["current_bid"], vals["bid_count"]),
            )
        conn.execute(
            "UPDATE client_status SET found = found + 1 WHERE search_id=? AND site=?",
            (search_id, listing.site),
        )
        return True


def get_search_state(search_id: int, after_id: int = 0) -> dict | None:
    with connect() as conn:
        search = conn.execute("SELECT * FROM searches WHERE id=?", (search_id,)).fetchone()
        if not search:
            return None
        statuses = conn.execute(
            "SELECT site, status, message, found FROM client_status WHERE search_id=?",
            (search_id,),
        ).fetchall()
        rows = conn.execute(
            """SELECT sr.id AS cursor_id, sr.is_new, l.*
               FROM search_results sr JOIN listings l ON l.id = sr.listing_id
               WHERE sr.search_id=? AND sr.id>? ORDER BY sr.id""",
            (search_id, after_id),
        ).fetchall()
        listings = []
        for r in rows:
            d = dict(r)
            d["listing_id"] = d.pop("id")
            d["id"] = d.pop("cursor_id")   # the UI's poll cursor
            d["is_new"] = bool(d["is_new"])
            d["extra"] = json.loads(d["extra"] or "{}")
            d["attributes"] = json.loads(d["attributes"] or "{}")
            listings.append(d)
        return {
            "id": search["id"],
            "status": search["status"],
            "criteria": json.loads(search["criteria"]),
            "clients": [dict(s) for s in statuses],
            "listings": listings,
        }


def list_searches(limit: int = 25, vertical: str | None = None) -> list[dict]:
    where = "WHERE s.vertical = ?" if vertical else ""
    params = ([vertical] if vertical else []) + [limit]
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT s.id, s.created_at, s.vertical, s.criteria, s.status,
                      (SELECT COUNT(*) FROM search_results sr
                       WHERE sr.search_id = s.id) AS found
               FROM searches s {where} ORDER BY s.id DESC LIMIT ?""",
            params,
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["criteria"] = json.loads(d["criteria"])
            out.append(d)
        return out


def delete_search(search_id: int) -> dict | None:
    """Delete one search: its status rows, its result joins, and any listing
    (plus price history) that no remaining search references. Listings other
    searches also surfaced survive untouched. Returns rows-deleted per table,
    or None if the search doesn't exist."""
    with connect() as conn:
        if conn.execute("SELECT 1 FROM searches WHERE id=?",
                        (search_id,)).fetchone() is None:
            return None
        counts = {"searches": conn.execute(
            "DELETE FROM searches WHERE id=?", (search_id,)).rowcount}
        # sweep by "no longer has a search" rather than by id: also collects
        # strays a mid-run delete left behind (threads of a deleted search
        # keep inserting until they finish), which would otherwise pin their
        # listings as referenced forever
        counts["client_status"] = conn.execute(
            "DELETE FROM client_status WHERE search_id NOT IN "
            "(SELECT id FROM searches)").rowcount
        counts["search_results"] = conn.execute(
            "DELETE FROM search_results WHERE search_id NOT IN "
            "(SELECT id FROM searches)").rowcount
        orphan = ("NOT EXISTS (SELECT 1 FROM search_results sr "
                  "WHERE sr.listing_id = listings.id)")
        counts["price_observations"] = conn.execute(
            "DELETE FROM price_observations WHERE listing_id IN "
            f"(SELECT id FROM listings WHERE {orphan})").rowcount
        counts["listings"] = conn.execute(
            f"DELETE FROM listings WHERE {orphan}").rowcount
        return counts


# ---- danger zone ---------------------------------------------------------

def clear_all_data() -> dict:
    """Wipe every table — searches, listings, price history, health log —
    and reclaim the file space. Returns rows-deleted per table."""
    tables = ("price_observations", "search_results", "client_status",
              "health_log", "listings", "searches")  # children before parents
    conn = connect()
    try:
        counts = {}
        with conn:  # one transaction for the whole wipe
            for t in tables:
                counts[t] = conn.execute(f"DELETE FROM {t}").rowcount
            conn.execute("DELETE FROM sqlite_sequence")  # ids restart at 1
        conn.execute("VACUUM")  # shrink the file; must run outside the txn
        # in WAL mode the vacuumed image sits in the -wal file until a
        # checkpoint pushes it back and truncates the main db
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return counts
    finally:
        conn.close()


# ---- auction close tracking ---------------------------------------------

def auctions_needing_close_check(site: str = "gunbroker") -> list[dict]:
    """Ended auctions whose final price we haven't captured yet."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM listings
               WHERE site=? AND listing_type IN ('auction','auction_buynow')
                 AND ends_at IS NOT NULL AND ends_at < ?
                 AND close_checked_at IS NULL""",
            (site, time.time()),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["extra"] = json.loads(d["extra"] or "{}")
            out.append(d)
        return out


def reschedule_auction(listing_id: int, ends_at: float):
    """Auction got extended/relisted — push the close check to the new end."""
    with connect() as conn:
        conn.execute("UPDATE listings SET ends_at=? WHERE id=?", (ends_at, listing_id))


def record_auction_close(listing_id: int, final_price: float | None,
                         bid_count: int | None = None):
    """Record the outcome of an ended auction. final_price=None means it ended
    without a sale (no bids / not sold) — still marked checked so the poller
    doesn't retry forever."""
    now = time.time()
    with connect() as conn:
        conn.execute(
            "UPDATE listings SET final_price=?, close_checked_at=? WHERE id=?",
            (final_price, now, listing_id),
        )
        if final_price is not None:
            conn.execute(
                "INSERT INTO price_observations (listing_id, observed_at, price, "
                "current_bid, bid_count) VALUES (?,?,?,?,?)",
                (listing_id, now, None, final_price, bid_count),
            )


# ---- health --------------------------------------------------------------

def log_health(site: str, status: str, message: str, found: int, elapsed_ms: int,
               vertical: str = "guns"):
    with connect() as conn:
        conn.execute(
            "INSERT INTO health_log (site, vertical, checked_at, status, message, "
            "found, elapsed_ms) VALUES (?,?,?,?,?,?,?)",
            (site, vertical, time.time(), status, message, found, elapsed_ms),
        )


def latest_health() -> list[dict]:
    """Most recent health record per site."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT h.* FROM health_log h
               JOIN (SELECT site, MAX(id) AS mid FROM health_log GROUP BY site) m
                 ON h.id = m.mid
               ORDER BY h.site""").fetchall()
        return [dict(r) for r in rows]
