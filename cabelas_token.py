"""Automatic Cabela's Coveo token manager.

Cabela's search runs on Coveo, which needs a short-lived (~4h) bearer token.
That token can only be minted by a real browser (Akamai blocks server-side
requests), so `cabelas_minter/mint.js` drives a headed Playwright/Edge session
to capture one. This module is the brain around that:

  * caches the token + when it was minted + when it expires in a JSON store,
  * hands out a valid token on demand, and
  * transparently re-runs the minter whenever the cached token is missing,
    expired, or within a safety buffer of expiring — so callers never deal with
    token freshness themselves.

Run it directly to mint/verify without touching the app:
    python cabelas_token.py            # ensure a fresh token, print status
    python cabelas_token.py --force    # mint a new one even if current is fine
    python cabelas_token.py --status    # show cache state, don't mint
"""
import base64
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("gun_scout")

_ROOT = Path(__file__).parent
STORE = _ROOT / "cabelas_token.json"
MINTER_DIR = _ROOT / "cabelas_minter"
MINTER_JS = MINTER_DIR / "mint.js"

# Remint this many seconds BEFORE the JWT actually expires, so a token never
# dies mid-search.
SAFETY_BUFFER_S = 300
# Hard ceiling on how long the headed browser mint may take (a warm profile is
# ~60s; a cold first run with the interaction fallback can be a few minutes).
MINT_TIMEOUT_S = 300
# After a mint FAILS, don't retry for this long — otherwise every queued and
# incoming search thread serializes into back-to-back ~5-minute browser
# launches (a failure stampede). During the cooldown the cached error is raised
# immediately so searches fail fast instead of hanging.
MINT_FAIL_COOLDOWN_S = 120

# One mint at a time across all search threads. The Flask app is single-process
# (threaded), so a threading.Lock is enough; we double-check freshness inside it
# so only the first thread to find a stale token pays the mint cost.
_lock = threading.Lock()
_last_fail = {"at": 0.0, "msg": ""}   # negative cache, guarded by _lock


class MintError(Exception):
    """The minter could not produce a token (browser/deps/Akamai failure)."""


# ---- token store ---------------------------------------------------------

