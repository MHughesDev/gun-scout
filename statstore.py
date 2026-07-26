"""Streaming market-stats engine — the ONLY thing Gun Scout persists.

Every listing observation is reduced to a compact anonymous "fact": just the
dimension values stats can group and filter by (brand, model, caliber, action,
grain, part category, …) plus one price basis per vertical (firm price for
guns/parts, cost-per-round for ammo). No URLs, titles, images, search links or
price history are ever written to disk — facts are keyed by a 64-bit hash of
the listing URL purely so re-observations update in place instead of double
counting.

How stats change AS SEARCHES HAPPEN:
- The full fact set lives in RAM (url-hash -> fact). `observe()` is called by
  every search thread for every listing it emits; it's an O(1) dict upsert
  under a lock, so the hot path never blocks on I/O.
- Every mutation bumps a version counter. The stats API (stats.py) computes
  from the in-memory facts and memoizes per (query, version), so a stats page
  polling during an active search sees numbers move with each batch of
  results, while idle polling costs nothing.
- Persistence is write-behind: a flusher daemon batches dirty facts into
  SQLite every couple of seconds (and once more at exit). A crash loses at
  most the last flush interval of fact updates — never search data, because
  search data intentionally isn't stored at all (see store.py).

First run against an old multi-table gun_scout.db migrates every stored
listing into a fact, then drops the legacy tables and VACUUMs the file.
"""
import atexit
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("gun_scout.stats")

# GUN_SCOUT_DB lets a cloud deploy point the fact store at a persistent
# volume (free-tier hosts wipe the app directory on every redeploy).
DB_PATH = Path(os.environ.get("GUN_SCOUT_DB")
               or Path(__file__).parent / "gun_scout.db")
