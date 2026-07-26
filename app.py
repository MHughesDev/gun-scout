"""Gun Scout — multi-vertical listing search aggregator.

Three engines (guns / parts / ammo) share one backend; each is described by a
Vertical descriptor (see verticals/).

Run locally:   python app.py                    (http://127.0.0.1:8777)
Cloud deploy:  set PORT (free hosts inject it) — the app binds 0.0.0.0 and
               serves via waitress. State is multi-user by design: the stats
               fact store and site tokens/keys (CABELAS_COVEO_TOKEN /
               cabelas_token.json, GUNBROKER_DEV_KEY) are shared server-side
               across all visitors; per-user search state is RAM-only and
               short-lived; search history never leaves the visitor's browser.
               Point GUN_SCOUT_DB at a persistent volume so stats survive
               redeploys. Run a single process (1 worker) — the fact store
               lives in process memory.
"""
import hmac
import logging
import os

from flask import Flask, jsonify, request, send_from_directory

import cabelas_token
import close_poller
import remote
import search_manager
import statstore
import stats as stats_mod
import store
import verticals
from models import Listing, SearchCriteria

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

app = Flask(__name__, static_folder="static", static_url_path="")
statstore.start()  # load facts, migrate any legacy db, start write-behind flusher
close_poller.start()  # records final hammer prices of ended auctions (idles without an API key)
remote.start()  # releases queued jobs when the operator's worker is offline

# Cap request bodies: the worker posts result batches, and nothing else this
# app accepts is large. Keeps a hostile POST from ballooning memory.
app.config["MAX_CONTENT_LENGTH"] = 24 * 1024 * 1024

# Public/hosted mode (a host injected PORT, or GS_PUBLIC=1): ops UI — the site
# health checker and raw client error text — is for the operator running
# locally, not for visitors, so the API refuses it and the frontend hides it.
PUBLIC = bool(os.environ.get("GS_PUBLIC") or os.environ.get("PORT"))

# Page ceiling for visitor searches. Locally a search walks every site to
# exhaustion, which is fine on your own machine — but hosted, each page is
# pulled through the operator's home connection, and one uncapped search
# measured 192 MB. Capping depth keeps a public search to a sane slice of
# that (and stops the worker's own rate limiter from truncating results
# mid-run). Raise GS_PUBLIC_MAX_PAGES for deeper coverage at more data.
PUBLIC_MAX_PAGES = int(os.environ.get("GS_PUBLIC_MAX_PAGES", "5"))


def _vert_or_404(vertical: str):
    v = verticals.VERTICALS.get(vertical)
    return v


# ---- pages ---------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory("static", "search.html")


@app.get("/parts")
def parts_page():
    return send_from_directory("static", "search.html")


@app.get("/ammo")
def ammo_page():
    return send_from_directory("static", "search.html")


@app.get("/stats")
def stats_page():
    return send_from_directory("static", "stats.html")


@app.get("/ballistics")
def ballistics():
    return send_from_directory("static", "ballistics.html")


# ---- shared metadata -----------------------------------------------------

@app.get("/api/config")
def app_config():
    """Frontend boot flags. `public` hides operator-only UI (health checks,
    raw scraper error text) on the hosted deployment."""
    return jsonify({"public": PUBLIC})


@app.get("/api/verticals")
def list_verticals():
    """Nav data: every engine's id / label / path, in display order."""
    return jsonify([{"id": v.id, "label": v.label, "path": v.path}
                    for v in verticals.all_verticals()])


@app.get("/api/<vertical>/schema")
def vertical_schema(vertical):
    v = _vert_or_404(vertical)
    if v is None:
        return jsonify({"error": "unknown vertical"}), 404
    return jsonify(v.schema())


@app.get("/api/<vertical>/clients")
def vertical_clients(vertical):
    if _vert_or_404(vertical) is None:
        return jsonify({"error": "unknown vertical"}), 404
    return jsonify(search_manager.available_sites(vertical))


# ---- search --------------------------------------------------------------

@app.post("/api/<vertical>/search")
def start_search(vertical):
    if _vert_or_404(vertical) is None:
        return jsonify({"error": "unknown vertical"}), 404
    payload = request.get_json(force=True) or {}
    payload["vertical"] = vertical          # path is the source of truth
    criteria = SearchCriteria.from_dict(payload)
    if PUBLIC and criteria.max_pages is None:
        criteria.max_pages = PUBLIC_MAX_PAGES
    return jsonify({"search_id": search_manager.start_search(criteria)})


@app.get("/api/search/<int:search_id>")
def search_state(search_id):
    after = request.args.get("after", 0, type=int)
    state = store.get_search_state(search_id, after_id=after)
    if state is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(state)


# ---- stats ---------------------------------------------------------------

@app.get("/api/<vertical>/stats")
def vertical_stats(vertical):
    if _vert_or_404(vertical) is None:
        return jsonify({"error": "unknown vertical"}), 404
    args = request.args.to_dict()
    args["vertical"] = vertical
    return jsonify(stats_mod.compute(args))


# ---- local worker protocol ------------------------------------------------
# The operator's machine claims search jobs, runs the real clients on its own
# connection, and posts listings back. It is handed CRITERIA, never URLs —
# see remote.py and guard.py for why that boundary matters.

