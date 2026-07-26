"""Runs one thread per site client and streams results into process memory.

Vertical-aware: clients live in REGISTRIES[vertical]; a search uses the registry
for criteria.vertical. Health checks sweep every vertical's clients.

Every emitted listing takes two hops: statstore.ENGINE.observe() folds it into
the live market stats (the only thing persisted), then store.record_listing()
appends it to the in-RAM search the UI is polling.
"""
import logging
import threading

import statstore
import store
from clients import REGISTRIES, ClientBlocked, StructureError
from models import SearchCriteria

log = logging.getLogger("gun_scout")


def _registry(vertical: str) -> dict:
    return REGISTRIES.get(vertical or "guns") or {}


def available_sites(vertical: str = "guns") -> list[dict]:
    return [
        {"name": cls.name, "label": cls.label, "homepage": cls.homepage}
        for cls in _registry(vertical).values()
    ]


def _all_clients() -> dict:
    """name -> (vertical, class) across every registry (for health checks)."""
    out = {}
    for vertical, reg in REGISTRIES.items():
        for name, cls in reg.items():
            out[name] = (vertical, cls)
    return out


def start_search(criteria: SearchCriteria) -> int:
    reg = _registry(criteria.vertical)
    sites = [s for s in (criteria.sites or reg.keys()) if s in reg]
    if not sites:
        sites = list(reg.keys())
    search_id = store.create_search(criteria.to_dict(), sites)
    for site in sites:
        t = threading.Thread(
            target=_run_client, args=(search_id, site, criteria), daemon=True,
            name=f"search-{search_id}-{site}",
        )
        t.start()
    return search_id


def _run_client(search_id: int, site: str, criteria: SearchCriteria):
    client = _registry(criteria.vertical)[site]()
    store.set_client_status(search_id, site, "running")

    def emit(listing):
        is_new = statstore.ENGINE.observe(listing)   # live stats move here
        store.record_listing(search_id, listing, is_new)

    try:
        client.search(criteria, emit)
        store.set_client_status(search_id, site, "done")
    except ClientBlocked as e:
        store.set_client_status(search_id, site, "blocked", str(e))
    except StructureError as e:
        log.warning("STRUCTURE CHANGE detected for %s: %s", site, e)
        store.set_client_status(search_id, site, "schema", str(e))
        store.log_health(site, "schema_changed", str(e), 0, 0, criteria.vertical)
    except Exception as e:
        log.exception("client %s failed", site)
        store.set_client_status(search_id, site, "error", f"{type(e).__name__}: {e}")
    finally:
        store.finish_search_if_done(search_id)


def run_health_checks(sites: list[str] | None = None) -> list[dict]:
    """Run every client's canary health check in parallel; log and return."""
    everything = _all_clients()
    targets = [s for s in (sites or everything.keys()) if s in everything]
    results: dict[str, dict] = {}

    def check(site):
        try:
            results[site] = everything[site][1]().health_check()
        except Exception as e:  # health_check itself should not raise, but be safe
            results[site] = {"site": site, "status": "error",
                             "message": f"{type(e).__name__}: {e}",
                             "found": 0, "elapsed_ms": 0}

    threads = [threading.Thread(target=check, args=(s,), daemon=True) for s in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    out = []
    for site in targets:
        r = results.get(site) or {"site": site, "status": "error",
                                  "message": "health check timed out",
                                  "found": 0, "elapsed_ms": 60000}
        if r["status"] != "ok":
            log.warning("health check %s: %s — %s", site, r["status"], r["message"])
        store.log_health(site, r["status"], r["message"], r["found"], r["elapsed_ms"],
                         everything[site][0])
        r["label"] = everything[site][1].label
        r["vertical"] = everything[site][0]
        out.append(r)
    return out
