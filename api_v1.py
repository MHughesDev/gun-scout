"""Versioned JSON API for non-browser clients (the iOS/Android app).

Why a second surface at all, when `/api/...` already returns JSON: the web UI
ships with the server, so those routes may change shape the same day the
frontend does. An installed mobile binary can't be redeployed — a two-year-old
build will still be calling this URL — so the mobile contract needs a stable
prefix, an explicit version, and a way for the server to tell old clients they
must upgrade. That is all this module is:

  * `/api/v1/...` — the same data the web UI reads, from the same functions
    (no logic is duplicated here; every handler delegates), pinned to a shape
    that only ever grows additively. Breaking changes get `/api/v2`.
  * `/api/v1/meta` — one boot call: verticals, feature flags, and the minimum
    client version the server still accepts. The kill switch for a bad build.
  * Server-owned feature flags. App review outcomes (see
    docs/mobile-platform-plan.md §2) can force a capability off across every
    installed copy in seconds; shipping that as a client constant would mean
    waiting days for a re-review instead.
  * `/api/v1/healthz` / `/readyz` — Cloud Run's startup and liveness probes
    want a cheap endpoint that doesn't touch a scraper.

The worker protocol (`/api/worker/...`) deliberately stays unversioned and
off this blueprint: it is an operator-to-operator link where both halves are
deployed together, not a public client contract.
"""
import os

from flask import Blueprint, jsonify, request

import remote
import search_manager
import statstore
import stats as stats_mod
import store
import verticals
from models import SearchCriteria

bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")

API_VERSION = 1

# Oldest app build the server will still serve. Bump it (env var, no deploy of
# the app needed) when a build is broken badly enough that it must stop
# talking to us; clients get 426 and show their upgrade screen.
MIN_CLIENT_VERSION = os.environ.get("GS_MIN_CLIENT_VERSION", "0.0.0")

# --- store-compliance flags, server-owned -------------------------------
# Both app stores restrict apps that facilitate firearm/ammunition purchase
# (docs/mobile-platform-plan.md §2). Whether the app may show a link out to a
# retailer's listing page is therefore a live policy decision, not a build-time
# one: default off, switched on per platform only once review outcomes allow.
# Enforced server-side — with it off the URLs never reach the device, so the
# rule holds even for a build that ignored the flag.
def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def listing_links_enabled() -> bool:
    return _flag("GS_MOBILE_LISTING_LINKS", False)


def _cors_origins() -> list[str]:
    """Allowed browser origins (Expo web builds, local dev). Native builds
    aren't subject to CORS at all, so this stays empty in production."""
    raw = os.environ.get("GS_CORS_ORIGINS", "").strip()
    return [o.strip() for o in raw.split(",") if o.strip()]


def _version_tuple(v: str) -> tuple:
    parts = []
    for chunk in str(v or "0").split(".")[:4]:
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _vert_or_404(vertical: str):
    return verticals.VERTICALS.get(vertical)


@bp.before_request
def _gate_client_version():
    """Refuse builds older than the floor. 426 is the one status the app is
    required to handle by showing 'update required' rather than an error."""
    if request.method == "OPTIONS" or request.path.endswith(("/healthz", "/readyz")):
        return None
    client = request.headers.get("X-Client-Version", "")
    if client and _version_tuple(client) < _version_tuple(MIN_CLIENT_VERSION):
        return jsonify({"error": "client too old",
                        "min_client_version": MIN_CLIENT_VERSION}), 426
    return None


@bp.after_request
def _cors(resp):
    origin = request.headers.get("Origin")
    allowed = _cors_origins()
    if origin and (origin in allowed or "*" in allowed):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Client-Version"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "600"
    return resp


# ---- boot / meta ---------------------------------------------------------

