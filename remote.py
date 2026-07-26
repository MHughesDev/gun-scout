"""Remote search jobs — the hosted app's half of the local-worker split.

Cloudflare judges datacenter IPs far more harshly than home connections, so
several retailers 403 the hosted app while serving the operator's own machine
normally. Rather than disguising where requests come from, the scrapers simply
run where they already work: this module queues a job per site, a worker on
the operator's machine claims it, runs the real client, and posts listings
back. Results land in exactly the same store/statstore path as locally-run
clients, so the UI can't tell the difference.

The queue deliberately hands the worker SEARCH CRITERIA, never URLs — the
worker builds its own requests from its own hardcoded site constants. That is
what keeps a compromised server from turning the worker into an open proxy
(see guard.py for the enforcement side).

Everything here is process memory, like store.py: jobs are ephemeral and
nothing about a search is ever written to disk.
"""
import logging
import threading
import time

log = logging.getLogger("gun_scout.remote")

# A worker is considered online if it has polled within this many seconds.
# Longer than the long-poll window so a poll in flight never looks like a drop.
WORKER_TIMEOUT_S = 90
# A claimed job that goes this long without completing is presumed dead (worker
# killed mid-search) and released so the search can finish rather than hang.
JOB_TIMEOUT_S = 600
MAX_PENDING = 200          # backstop against a queue flood

_lock = threading.RLock()
_jobs: dict[int, dict] = {}
_next_id = 0
_workers: dict[str, float] = {}     # worker_id -> last seen (epoch)
_started = False


def start():
    """Start the sweeper that fails jobs when the worker goes away."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_sweep_loop, daemon=True, name="remote-sweeper").start()


def worker_online() -> bool:
    now = time.time()
    with _lock:
        return any(now - seen < WORKER_TIMEOUT_S for seen in _workers.values())


def note_worker(worker_id: str):
    with _lock:
        _workers[worker_id] = time.time()


def status() -> dict:
    now = time.time()
    with _lock:
        return {
            "online": any(now - s < WORKER_TIMEOUT_S for s in _workers.values()),
            "workers": [{"id": w, "last_seen_s": round(now - s, 1)}
                        for w, s in sorted(_workers.items())],
            "pending": sum(1 for j in _jobs.values() if j["status"] == "pending"),
            "running": sum(1 for j in _jobs.values() if j["status"] == "claimed"),
        }


def enqueue(search_id: int, site: str, criteria: dict) -> int | None:
    """Queue one site of one search for the worker. Returns the job id, or
    None if the queue is saturated (caller should mark the site errored)."""
    global _next_id
    with _lock:
        if sum(1 for j in _jobs.values() if j["status"] == "pending") >= MAX_PENDING:
            return None
        _next_id += 1
        jid = _next_id
        _jobs[jid] = {
            "id": jid, "search_id": search_id, "site": site,
            "criteria": criteria, "status": "pending",
            "created_at": time.time(), "touched_at": time.time(),
        }
        return jid


def claim(worker_id: str, sites: list[str], limit: int = 4) -> list[dict]:
    """Hand pending jobs to a worker. Only criteria travel — never URLs."""
    note_worker(worker_id)
    out = []
    with _lock:
        for job in sorted(_jobs.values(), key=lambda j: j["id"]):
            if len(out) >= limit:
                break
            if job["status"] != "pending" or job["site"] not in sites:
                continue
            job["status"] = "claimed"
            job["worker_id"] = worker_id
            job["touched_at"] = time.time()
            out.append({"job_id": job["id"], "site": job["site"],
                        "criteria": job["criteria"]})
    if out:
        import store
        for j in out:
            job = _jobs.get(j["job_id"])
            if job:
                store.set_client_status(job["search_id"], job["site"], "running")
    return out


def touch(job_id: int):
    with _lock:
        job = _jobs.get(job_id)
        if job:
            job["touched_at"] = time.time()


def get(job_id: int) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def complete(job_id: int, status: str, message: str = "") -> bool:
    """Mark a job finished and settle the search's per-site status."""
    import store
    with _lock:
        job = _jobs.pop(job_id, None)
    if job is None:
        return False
    store.set_client_status(job["search_id"], job["site"], status, message)
    store.finish_search_if_done(job["search_id"])
    return True


def _sweep_loop():
    while True:
        time.sleep(10)
        try:
            _sweep()
        except Exception:
            log.exception("remote job sweep failed")


def _sweep():
    """Fail jobs that can't proceed: no worker online to take them, or a
    claimed job whose worker vanished. Without this a search would sit at
    'queued' forever whenever the operator's machine is off."""
    import store
    now = time.time()
    online = worker_online()
    dead: list[tuple[dict, str]] = []
    with _lock:
        for jid, job in list(_jobs.items()):
            age = now - job["touched_at"]
            if job["status"] == "pending" and not online and age > WORKER_TIMEOUT_S:
                dead.append((_jobs.pop(jid), "search worker offline — this "
                                             "source is unavailable right now"))
            elif job["status"] == "claimed" and age > JOB_TIMEOUT_S:
                dead.append((_jobs.pop(jid), "search worker stopped responding"))
    for job, msg in dead:
        store.set_client_status(job["search_id"], job["site"], "unavailable", msg)
        store.finish_search_if_done(job["search_id"])
    if dead:
        log.info("released %d remote job(s): worker unavailable", len(dead))
