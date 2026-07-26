"""Aggregate statistics over stored listings, per vertical.

Guns: median price by caliber/manufacturer/action/… over kind='firearm'.
Ammo: the price basis is COST PER ROUND, grouped by caliber/bullet/brand/grain.
Parts: price by category/fitment/brand.

The vertical descriptor supplies the relevance kind, the price column, the
default grouping, and the quality fields; the grouper functions live here.
Bundles (gun + optic packages) are excluded from PRICE stats by default. Open
auctions (only a current bid) count toward listing counts but never price stats.
"""
from collections import defaultdict

import db
from clients import calibers, titleparse

# ---- shared grouping helpers ---------------------------------------------
_CAP_BUCKETS = ((1, 5, "1–5"), (6, 10, "6–10"), (11, 15, "11–15"),
                (16, 20, "16–20"), (21, 30, "21–30"), (31, 999, "31+"))
_BBL_BUCKETS = ((0, 4, "under 4\""), (4, 6, "4–6\""), (6, 10, "6–10\""),
                (10, 16, "10–16\""), (16, 20, "16–20\""), (20, 24, "20–24\""),
                (24, 28, "24–28\""), (28, 99, "28\"+"))
_GRAIN_BUCKETS = ((1, 40, "≤40 gr"), (41, 60, "41–60 gr"), (61, 90, "61–90 gr"),
                  (91, 120, "91–120 gr"), (121, 150, "121–150 gr"),
                  (151, 180, "151–180 gr"), (181, 999, "180 gr+"))
_ROUND_BUCKETS = ((1, 20, "1–20"), (21, 50, "21–50"), (51, 200, "51–200"),
                  (201, 600, "201–600"), (601, 99999, "600+"))

_MFR_CANON_CACHE: dict[str, str] = {}


