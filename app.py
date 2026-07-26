"""Gun Scout — local multi-vertical listing search aggregator.

Three engines (guns / parts / ammo) share one backend; each is described by a
Vertical descriptor (see verticals/). Run:  python app.py  (http://127.0.0.1:8777)
"""
import logging

from flask import Flask, jsonify, request, send_from_directory

import close_poller
import db
import search_manager
import stats as stats_mod
import verticals
from models import SearchCriteria

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

app = Flask(__name__, static_folder="static", static_url_path="")
db.init_db()


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
    state = db.get_search_state(search_id, after_id=after)
    if state is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(state)


@app.get("/api/searches")
def searches():
    vertical = request.args.get("vertical")
    return jsonify(db.list_searches(vertical=vertical))


@app.delete("/api/search/<int:search_id>")
def delete_search(search_id):
    counts = db.delete_search(search_id)
    if counts is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"deleted": counts})


# ---- stats ---------------------------------------------------------------

@app.get("/api/<vertical>/stats")
def vertical_stats(vertical):
    if _vert_or_404(vertical) is None:
        return jsonify({"error": "unknown vertical"}), 404
    args = request.args.to_dict()
    args["vertical"] = vertical
    return jsonify(stats_mod.compute(args))


# ---- health & data mgmt (vertical-agnostic) ------------------------------

@app.post("/api/health")
def run_health():
    return jsonify(search_manager.run_health_checks())


@app.get("/api/health")
def last_health():
    return jsonify(db.latest_health())


@app.post("/api/clear")
def clear_data():
    return jsonify({"cleared": db.clear_all_data()})


if __name__ == "__main__":
    close_poller.start()  # records final hammer prices of ended auctions
    app.run(host="127.0.0.1", port=8777, debug=False, threaded=True)
