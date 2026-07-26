"""Re-enrich every stored listing with the current extraction stack.

Stored rows predate improvements to titleparse/modelmap/calibers and the
kind/gun_type/capacity_rounds columns, so their descriptive fields are frozen
at whatever the code could extract when they were first seen. This script
rebuilds each row's descriptive fields the same way a fresh scrape would:

- kind         via titleparse.classify (hard part signals override the site
               category, so stored stripped lowers/BCGs get reclassified)
- gun_type +   from the site's own stored category data (gunsamerica family
  action       id, cabelas class names, gunscom/sportsmans/gundeals category)
               -> then the model->action table -> then title words
- manufacturer/model/caliber/capacity/barrel: title parsing fills gaps only —
               structured values already stored are never overwritten
- caliber_canon / capacity_rounds / is_bundle / trade_in: recomputed

Usage:
  python backfill.py            # dry run: report what would change
  python backfill.py --apply    # write changes
  python backfill.py --apply --cabelas
        also pulls the full Coveo gun catalog (~45k docs, a few minutes) and
        grafts the structured action/magazine_capacity/upc/class fields onto
        stored cabelas rows by URL — those fields were never requested before
        2026-07-20, so stored rows can't get them from local data alone.
"""
import argparse
import json
import sys
import time

import db
from models import Listing, SearchCriteria

# fields backfill may rewrite; price/condition/lifecycle stay untouched
_DESC_FIELDS = ("title", "manufacturer", "model", "caliber", "caliber_canon",
                "action", "gun_type", "kind", "barrel_length", "capacity",
                "capacity_rounds", "trade_in", "is_bundle", "upc")


def _seed_from_extra(site: str, extra: dict, title: str):
    """(gun_type, action) the stored site-category data implies."""
    from clients import titleparse
    from clients.gunsamerica import _FAMILY_INFO
    from clients.cabelas import _gun_type_from_cat
    from clients.gunscom import _CATEGORY_GUN_TYPE

    cat = str(extra.get("category") or "")
    if site == "gunsamerica":
        return _FAMILY_INFO.get(str(extra.get("family") or ""), ("", ""))
    if site == "cabelas":
        cls = str(extra.get("class") or "")
        return (_gun_type_from_cat(cat.upper(), cls.upper()),
                titleparse.normalize_action(cat) or titleparse.normalize_action(cls))
    if site == "gunscom":
        return _CATEGORY_GUN_TYPE.get(cat.upper().strip(), ""), ""
    if site == "sportsmans":
        m = {"Rifles": "rifle", "Handguns": "pistol", "Shotguns": "shotgun",
             "Muzzleloaders": "muzzleloader",
             "Black Powder Handguns": "muzzleloader"}
        return m.get(cat, ""), ""
    if site == "gundeals":
        m = {"hand-guns": "pistol", "rifles": "rifle", "shotguns": "shotgun",
             "nfa": "other"}
        return m.get(cat, ""), ""
    return "", ""


def _pull_cabelas_map() -> dict:
    """url -> freshly-mapped Listing for the whole Coveo gun catalog."""
    from clients.cabelas import CabelasClient
    print("pulling Cabela's Coveo catalog (isgun=1, all conditions)...",
          flush=True)
    listings: dict[str, Listing] = {}
    crit = SearchCriteria(keyword="", condition="both", vertical="guns")
    t0 = time.time()
    CabelasClient().search(crit, lambda l: listings.setdefault(l.url, l))
    print(f"  {len(listings)} docs in {time.time()-t0:.0f}s")
    return listings