@bp.get("/meta")
def meta():
    """Everything the app needs on launch, in one round trip: what engines
    exist, what this server can currently do, and whether this build is still
    allowed to talk to it."""
    return jsonify({
        "api_version": API_VERSION,
        "min_client_version": MIN_CLIENT_VERSION,
        "verticals": [{"id": v.id, "label": v.label, "path": v.path}
                      for v in verticals.all_verticals()],
        "features": {
            # may the app show/open a link to the retailer's listing page?
            "listing_links": listing_links_enabled(),
            "stats": True,
            "ballistics": True,
            # sources behind Cloudflare only answer while the operator's
            # worker is up; the app greys them out rather than showing
            # "unavailable" after the fact.
            "worker_online": remote.worker_online(),
        },
    })


@bp.get("/verticals")
def list_verticals():
    return jsonify([{"id": v.id, "label": v.label, "path": v.path}
                    for v in verticals.all_verticals()])


@bp.get("/<vertical>/schema")
def vertical_schema(vertical):
    v = _vert_or_404(vertical)
    if v is None:
        return jsonify({"error": "unknown vertical"}), 404
    return jsonify(v.schema())


@bp.get("/<vertical>/clients")
def vertical_clients(vertical):
    if _vert_or_404(vertical) is None:
        return jsonify({"error": "unknown vertical"}), 404
    return jsonify(search_manager.available_sites(vertical))


# ---- search --------------------------------------------------------------

def _strip_links(state: dict) -> dict:
    """Remove listing URLs when the store-compliance flag is off. Done here,
    not on the device: the guarantee has to hold regardless of what the
    installed build does with the field.

    Copies each row rather than blanking it — `store.get_search_state` hands
    back the live row dicts, so editing them in place would erase the URL for
    the web UI polling the same search too.
    """
    state["listings"] = [{**row, "url": ""} for row in state.get("listings", ())]
    return state


@bp.post("/<vertical>/search")
def start_search(vertical):
    if _vert_or_404(vertical) is None:
        return jsonify({"error": "unknown vertical"}), 404
    payload = request.get_json(force=True, silent=True) or {}
    payload["vertical"] = vertical          # path is the source of truth
    criteria = SearchCriteria.from_dict(payload)
    # Mobile searches always run capped: a phone on cellular polling an
    # exhaustive walk of every site is the one shape of request this backend
    # should never accept, however the client asked.
    cap = int(os.environ.get("GS_MOBILE_MAX_PAGES", "5"))
    if criteria.max_pages is None or criteria.max_pages > cap:
        criteria.max_pages = cap
    return jsonify({"search_id": search_manager.start_search(criteria),
                    "poll_after_ms": 700})


@bp.get("/search/<int:search_id>")
def search_state(search_id):
    """Incremental poll. `after` is the last row id the client holds; the
    response carries only newer rows, so a long search stays cheap on
    cellular. A search that has aged out of RAM returns 404 — the app treats
    that as 'expired', not an error."""
    after = request.args.get("after", 0, type=int)
    state = store.get_search_state(search_id, after_id=after)
    if state is None:
        return jsonify({"error": "not found", "expired": True}), 404
    if not listing_links_enabled():
        state = _strip_links(state)
    state["done"] = state.get("status") == "done"
    return jsonify(state)


# ---- stats ---------------------------------------------------------------

@bp.get("/<vertical>/stats")
def vertical_stats(vertical):
    if _vert_or_404(vertical) is None:
        return jsonify({"error": "unknown vertical"}), 404
    args = request.args.to_dict()
    args["vertical"] = vertical
    return jsonify(stats_mod.compute(args))


# ---- probes --------------------------------------------------------------

@bp.get("/healthz")
def healthz():
    """Liveness: the process is up and serving. Deliberately touches nothing
    else — a scraper being blocked is not a reason to restart the container."""
    return jsonify({"ok": True})


@bp.get("/readyz")
def readyz():
    """Readiness: the fact store finished loading, so stats queries won't
    return a misleadingly empty market. Cloud Run holds traffic until this
    passes on a cold start."""
    ready = statstore.ENGINE.loaded
    body = {"ready": ready, "facts": statstore.ENGINE.total_facts()}
    return jsonify(body), (200 if ready else 503)
