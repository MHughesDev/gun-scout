"""Gun Scout local worker — run this on YOUR machine to power the public site.

Several retailers sit behind Cloudflare, which judges datacenter IPs far more
harshly than home connections: the hosted app gets 403s where your own machine
is served normally. So instead of disguising where requests come from, the
scrapers just run where they already work — here.

The worker polls the hosted app for search jobs, runs the real site clients
locally, and posts the parsed listings back. It sends only extracted listings,
never raw HTML, which keeps the upload leg small (a big search is ~1 MB back
instead of tens of MB).

Security: the app hands this process SEARCH CRITERIA, never URLs. Every
request it makes is additionally checked by guard.py against a hardcoded host
allowlist, a public-IP requirement (so nothing can reach your LAN), a redirect
validator, size caps and a rate limit — all enforced HERE, so a compromised
server cannot widen them. Requests are written to worker_egress.log.

It makes only outbound connections: no listening port, no port forwarding.

Usage:
    python worker.py                       # uses GS_PUBLIC_URL / defaults
    python worker.py --url https://…       # target a specific deployment
    python worker.py --once                # run one poll cycle and exit

Config (env vars):
    GS_PUBLIC_URL   deployment base URL (default below)
    GS_WORKER_KEY   must match the same var on the deployment
"""
import argparse
import json
import logging
import os
import platform
import sys
import threading
import time
from dataclasses import asdict
from urllib.parse import urlsplit

import requests

import guard

log = logging.getLogger("gun_scout.worker")

DEFAULT_URL = "https://gun-scout.onrender.com"
IDLE_POLL_S = 3          # between "nothing to do" polls
ERROR_BACKOFF_S = 15     # after a failed poll (deployment asleep/restarting)
RESULT_BATCH = 100       # listings per upload
MAX_PARALLEL_JOBS = 2    # concurrent site scrapes; keeps home bandwidth sane
TOKEN_REFRESH_S = 1800   # how often to top up the shared Cabela's token


class Worker:
    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        if key:
            self.session.headers["X-Worker-Key"] = key
        self.worker_id = f"{platform.node()}-{os.getpid()}"
        self._sem = threading.Semaphore(MAX_PARALLEL_JOBS)

    # ---- transport -------------------------------------------------------

    def _post(self, path: str, body: dict, timeout: int = 60) -> dict | None:
        try:
            r = self.session.post(f"{self.url}{path}", data=json.dumps(body),
                                  timeout=timeout)
        except requests.RequestException as e:
            log.warning("%s failed: %s", path, e)
            return None
        if r.status_code in (403, 503):
            log.error("%s refused (HTTP %d): %s", path, r.status_code,
                      (r.text or "")[:200])
            return None
        if r.status_code >= 400:
            log.warning("%s -> HTTP %d", path, r.status_code)
            return None
        try:
            return r.json()
        except ValueError:
            return None

    # ---- job execution ---------------------------------------------------

    def run_job(self, job: dict):
        from clients import REGISTRIES, ClientBlocked, StructureError
        from models import SearchCriteria

        job_id, site = job["job_id"], job["site"]
        criteria = SearchCriteria.from_dict(job.get("criteria") or {})
        cls = REGISTRIES.get(criteria.vertical, {}).get(site)
        if cls is None:
            self._post("/api/worker/complete",
                       {"job_id": job_id, "status": "error",
                        "message": f"worker has no client for {site}"})
            return

        batch: list[dict] = []
        sent = 0

        def flush():
            nonlocal batch, sent
            if not batch:
                return
            res = self._post("/api/worker/results",
                             {"job_id": job_id, "listings": batch})
            sent += (res or {}).get("accepted", 0)
            batch = []

        def emit(listing):
            batch.append(asdict(listing))
            if len(batch) >= RESULT_BATCH:
                flush()

        status, message = "done", ""
        t0 = time.time()
        try:
            cls().search(criteria, emit)
        except ClientBlocked as e:
            status, message = "blocked", str(e)
        except StructureError as e:
            status, message = "schema", str(e)
        except guard.BlockedByGuard as e:
            # the guard refused something — surface it plainly, never retry
            status, message = "error", f"blocked by local egress guard: {e}"
            log.warning("guard blocked %s: %s", site, e)
        except Exception as e:
            status, message = "error", f"{type(e).__name__}: {e}"
            log.exception("client %s failed", site)
        finally:
            flush()
        log.info("%-12s %-9s %4d listings in %5.1fs", site, status, sent,
                 time.time() - t0)
        self._post("/api/worker/complete",
                   {"job_id": job_id, "status": status, "message": message})

    # ---- main loop -------------------------------------------------------

    def sites(self) -> list[str]:
        from clients import REGISTRIES
        return sorted({name for reg in REGISTRIES.values() for name in reg})

    def poll_once(self) -> int:
        res = self._post("/api/worker/claim",
                         {"worker_id": self.worker_id, "sites": self.sites()})
        if res is None:
            return -1
        jobs = res.get("jobs") or []
        for job in jobs:
            self._sem.acquire()

            def run(j=job):
                try:
                    self.run_job(j)
                finally:
                    self._sem.release()

            threading.Thread(target=run, daemon=True,
                             name=f"job-{job['job_id']}").start()
        return len(jobs)

    def loop(self):
        log.info("worker %s -> %s", self.worker_id, self.url)
        log.info("serving sites: %s", ", ".join(self.sites()))
        while True:
            n = self.poll_once()
            time.sleep(ERROR_BACKOFF_S if n < 0 else (IDLE_POLL_S if n == 0 else 0.5))


def _token_loop(url: str, key: str):
    """Keep the deployment's shared Cabela's token fresh. Only this machine can
    mint it (a real browser on a cabelas.com page), so the worker owns it."""
    import subprocess
    while True:
        try:
            subprocess.run([sys.executable, "push_token.py", "--url", url],
                           cwd=os.path.dirname(os.path.abspath(__file__)),
                           capture_output=True, timeout=600)
        except Exception:
            log.exception("cabelas token refresh failed")
        time.sleep(TOKEN_REFRESH_S)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("GS_PUBLIC_URL", DEFAULT_URL))
    ap.add_argument("--once", action="store_true", help="one poll cycle, then exit")
    ap.add_argument("--no-token", action="store_true",
                    help="skip the Cabela's token refresher")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    key = os.environ.get("GS_WORKER_KEY", "")
    if not key:
        log.warning("GS_WORKER_KEY is not set — fine for a local test, but the "
                    "public deployment will refuse this worker")

    worker = Worker(args.url, key)
    # Arm the egress guard. The deployment we poll is exempted explicitly —
    # from THIS local config, never from anything the server tells us.
    guard.set_control_host(urlsplit(args.url).hostname or "")
    guard.enable()
    guard.install_requests_guard()

    if not args.no_token:
        threading.Thread(target=_token_loop, args=(args.url, key), daemon=True,
                         name="cabelas-token").start()

    if args.once:
        n = worker.poll_once()
        log.info("claimed %d job(s)", max(0, n))
        time.sleep(5)
        return 0
    try:
        worker.loop()
    except KeyboardInterrupt:
        log.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