def _recompute(row: dict, cab_map: dict) -> dict:
    """Fresh _listing_values-style dict for a stored row."""
    from clients import titleparse
    extra = json.loads(row["extra"] or "{}")
    site = row["site"]

    seed_gt, seed_act = _seed_from_extra(site, extra, row["title"])

    fresh = cab_map.get(row["url"]) if site == "cabelas" else None
    if fresh is not None:
        # structured Coveo fields the stored row never had
        seed_gt = fresh.gun_type or seed_gt
        seed_act = fresh.action or seed_act
        capacity = fresh.capacity or row["capacity"]
        upc = fresh.upc or row["upc"]
        manufacturer = fresh.manufacturer or row["manufacturer"]
        caliber = fresh.caliber or row["caliber"]
        if fresh.extra.get("class"):
            extra["class"] = fresh.extra["class"]
            extra["category"] = fresh.extra.get("category") or extra.get("category")
        kind_hint = fresh.extra.get("kind") or extra.get("kind") or ""
    else:
        capacity, upc = row["capacity"], row["upc"]
        manufacturer, caliber = row["manufacturer"], row["caliber"]
        kind_hint = extra.get("kind") or ""

    # a stored action survives only if it maps into the canonical vocabulary;
    # else the category seed, then enrichment, takes over
    action = titleparse.normalize_action(row["action"], seed_gt) or seed_act

    listing = Listing(
        site=site, url=row["url"], title=row["title"],
        manufacturer=manufacturer, model=row["model"], caliber=caliber,
        action=action, gun_type=seed_gt,
        barrel_length=row["barrel_length"], capacity=capacity,
        condition=row["condition"], condition_grade=row["condition_grade"],
        trade_in=bool(row["trade_in"]), upc=upc,
        extra={**extra, "kind": kind_hint},
    )
    vals = db._listing_values(listing)
    vals["extra"] = json.dumps(extra)
    return {k: vals[k] for k in (*_DESC_FIELDS, "extra")}


def _fill_stats(conn):
    q = """
    select site, count(*) n,
      sum(kind='firearm') firearms, sum(kind='accessory') accessories,
      round(100.0*sum(manufacturer!='')/count(*),1) mfr,
      round(100.0*sum(model!='')/count(*),1) model,
      round(100.0*sum(caliber_canon!='')/count(*),1) cal,
      round(100.0*sum(action!='')/count(*),1) act,
      round(100.0*sum(gun_type!='')/count(*),1) gt,
      round(100.0*sum(capacity!='')/count(*),1) cap
    from listings group by site order by n desc"""
    print(f"{'site':<13}{'n':>7}{'guns':>7}{'parts':>7}{'mfr%':>7}"
          f"{'model%':>8}{'cal%':>7}{'act%':>7}{'type%':>7}{'cap%':>7}")
    for r in conn.execute(q):
        print(f"{r[0]:<13}{r[1]:>7}{r[2] or 0:>7}{r[3] or 0:>7}{r[4]:>7}"
              f"{r[5]:>8}{r[6]:>7}{r[7]:>7}{r[8]:>7}{r[9]:>7}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes")
    ap.add_argument("--cabelas", action="store_true",
                    help="also pull structured fields from Coveo by URL")
    args = ap.parse_args()

    db.init_db()
    cab_map = _pull_cabelas_map() if args.cabelas else {}

    conn = db.connect()
    print("--- before ---")
    _fill_stats(conn)

    rows = conn.execute("SELECT * FROM listings").fetchall()
    changed = 0
    reclassified = []
    t0 = time.time()
    updates = []
    for i, row in enumerate(rows):
        vals = _recompute(dict(row), cab_map)
        diff = {k: v for k, v in vals.items()
                if (row[k] if row[k] is not None else "") != (v if v is not None else "")}
        if not diff:
            continue
        changed += 1
        if diff.get("kind") == "accessory":
            reclassified.append((row["site"], row["title"]))
        updates.append((vals, row["id"]))
        if (i + 1) % 5000 == 0:
            print(f"  ...{i+1}/{len(rows)} scanned ({time.time()-t0:.0f}s)")

    print(f"\n{changed}/{len(rows)} rows change "
          f"({len(reclassified)} newly classified as parts/accessories)")
    for site, title in reclassified[:12]:
        print(f"   -> accessory: {site:<12} {title[:80]}")

    if args.apply and updates:
        cols = (*_DESC_FIELDS, "extra")
        sets = ", ".join(f"{c}=?" for c in cols)
        conn.executemany(
            f"UPDATE listings SET {sets} WHERE id=?",
            [(*[v[c] for c in cols], rid) for v, rid in updates])
        conn.commit()
        print("\n--- after ---")
        _fill_stats(conn)
    elif updates:
        print("\n(dry run — pass --apply to write)")
    conn.close()


if __name__ == "__main__":
    main()