FLUSH_INTERVAL = 2.0  # seconds between write-behind flushes

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stat_facts (
    url_hash   INTEGER PRIMARY KEY,   -- 64-bit hash of the listing URL (the URL itself is never stored)
    vertical   TEXT NOT NULL,
    fact       TEXT NOT NULL,         -- JSON dimension values + price basis
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_vertical ON stat_facts(vertical);
"""

_LEGACY_TABLES = ("price_observations", "search_results", "client_status",
                  "health_log", "searches", "listings")

_UPSERT = ("INSERT INTO stat_facts (url_hash, vertical, fact, first_seen, last_seen) "
           "VALUES (?,?,?,?,?) ON CONFLICT(url_hash) DO UPDATE SET "
           "fact=excluded.fact, last_seen=excluded.last_seen")


def hash_url(url: str) -> int:
    """Stable signed 64-bit identity for a listing URL."""
    d = hashlib.blake2s(url.encode("utf-8", "surrogatepass"), digest_size=8).digest()
    return int.from_bytes(d, "big", signed=True)


# ---- value normalizers (shared by live observations and legacy migration) --

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


def _pos(v):
    """A price only counts if it's a real positive number ('call for price' = $0)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


_MFR_CANON_CACHE: dict[str, str] = {}


def canon_mfr(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    hit = _MFR_CANON_CACHE.get(s)
    if hit is None:
        from clients import titleparse
        hit = titleparse.manufacturer(s) or s.title()
        _MFR_CANON_CACHE[s] = hit
    return hit


# ---- fact construction ----------------------------------------------------
# Fact keys (kept short — they're persisted as JSON):
#   v=vertical  rel=belongs to its vertical (kind == relevance_kind)
#   site  mfr  cal=canonical caliber  cond=condition
#   guns:  model act=action gt=gun_type grade=condition_grade ltype=listing_type
#          cap=capacity_rounds bbl=barrel_length ti=trade_in bun=is_bundle
#          hammer=price is a closed-auction hammer price
#   ammo:  grain bt=bullet_type case=case_material rc=round_count stock
#   parts: pcat=part_category fit=fitment stock
#   price: the vertical's price basis (guns/parts $, ammo $/round); never a bid

def fact_from_listing(listing) -> dict:
    from clients import calibers, titleparse
    from verticals import get as get_vertical
    vert = get_vertical(getattr(listing, "vertical", "guns") or "guns")
    attrs = dict(getattr(listing, "attributes", None) or {})
    kind = vert.classify_kind(listing)
    fact = {
        "v": vert.id,
        "rel": int(kind == vert.relevance_kind),
        "site": listing.site,
        "mfr": canon_mfr(listing.manufacturer),
        "cal": calibers.canonical(listing.caliber) or calibers.canonical(listing.title),
        "cond": listing.condition or "",
    }
    price = _pos(listing.price)  # firm / buy-now only; a live bid is not a price
    if vert.id == "ammo":
        fact.update({
            "grain": _int(attrs.get("grain")),
            "bt": attrs.get("bullet_type") or "",
            "case": attrs.get("case_material") or "",
            "rc": _int(attrs.get("round_count")),
            "stock": _stock(attrs.get("in_stock")),
            "price": _cpr(price, attrs),
        })
    elif vert.id == "parts":
        fact.update({
            "pcat": attrs.get("part_category") or "",
            "fit": attrs.get("fitment_canon") or "",
            "stock": _stock(attrs.get("in_stock")),
            "price": price,
        })
    else:  # guns
        fact.update({
            "model": (listing.model or "").strip(),
            "act": listing.action or "",
            "gt": getattr(listing, "gun_type", "") or "",
            "grade": listing.condition_grade or "",
            "ltype": listing.listing_type or "",
            "cap": titleparse.capacity_rounds(listing.capacity)
                   or _int(attrs.get("capacity_rounds")),
            "bbl": listing.barrel_length,
            "ti": int(bool(listing.trade_in) or titleparse.is_trade_in(listing.title)),
            "bun": int(titleparse.looks_like_bundle(listing.title)),
            "price": price,
        })
    return fact


def _fact_from_legacy_row(row: dict) -> dict | None:
    """One-time conversion of an old `listings` table row into a fact."""
    from verticals import get as get_vertical
    vert = get_vertical(row.get("vertical") or "guns")
    fact = {
        "v": vert.id,
        "rel": int((row.get("kind") or "") == vert.relevance_kind),
        "site": row.get("site") or "",
        "mfr": canon_mfr(row.get("manufacturer") or ""),
        "cal": row.get("caliber_canon") or "",
        "cond": row.get("condition") or "",
    }
    if vert.id == "ammo":
        fact.update({
            "grain": _int(row.get("grain")),
            "bt": row.get("bullet_type") or "",
            "case": row.get("case_material") or "",
            "rc": _int(row.get("round_count")),
            "stock": row.get("in_stock"),
            "price": _pos(row.get("price_per_round")),
        })
    elif vert.id == "parts":
        fact.update({
            "pcat": row.get("part_category") or "",
            "fit": row.get("fitment_canon") or "",
            "stock": row.get("in_stock"),
            "price": _pos(row.get("price")) or _pos(row.get("final_price")),
        })
    else:
        price = _pos(row.get("price"))
        hammer = price is None and _pos(row.get("final_price")) is not None
        fact.update({
            "model": (row.get("model") or "").strip(),
            "act": row.get("action") or "",
            "gt": row.get("gun_type") or "",
            "grade": row.get("condition_grade") or "",
            "ltype": row.get("listing_type") or "",
            "cap": _int(row.get("capacity_rounds")),
            "bbl": row.get("barrel_length"),
            "ti": int(row.get("trade_in") or 0),
            "bun": int(row.get("is_bundle") or 0),
            "price": price if price is not None else _pos(row.get("final_price")),
        })
        if hammer:
            fact["hammer"] = 1
    return fact


# ---- the engine -----------------------------------------------------------

class StatEngine:
    def __init__(self, db_path: Path = DB_PATH):
        self._db_path = db_path
        self._lock = threading.RLock()      # guards all in-memory state
        self._dblock = threading.Lock()     # guards the shared sqlite connection
        self._conn: sqlite3.Connection | None = None
        self._facts: dict[int, dict] = {}   # url_hash -> {"f": fact, "first": ts, "last": ts}
        self._dirty: set[int] = set()       # hashes awaiting flush
        self._version = 0                   # bumped on every stat-affecting change
        self._auctions: dict[int, dict] = {}  # RAM-only auction close tracking
        self._started = False

    # ---- lifecycle -------------------------------------------------------

    def start(self):
        """Open the DB, migrate any legacy schema, load facts, start flusher."""
        with self._lock:
            if self._started:
                return
            self._started = True
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, timeout=15,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate_legacy()
        n = self._load()
        log.info("stat engine up: %d facts loaded", n)
        threading.Thread(target=self._flush_loop, daemon=True,
                         name="stat-flusher").start()
        atexit.register(self.flush)

    def _migrate_legacy(self):
        conn = self._conn
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        legacy = [t for t in _LEGACY_TABLES if t in tables]
        if not legacy:
            return
        migrated = 0
        if "listings" in tables:
            for row in conn.execute("SELECT * FROM listings"):
                row = dict(row)
                try:
                    fact = _fact_from_legacy_row(row)
                except Exception:
                    continue
                conn.execute(_UPSERT, (
                    hash_url(row["url"]), fact["v"],
                    json.dumps(fact, separators=(",", ":")),
                    row.get("first_seen_at") or time.time(),
                    row.get("last_seen_at") or time.time()))
                migrated += 1
        for t in legacy:
            conn.execute(f"DROP TABLE {t}")
        if "sqlite_sequence" in tables:
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN (%s)"
                         % ",".join("?" * len(legacy)), legacy)
        conn.commit()
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        log.info("migrated legacy db: %d listings -> facts; dropped %s",
                 migrated, ", ".join(legacy))

    def _load(self) -> int:
        rows = self._conn.execute(
            "SELECT url_hash, fact, first_seen, last_seen FROM stat_facts").fetchall()
        with self._lock:
            for r in rows:
                try:
                    fact = json.loads(r["fact"])
                except ValueError:
                    continue
                self._facts[r["url_hash"]] = {
                    "f": fact, "first": r["first_seen"], "last": r["last_seen"]}
            self._version += 1
            return len(self._facts)

    # ---- the hot path: called by every search thread per listing ---------

    def observe(self, listing) -> bool:
        """Fold one listing observation into the live stats.
        Returns True if this URL had never been seen before (powers the NEW
        badge). O(1); never touches disk."""
        try:
            fact = fact_from_listing(listing)
        except Exception:
            log.exception("fact extraction failed for %s", listing.url)
            return False
        h = hash_url(listing.url)
        now = time.time()
        with self._lock:
            rec = self._facts.get(h)
            if rec is None:
                self._facts[h] = {"f": fact, "first": now, "last": now}
                self._dirty.add(h)
                self._version += 1
                is_new = True
            else:
                # keep a recorded hammer price: a stale live-listing re-scrape
                # must not resurrect the pre-close asking state
                if rec["f"].get("hammer") and fact.get("price") is None:
                    fact["price"] = rec["f"]["price"]
                    fact["hammer"] = 1
                if rec["f"] != fact:
                    rec["f"] = fact
                    self._version += 1
                rec["last"] = now
                self._dirty.add(h)
                is_new = False
        self._note_auction(h, listing)
        return is_new

    # ---- queries ---------------------------------------------------------

    @property
    def version(self) -> int:
        return self._version

    def facts(self, vertical: str) -> list[dict]:
        """Snapshot of every fact for a vertical (treat as read-only)."""
        with self._lock:
            return [rec["f"] for rec in self._facts.values()
                    if rec["f"].get("v") == vertical]

    def total_facts(self) -> int:
        with self._lock:
            return len(self._facts)

    # ---- persistence (write-behind) --------------------------------------

    def _flush_loop(self):
        while True:
            time.sleep(FLUSH_INTERVAL)
            try:
                self.flush()
            except Exception:
                log.exception("stat flush failed")

    def flush(self) -> int:
        """Write dirty facts to SQLite in one batch. Safe from any thread."""
        with self._lock:
            if not self._dirty:
                return 0
            batch = []
            for h in self._dirty:
                rec = self._facts.get(h)
                if rec is not None:
                    batch.append((h, rec["f"].get("v") or "guns",
                                  json.dumps(rec["f"], separators=(",", ":")),
                                  rec["first"], rec["last"]))
            self._dirty.clear()
        if not batch or self._conn is None:
            return 0
        with self._dblock:
            self._conn.executemany(_UPSERT, batch)
            self._conn.commit()
        return len(batch)

    def clear_all(self) -> int:
        """Wipe the fact store (RAM + disk). Returns facts deleted."""
        with self._lock:
            n = len(self._facts)
            self._facts.clear()
            self._dirty.clear()
            self._auctions.clear()
            self._version += 1
        if self._conn is not None:
            with self._dblock:
                self._conn.execute("DELETE FROM stat_facts")
                self._conn.commit()
                self._conn.execute("VACUUM")
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return n

    # ---- auction close tracking (RAM-only; feeds hammer prices to facts) --

    def _note_auction(self, h: int, listing):
        if listing.site != "gunbroker":
            return
        if (listing.listing_type or "") not in ("auction", "auction_buynow"):
            return
        item_id = (listing.extra or {}).get("itemID")
        if not item_id or not listing.ends_at:
            return
        with self._lock:
            a = self._auctions.get(h)
            if a and a["checked"] and a["ends_at"] == listing.ends_at:
                return  # already recorded this auction's close
            self._auctions[h] = {"item_id": item_id, "ends_at": listing.ends_at,
                                 "checked": False}

    def auctions_needing_close_check(self) -> list[dict]:
        now = time.time()
        with self._lock:
            return [{"url_hash": h, "item_id": a["item_id"], "ends_at": a["ends_at"]}
                    for h, a in self._auctions.items()
                    if not a["checked"] and a["ends_at"] < now]

    def reschedule_auction(self, url_hash: int, ends_at: float):
        with self._lock:
            a = self._auctions.get(url_hash)
            if a:
                a["ends_at"] = ends_at
                a["checked"] = False

    def record_auction_close(self, url_hash: int, final_price: float | None):
        """Fold an ended auction's hammer price (the one true market-clearing
        price) into its fact. final_price=None = ended without a sale."""
        with self._lock:
            a = self._auctions.get(url_hash)
            if a:
                a["checked"] = True
            rec = self._facts.get(url_hash)
            if rec is None or final_price is None:
                return
            fact = dict(rec["f"])
            fact["price"] = round(float(final_price), 2)
            fact["hammer"] = 1
            rec["f"] = fact
            rec["last"] = time.time()
            self._dirty.add(url_hash)
            self._version += 1


ENGINE = StatEngine()


def start():
    ENGINE.start()
