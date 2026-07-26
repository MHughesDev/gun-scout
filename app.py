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
import logging
import os

from flask import Flask, jsonify, request, send_from_directory

import cabelas_token
import close_poller
import search_manager
import statstore
import stats as stats_mod
import store
import verticals
from models import SearchCriteria

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

app = Flask(__name__, static_folder="static", static_url_path="")
statstore.start()  # load facts, migrate any legacy db, start write-behind flusher
close_poller.start()  # records final hammer prices of ended auctions (idles without an API key)

# Public/hosted mode (a host injected PORT, or GS_PUBLIC=1): ops UI — the site
# health checker and raw client error text — is for the operator running
# locally, not for visitors, so the API refuses it and the frontend hides it.
PUBLIC = bool(os.environ.get("GS_PUBLIC") or os.environ.get("PORT"))


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


# ---- shared Cabela's token (crowd-refreshed, stored in the app DB) -------

@app.get("/api/cabelas/token")
def cabelas_token_status():
    """Freshness of the shared Coveo token + where a browser can mint one.
    The token value itself never leaves the server this way."""
    return jsonify(cabelas_token.public_status())


@app.post("/api/cabelas/token")
def cabelas_token_push():
    """A visitor's browser minted a fresh token from cabelas.com — validate it
    hard (pinned org + live catalog probe) and make it the shared token."""
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