def _canon_mfr(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    hit = _MFR_CANON_CACHE.get(s)
    if hit is None:
        hit = titleparse.manufacturer(s) or s.title()
        _MFR_CANON_CACHE[s] = hit
    return hit


def _bucket(v, buckets):
    if v is None:
        return ""
    for lo, hi, label in buckets:
        if lo <= v <= hi:
            return label
    return ""


# group_by name -> function(sqlite row) -> group key ('' -> "(unknown)")
_GROUPERS_BY_VERTICAL = {
    "guns": {
        "caliber": lambda r: r["caliber_canon"],
        "manufacturer": lambda r: _canon_mfr(r["manufacturer"]),
        "model": lambda r: f"{_canon_mfr(r['manufacturer'])} {r['model']}".strip()
                           if r["model"] else "",
        "action": lambda r: r["action"],
        "gun_type": lambda r: r["gun_type"],
        "site": lambda r: r["site"],
        "condition": lambda r: r["condition"],
        "condition_grade": lambda r: r["condition_grade"],
        "listing_type": lambda r: r["listing_type"],
        "capacity": lambda r: _bucket(r["capacity_rounds"], _CAP_BUCKETS),
        "barrel": lambda r: _bucket(r["barrel_length"], _BBL_BUCKETS),
    },
    "ammo": {
        "caliber": lambda r: r["caliber_canon"],
        "manufacturer": lambda r: _canon_mfr(r["manufacturer"]),
        "bullet_type": lambda r: r["bullet_type"],
        "grain": lambda r: _bucket(r["grain"], _GRAIN_BUCKETS),
        "case_material": lambda r: r["case_material"],
        "round_count": lambda r: _bucket(r["round_count"], _ROUND_BUCKETS),
        "condition": lambda r: r["condition"],
        "site": lambda r: r["site"],
    },
    "parts": {
        "part_category": lambda r: r["part_category"],
        "fitment": lambda r: r["fitment_canon"],
        "manufacturer": lambda r: _canon_mfr(r["manufacturer"]),
        "caliber": lambda r: r["caliber_canon"],
        "condition": lambda r: r["condition"],
        "site": lambda r: r["site"],
    },
}


def _pct(sorted_vals: list, q: float):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 >= len(sorted_vals):
        return sorted_vals[-1]
    return sorted_vals[lo] * (1 - frac) + sorted_vals[lo + 1] * frac


def _r(v):
    return None if v is None else round(v, 2)


def _rp(v):
    """Round a price — 4 dp for sub-dollar cost-per-round, else 2 dp."""
    if v is None:
        return None
    return round(v, 4) if abs(v) < 1 else round(v, 2)


def _price_summary(prices: list) -> dict:
    prices = sorted(prices)
    n = len(prices)
    return {
        "priced": n,
        "min": _rp(prices[0]) if n else None,
        "p25": _rp(_pct(prices, .25)),
        "median": _rp(_pct(prices, .50)),
        "p75": _rp(_pct(prices, .75)),
        "p95": _rp(_pct(prices, .95)),
        "max": _rp(prices[-1]) if n else None,
        "mean": _rp(sum(prices) / n) if n else None,
    }


_SELECT = """SELECT site, manufacturer, model, caliber_canon, action,
                    gun_type, condition, condition_grade, trade_in,
                    is_bundle, listing_type, price, final_price,
                    capacity_rounds, barrel_length,
                    grain, bullet_type, round_count, price_per_round,
                    case_material, part_category, fitment_canon
             FROM listings WHERE vertical=? AND kind=?"""


def _price_of(r, price_col: str):
    if price_col == "price_per_round":
        v = r["price_per_round"]
        return v if (v is not None and v > 0) else None
    v = r["price"] if r["price"] is not None else r["final_price"]
    if v is not None and v <= 0:
        v = None    # 'call for price' listings come through as $0
    return v


def compute(args) -> dict:
    from verticals import get as get_vertical
    vertical = (args.get("vertical") or "guns").strip().lower()
    vert = get_vertical(vertical)
    groupers = _GROUPERS_BY_VERTICAL.get(vert.id, _GROUPERS_BY_VERTICAL["guns"])
    price_col = vert.stats_price_col

    group_by = (args.get("group_by") or vert.stats_default_group).strip()
    if group_by not in groupers:
        return {"error": f"unknown group_by '{group_by}' for {vert.id}",
                "choices": sorted(groupers)}
    grouper = groupers[group_by]

    want_caliber = calibers.canonical(args.get("caliber") or "")
    want_mfr = _canon_mfr(args.get("manufacturer") or "")
    want_action = titleparse.normalize_action(args.get("action") or "")
    want_type = (args.get("gun_type") or "").strip().lower()
    want_cond = (args.get("condition") or "").strip().lower()
    want_cat = (args.get("part_category") or "").strip()
    want_fit = (args.get("fitment") or "").strip()
    want_bullet = (args.get("bullet_type") or "").strip()
    want_sites = {s for s in (args.get("sites") or "").split(",") if s}
    price_min = _num(args.get("price_min"))
    price_max = _num(args.get("price_max"))
    include_bundles = args.get("include_bundles") in ("1", "true", "yes")
    exclude_tradeins = args.get("exclude_tradeins") in ("1", "true", "yes")
    limit = int(args.get("limit") or 40)

    with db.connect() as conn:
        rows = conn.execute(_SELECT, (vert.id, vert.relevance_kind)).fetchall()

    total = len(rows)
    kept = []
    n_bundles_dropped = 0
    for r in rows:
        if want_caliber and r["caliber_canon"] != want_caliber:
            continue
        if want_mfr and _canon_mfr(r["manufacturer"]) != want_mfr:
            continue
        if want_action and r["action"] != want_action:
            continue
        if want_type and r["gun_type"] != want_type:
            continue
        if want_cond in ("new", "used", "reman", "surplus") and r["condition"] != want_cond:
            continue
        if want_cat and r["part_category"] != want_cat:
            continue
        if want_fit and r["fitment_canon"] != want_fit:
            continue
        if want_bullet and (r["bullet_type"] or "").lower() != want_bullet.lower():
            continue
        if want_sites and r["site"] not in want_sites:
            continue
        if exclude_tradeins and r["trade_in"]:
            continue
        price = _price_of(r, price_col)
        if price_min is not None and (price is None or price < price_min):
            continue
        if price_max is not None and (price is None or price > price_max):
            continue
        kept.append((r, price))

    groups: dict[str, dict] = defaultdict(lambda: {"count": 0, "prices": []})
    for r, price in kept:
        key = grouper(r) or "(unknown)"
        g = groups[key]
        g["count"] += 1
        if price is not None and not (r["is_bundle"] and not include_bundles):
            g["prices"].append(price)
        if r["is_bundle"] and not include_bundles and price is not None:
            n_bundles_dropped += 1

    n = len(kept)
    out_groups = []
    for key, g in sorted(groups.items(), key=lambda kv: -kv[1]["count"]):
        out_groups.append({
            "key": key,
            "count": g["count"],
            "share": round(100.0 * g["count"] / n, 1) if n else 0.0,
            **_price_summary(g["prices"]),
        })
    truncated = max(0, len(out_groups) - limit)
    out_groups = out_groups[:limit]

    all_prices = [p for r, p in kept
                  if p is not None and not (r["is_bundle"] and not include_bundles)]
    quality = {}
    if n:
        for col, _label in vert.stats_quality_fields:
            quality[col + "_known_pct"] = round(
                100.0 * sum(1 for r, _ in kept if r[col] not in (None, "", 0)) / n, 1)

    return {
        "vertical": vert.id,
        "group_by": group_by,
        "group_choices": sorted(groupers),
        "price_label": vert.stats_price_label,
        "total_stored": total,
        "matched": n,
        "overall": _price_summary(all_prices),
        "bundles_excluded_from_prices": n_bundles_dropped,
        "groups": out_groups,
        "truncated_groups": truncated,
        "quality": quality,
        "quality_labels": {c + "_known_pct": l for c, l in vert.stats_quality_fields},
    }


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
