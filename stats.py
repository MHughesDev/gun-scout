"""Live aggregate statistics over the in-memory fact set, per vertical.

Guns: price stats by caliber/manufacturer/action/… over facts with rel=1.
Ammo: the price basis is COST PER ROUND, grouped by caliber/bullet/brand/grain.
Parts: price by category/fitment/brand.

Queries run against statstore.ENGINE's RAM fact set — never the disk — and are
memoized per (query, engine version), so a stats page polling while a search
is running sees the numbers move with every batch of results, and idle polling
is effectively free. Bundles (gun + optic packages) are excluded from PRICE
stats by default; open auctions (only a current bid) count toward listing
counts but never price stats (their fact price arrives when the close poller
records the hammer price).
"""
import time
from collections import defaultdict

import statstore
from clients import calibers, titleparse

# ---- bucketed groupers (buckets applied at query time, so they can evolve
# without invalidating stored facts) ----------------------------------------
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


def _bucket(v, buckets):
    if v is None:
        return ""
    for lo, hi, label in buckets:
        if lo <= v <= hi:
            return label
    return ""


# group_by name -> function(fact dict) -> group key ('' -> "(unknown)")
_GROUPERS_BY_VERTICAL = {
    "guns": {
        "caliber": lambda f: f.get("cal", ""),
        "manufacturer": lambda f: f.get("mfr", ""),
        "model": lambda f: f"{f.get('mfr', '')} {f['model']}".strip()
                           if f.get("model") else "",
        "action": lambda f: f.get("act", ""),
        "gun_type": lambda f: f.get("gt", ""),
        "site": lambda f: f.get("site", ""),
        "condition": lambda f: f.get("cond", ""),
        "condition_grade": lambda f: f.get("grade", ""),
        "listing_type": lambda f: f.get("ltype", ""),
        "capacity": lambda f: _bucket(f.get("cap"), _CAP_BUCKETS),
        "barrel": lambda f: _bucket(f.get("bbl"), _BBL_BUCKETS),
    },
    "ammo": {
        "caliber": lambda f: f.get("cal", ""),
        "manufacturer": lambda f: f.get("mfr", ""),
        "bullet_type": lambda f: f.get("bt", ""),
        "grain": lambda f: _bucket(f.get("grain"), _GRAIN_BUCKETS),
        "case_material": lambda f: f.get("case", ""),
        "round_count": lambda f: _bucket(f.get("rc"), _ROUND_BUCKETS),
        "condition": lambda f: f.get("cond", ""),
        "site": lambda f: f.get("site", ""),
    },
    "parts": {
        "part_category": lambda f: f.get("pcat", ""),
        "fitment": lambda f: f.get("fit", ""),
        "manufacturer": lambda f: f.get("mfr", ""),
        "caliber": lambda f: f.get("cal", ""),
        "condition": lambda f: f.get("cond", ""),
        "site": lambda f: f.get("site", ""),
    },
}

# vertical quality-field column name (verticals/*.py) -> fact key
_QUALITY_KEY = {"caliber_canon": "cal", "manufacturer": "mfr", "action": "act",
                "gun_type": "gt", "model": "model", "capacity_rounds": "cap",
                "grain": "grain", "bullet_type": "bt", "round_count": "rc",
                "part_category": "pcat", "fitment_canon": "fit"}


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


# (query args, engine version) -> result. Cleared wholesale when it grows —
# entries die naturally anyway the moment the version bumps.
_cache: dict[tuple, dict] = {}
_CACHE_MAX = 64


def compute(args) -> dict:
    version = statstore.ENGINE.version
    key = (version, tuple(sorted((k, str(v)) for k, v in args.items())))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    result = _compute(args, version)
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = result
    return result


def _compute(args, version: int) -> dict:
    from verticals import get as get_vertical
    vertical = (args.get("vertical") or "guns").strip().lower()
    vert = get_vertical(vertical)
    groupers = _GROUPERS_BY_VERTICAL.get(vert.id, _GROUPERS_BY_VERTICAL["guns"])

    group_by = (args.get("group_by") or vert.stats_default_group).strip()
    if group_by not in groupers:
        return {"error": f"unknown group_by '{group_by}' for {vert.id}",
                "choices": sorted(groupers)}
    grouper = groupers[group_by]

    want_caliber = calibers.canonical(args.get("caliber") or "")
    want_mfr = statstore.canon_mfr(args.get("manufacturer") or "")
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

    facts = [f for f in statstore.ENGINE.facts(vert.id) if f.get("rel")]
    total = len(facts)

    kept = []
    for f in facts:
        if want_caliber and f.get("cal") != want_caliber:
            continue
        if want_mfr and f.get("mfr") != want_mfr:
            continue
        if want_action and f.get("act") != want_action:
            continue
        if want_type and f.get("gt") != want_type:
            continue
        if want_cond in ("new", "used", "reman", "surplus") and f.get("cond") != want_cond:
            continue
        if want_cat and f.get("pcat") != want_cat:
            continue
        if want_fit and f.get("fit") != want_fit:
            continue
        if want_bullet and (f.get("bt") or "").lower() != want_bullet.lower():
            continue
        if want_sites and f.get("site") not in want_sites:
            continue
        if exclude_tradeins and f.get("ti"):
            continue
        price = f.get("price")
        if price_min is not None and (price is None or price < price_min):
            continue
        if price_max is not None and (price is None or price > price_max):
            continue
        kept.append((f, price))

    groups: dict[str, dict] = defaultdict(lambda: {"count": 0, "prices": []})
    n_bundles_dropped = 0
    for f, price in kept:
        key = grouper(f) or "(unknown)"
        g = groups[key]
        g["count"] += 1
        if price is not None:
            if f.get("bun") and not include_bundles:
                n_bundles_dropped += 1
            else:
                g["prices"].append(price)

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

    all_prices = [p for f, p in kept
                  if p is not None and not (f.get("bun") and not include_bundles)]
    quality = {}
    if n:
        for col, _label in vert.stats_quality_fields:
            fk = _QUALITY_KEY.get(col, col)
            quality[col + "_known_pct"] = round(
                100.0 * sum(1 for f, _ in kept
                            if f.get(fk) not in (None, "", 0)) / n, 1)

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
        "version": version,
        "as_of": time.time(),
    }


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