def _read_store() -> dict:
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _write_store(d: dict):
    STORE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _jwt_org_exp(token: str) -> tuple[str, float | None]:
    """Read organization + expiry from the (unverified) Coveo JWT payload."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("organization") or "", data.get("exp")
    except (IndexError, ValueError, json.JSONDecodeError):
        return "", None


def _fresh(store: dict) -> bool:
    tok = store.get("token")
    exp = store.get("expires_at")
    return bool(tok) and bool(exp) and time.time() < exp - SAFETY_BUFFER_S


# ---- minting -------------------------------------------------------------

def _run_minter() -> dict:
    """Invoke the Node/Playwright minter and return a fresh store dict.
    Raises MintError with an actionable message on any failure."""
    if not MINTER_JS.exists():
        raise MintError(
            f"minter not found at {MINTER_JS}. Set up once with: "
            f"cd {MINTER_DIR} && npm install && npx playwright install msedge")
    node = _which_node()
    if not node:
        raise MintError("Node.js not on PATH — the token minter needs it "
                        "(install from nodejs.org).")

    log.info("cabelas: minting a fresh Coveo token (headed browser, ~1 min)…")
    t0 = time.time()
    # Popen (not run) so that on timeout we can kill the WHOLE process tree —
    # subprocess.run's timeout kills only node, orphaning the headed Edge
    # browser, which then keeps the persistent profile locked and breaks every
    # future mint. On Windows taskkill /T terminates the child browser tree too.
    proc = subprocess.Popen(
        [node, str(MINTER_JS)],
        cwd=str(MINTER_DIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        stdout, stderr = proc.communicate(timeout=MINT_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        _kill_tree(proc)
        raise MintError(f"minter timed out after {MINT_TIMEOUT_S}s "
                        "(browser process tree killed)") from e

    if proc.returncode == 2:
        raise MintError("browser ran but Akamai never yielded a token "
                        "(bot-blocked). stderr tail: " + _tail(stderr))
    if proc.returncode != 0:
        raise MintError(f"minter exited {proc.returncode}. stderr tail: "
                        + _tail(stderr))
    try:
        out = json.loads((stdout or "").strip().splitlines()[-1])
        token = out["token"]
    except (ValueError, KeyError, IndexError) as e:
        raise MintError(f"minter output not parseable ({e}): "
                        + _tail(proc.stdout)) from e

    org, exp = _jwt_org_exp(token)
    if not org or not exp:
        raise MintError("minted token is not a valid Coveo JWT")
    store = {
        "token": token,
        "org": org,
        "minted_at": time.time(),
        "expires_at": float(exp),
        "mint_seconds": round(time.time() - t0, 1),
    }
    _write_store(store)
    log.info("cabelas: token minted in %.0fs, valid until %s",
             time.time() - t0, _iso(exp))
    return store


# ---- public API ----------------------------------------------------------

def get_valid_token(force: bool = False) -> str:
    """Return a currently-valid Coveo token, minting a new one if the cached
    token is missing/stale (or force=True). Raises MintError if a fresh token
    can't be produced.

    A manual override wins: if CABELAS_COVEO_TOKEN is set we use that token and
    never launch a browser (lets a user paste one, or a CI box inject one,
    without Node/Playwright installed) — but we still honour its expiry, so an
    expired override fails loudly instead of silently returning a dead token."""
    env_tok = os.environ.get("CABELAS_COVEO_TOKEN", "").strip()
    if env_tok:
        _, exp = _jwt_org_exp(env_tok)
        if exp and time.time() >= exp - SAFETY_BUFFER_S:
            raise MintError(
                "CABELAS_COVEO_TOKEN is set but expired — update the env var "
                "(or unset it to let the browser minter take over).")
        return env_tok

    store = _read_store()
    if not force and _fresh(store):
        return store["token"]

    with _lock:
        # re-read inside the lock: another thread may have just minted
        store = _read_store()
        if not force and _fresh(store):
            return store["token"]
        # negative cache: after a recent mint failure, fail fast for a cooldown
        # instead of every waiting thread launching its own ~5-min browser mint
        since = time.time() - _last_fail["at"]
        if since < MINT_FAIL_COOLDOWN_S:
            raise MintError(
                f"mint failed {int(since)}s ago, backing off "
                f"{MINT_FAIL_COOLDOWN_S - int(since)}s: {_last_fail['msg']}")
        try:
            token = _run_minter()["token"]
        except MintError as e:
            _last_fail.update(at=time.time(), msg=str(e))
            raise
        _last_fail.update(at=0.0, msg="")
        return token


def invalidate(failed_token: str | None = None):
    """Drop the cached token so the next get_valid_token() re-mints. Called when
    Coveo rejects a token that looked fresh (clock skew, early revocation).

    Pass the exact token that was rejected: if the cache has already moved on to
    a different (freshly minted) token, we leave it alone — otherwise a lagging
    401 from an old in-flight request would nuke a perfectly good new token and
    trigger a needless re-mint."""
    with _lock:
        store = _read_store()
        if not store.get("token"):
            return
        if failed_token and store["token"] != failed_token:
            return  # cache already rotated to a newer token; keep it
        store.pop("token", None)
        store["expires_at"] = 0
        _write_store(store)


def status() -> dict:
    """Non-mutating snapshot of the token cache for diagnostics/health."""
    env_tok = os.environ.get("CABELAS_COVEO_TOKEN", "").strip()
    if env_tok:
        _, exp = _jwt_org_exp(env_tok)
        return {"source": "env override", "expires_at": _iso(exp),
                "fresh": bool(exp) and time.time() < exp - SAFETY_BUFFER_S}
    store = _read_store()
    if not store.get("token"):
        return {"source": "none", "fresh": False}
    exp = store.get("expires_at") or 0
    return {
        "source": "minted",
        "fresh": _fresh(store),
        "minted_at": _iso(store.get("minted_at")),
        "expires_at": _iso(exp),
        "expires_in_s": int(exp - time.time()),
        "mint_seconds": store.get("mint_seconds"),
        "org": store.get("org"),
    }


# ---- helpers -------------------------------------------------------------

def _which_node() -> str | None:
    import shutil
    return shutil.which("node") or shutil.which("node.exe")


def _kill_tree(proc: "subprocess.Popen"):
    """Kill a process AND its children (the headed browser the minter spawned).
    subprocess's own kill() only targets the direct child (node)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            proc.kill()
    except Exception as e:  # never let cleanup mask the original failure
        log.warning("cabelas: failed to kill minter process tree: %s", e)
    finally:
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


def _tail(s: str, n: int = 240) -> str:
    return (s or "").strip()[-n:]


def _iso(epoch) -> str | None:
    if not epoch:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if "--status" in sys.argv:
        print(json.dumps(status(), indent=2))
        sys.exit(0)
    force = "--force" in sys.argv
    try:
        tok = get_valid_token(force=force)
    except MintError as e:
        print(f"MINT FAILED: {e}")
        sys.exit(1)
    # verify the token actually answers Coveo before declaring success
    import requests
    org, _ = _jwt_org_exp(tok)
    r = requests.post(
        f"https://{org}.org.coveo.com/rest/search/v2?organizationId={org}",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        data=json.dumps({"q": "30-06", "aq": "@isgun==1", "numberOfResults": 1}),
        timeout=30)
    ok = r.status_code == 200
    print(json.dumps({
        "verified": ok,
        "coveo_status": r.status_code,
        "sample_total": r.json().get("totalCount") if ok else None,
        **status(),
    }, indent=2))
    sys.exit(0 if ok else 1)