_LISTING_FIELDS = set(Listing.__dataclass_fields__)
MAX_LISTINGS_PER_POST = 500


def _worker_auth():
    """(ok, error_response). Fails closed: a public deployment with no key
    configured refuses the worker protocol outright rather than letting
    anyone claim jobs or inject listings into the shared stats."""
    key = os.environ.get("GS_WORKER_KEY", "")
    if not key:
        if PUBLIC:
            return False, (jsonify({"error": "worker protocol disabled: "
                                             "GS_WORKER_KEY is not set"}), 503)
        return True, None       # local dev: no key needed
    if not hmac.compare_digest(request.headers.get("X-Worker-Key", ""), key):
        return False, (jsonify({"error": "bad or missing worker key"}), 403)
    return True, None


@app.post("/api/worker/claim")
def worker_claim():
    ok, err = _worker_auth()
    if not ok:
        return err
    payload = request.get_json(force=True, silent=True) or {}
    worker_id = str(payload.get("worker_id") or "worker")[:64]
    sites = [str(s)[:40] for s in (payload.get("sites") or [])][:40]
    jobs = remote.claim(worker_id, sites)
    return jsonify({"jobs": jobs, "poll_after_s": 0 if jobs else 3})


@app.post("/api/worker/results")
def worker_results():
    ok, err = _worker_auth()
    if not ok:
        return err
    payload = request.get_json(force=True, silent=True) or {}
    job = remote.get(int(payload.get("job_id") or 0))
    if job is None or job["status"] != "claimed":
        return jsonify({"error": "unknown or already-finished job"}), 404
    raw = payload.get("listings") or []
    if not isinstance(raw, list):
        return jsonify({"error": "listings must be a list"}), 400
    accepted = 0
    for d in raw[:MAX_LISTINGS_PER_POST]:
        if not isinstance(d, dict):
            continue
        try:
            # only known dataclass fields — never **d blindly
            listing = Listing(**{k: v for k, v in d.items() if k in _LISTING_FIELDS})
        except (TypeError, ValueError):
            continue
        if not listing.url or not listing.site:
            continue
        listing.site = job["site"]      # a job may only report its own site
        if search_manager.ingest(job["search_id"], listing):
            accepted += 1
    remote.touch(job["id"])
    return jsonify({"accepted": accepted})


@app.post("/api/worker/complete")
def worker_complete():
    ok, err = _worker_auth()
    if not ok:
        return err
    payload = request.get_json(force=True, silent=True) or {}
    status = str(payload.get("status") or "done")[:20]
    if status not in ("done", "error", "blocked", "schema", "unavailable"):
        status = "done"
    found = remote.complete(int(payload.get("job_id") or 0), status,
                            str(payload.get("message") or "")[:400])
    return jsonify({"ok": found})


@app.get("/api/worker/status")
def worker_status():
    ok, err = _worker_auth()
    if not ok:
        return err
    return jsonify(remote.status())


# ---- shared Cabela's token (crowd-refreshed, stored in the app DB) -------

@app.get("/api/cabelas/token")
def cabelas_token_status():
    """Freshness of the shared Coveo token + where a browser can mint one.
    The token value itself never leaves the server this way."""
    return jsonify(cabelas_token.public_status())


@app.post("/api/cabelas/token")
def cabelas_token_push():
    """Accept a freshly-minted token from the operator's relay (push_token.py)
    and make it the shared token. Validated hard (pinned org + live catalog
    probe) regardless, so the worst a leaked key allows is donating a
    perfectly good token."""
    key = os.environ.get("GS_TOKEN_PUSH_KEY", "")
    if key and not hmac.compare_digest(request.headers.get("X-Push-Key", ""), key):
        return jsonify({"error": "bad or missing push key"}), 403
    payload = request.get_json(force=True, silent=True) or {}
    accepted, message = cabelas_token.accept_token(payload.get("token") or "")
    body = {"accepted": accepted, "message": message,
            **cabelas_token.public_status()}
    return jsonify(body), (200 if accepted else 400)


# ---- health & data mgmt (vertical-agnostic) ------------------------------

@app.post("/api/health")
def run_health():
    if PUBLIC:  # canary scrapes are an operator tool, not a visitor feature
        return jsonify({"error": "not available"}), 404
    return jsonify(search_manager.run_health_checks())


@app.get("/api/health")
def last_health():
    if PUBLIC:
        return jsonify({"error": "not available"}), 404
    return jsonify(store.latest_health())


if __name__ == "__main__":
    # Free cloud hosts inject PORT; its presence is the "public deploy" signal.
    port = int(os.environ.get("PORT", "8777"))
    host = os.environ.get("HOST") or ("0.0.0.0" if "PORT" in os.environ
                                      else "127.0.0.1")
    try:
        from waitress import serve  # production WSGI server, pure Python
        logging.getLogger("gun_scout").info("serving via waitress on %s:%d",
                                            host, port)
        serve(app, host=host, port=port, threads=16)
    except ImportError:
        app.run(host=host, port=port, debug=False, threaded=True)
