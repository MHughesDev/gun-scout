"""Shared Cabela's Coveo token manager (DB-backed, crowd-refreshed).

Cabela's search runs on Coveo, which needs a short-lived (~4h) bearer token.
The token can only be minted by a real browser (Akamai blocks server-side
requests) — but the mint endpoint is CORS-open, so ANY visitor's browser can
fetch one from our own pages. The lifecycle in production:

  * The one current token lives in the app database (statstore kv table) and
    is shared by every user — nobody configures anything.
  * The search page checks GET /api/cabelas/token; only when the shared token
    is aged does the visitor's browser silently mint a fresh one straight
    from cabelas.com and POST it back. accept_token() validates it hard
    (pinned Coveo org + live catalog probe) before it may replace the shared
    token, so a bad or malicious push can never poison the shared slot.
  * On a dev box with Node, `cabelas_minter/mint.js` (headed Playwright/Edge)
    still mints transparently server-side as a fallback.

Run it directly to mint/verify without touching the app:
    python cabelas_token.py            # ensure a fresh token, print status
    python cabelas_token.py --force    # mint a new one even if current is fine
    python cabelas_token.py --status   # show cache state, don't mint
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
LEGACY_STORE = _ROOT / "cabelas_token.json"   # pre-DB cache; imported once
MINTER_DIR = _ROOT / "cabelas_minter"
MINTER_JS = MINTER_DIR / "mint.js"

_KV_KEY = "cabelas_coveo_token"

# The public CORS-open endpoint a visitor's browser mints from (served to the
# frontend via /api/cabelas/token so it lives in exactly one place).
MINT_URL = "https://www.cabelas.com/api/v2/10651/prod/coveo/getCoveoToken"

# Tokens are only accepted for Bass Pro/Cabela's production Coveo org — a
# token for any other org would pass a naive "does it answer Coveo" check
# while pointing our searches at someone else's index.
EXPECTED_ORG = "bassproshopsproductionl92epymr"

# Remint this many seconds BEFORE the JWT actually expires, so a token never
# dies mid-search.
SAFETY_BUFFER_S = 300
# Ask visitors' browsers to refresh when less than this is left, so the
# shared token rolls over before anything is actually blocked on it.
REFRESH_AHEAD_S = 900
# Hard ceiling on how long the headed browser mint may take (a warm profile is
# ~60s; a cold first run with the interaction fallback can be a few minutes).
MINT_TIMEOUT_S = 300
# After a mint FAILS, don't retry for this long — otherwise every queued and
# incoming search thread serializes into back-to-back ~5-minute browser
# launches (a failure stampede). During the cooldown the cached error is raised
# immediately so searches fail fast instead of hanging.
MINT_FAIL_COOLDOWN_S = 120

# One mint/accept at a time across all threads. The app is single-process, so
# a threading.Lock is enough; freshness is double-checked inside it.
_lock = threading.Lock()
_last_fail = {"at": 0.0, "msg": ""}   # negative cache, guarded by _lock


class MintError(Exception):
    """No valid token available and none could be produced right now."""


# ---- token store (app DB, shared across all users) ------------------------

def _read_store() -> dict:
    import statstore
    raw = statstore.ENGINE.kv_get(_KV_KEY)
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    # one-time import of the pre-DB file cache so a dev box keeps its token
    try:
        legacy = json.loads(LEGACY_STORE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    if legacy.get("token"):
        try:
            _write_store(legacy)
        except RuntimeError:
            pass  # engine not started (bare CLI); just use it in-memory
    return legacy


def _write_store(d: dict):
    import statstore
    statstore.ENGINE.kv_set(_KV_KEY, json.dumps(d))


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


# ---- minting (local dev fallback: headed browser via Node/Playwright) ------

def _minter_available() -> bool:
    """Only true on a dev box that actually set the minter up. Node alone is
    not enough — hosted images ship Node but never `npm install`ed the
    minter's deps, and spawning it there just crashes with MODULE_NOT_FOUND
    and burns the failure cooldown."""
    return (MINTER_JS.exists() and (MINTER_DIR / "node_modules").is_dir()
            and _which_node() is not None)


def _run_minter() -> dict:
    """Invoke the Node/Playwright minter and return a fresh store dict.
    Raises MintError with an actionable message on any failure."""
    node = _which_node()
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
                        + _tail(stdout)) from e

    org, exp = _jwt_org_exp(token)
    if not org or not exp:
        raise MintError("minted token is not a valid Coveo JWT")
    store = {
        "token": token,
        "org": org,
        "minted_at": time.time(),
        "expires_at": float(exp),
        "mint_seconds": round(time.time() - t0, 1),
        "source": "local minter",
    }
    _write_store(store)
    log.info("cabelas: token minted in %.0fs, valid until %s",
             time.time() - t0, _iso(exp))
    return store


# ---- public API -----------------------------------------------------------

def get_valid_token(force: bool = False) -> str:
    """Return a currently-valid Coveo token from the shared DB store, minting
    a new one via the local browser minter if possible (or force=True).
    Raises MintError when no valid token can be produced — in the hosted app
    that resolves itself as soon as any visitor's browser refreshes it."""
    store = _read_store()
    if not force and _fresh(store):
        return store["token"]

    if not _minter_available():
        raise MintError(
            "the shared Cabela's token is stale; it refreshes automatically "
            "when someone opens the Gun Scout search page in a browser "
            "(or run `python cabelas_token.py` on a machine with Node).")

    with _lock:
        # re-read inside the lock: another thread/visitor may have just minted
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


# Coveo probe used to vet visitor-supplied tokens: must answer for the pinned
# org AND report a plausibly-sized gun catalog, or the push is rejected.
_PROBE_TIMEOUT_S = 20
_MIN_CATALOG_DOCS = 100


def accept_token(token: str) -> tuple[bool, str]:
    """Validate a visitor-supplied token; store it as the shared token only if
    it passes. Returns (accepted, message). Never raises."""
    token = (token or "").strip()
    if not token or len(token) > 4096 or token.count(".") != 2:
        return False, "not a JWT"
    org, exp = _jwt_org_exp(token)
    if org != EXPECTED_ORG:
        return False, "token is not for Cabela's search organization"
    if not exp or time.time() > exp - SAFETY_BUFFER_S:
        return False, "token is already (or nearly) expired"

    with _lock:
        store = _read_store()
        if _fresh(store) and (store.get("expires_at") or 0) >= exp:
            return True, "shared token already fresh"  # keep the newer one
        import requests
        try:
            r = requests.post(
                f"https://{org}.org.coveo.com/rest/search/v2?organizationId={org}",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                data=json.dumps({"q": "", "aq": "@isgun==1",
                                 "numberOfResults": 1}),
                timeout=_PROBE_TIMEOUT_S)
        except requests.RequestException as e:
            return False, f"could not verify token against Coveo: {e}"
        if r.status_code != 200:
            return False, f"Coveo rejected the token (HTTP {r.status_code})"
        try:
            total = r.json().get("totalCount")
        except ValueError:
            return False, "Coveo verification response was not JSON"
        if not isinstance(total, int) or total < _MIN_CATALOG_DOCS:
            return False, "token answered but not for the expected catalog"
        try:
            _write_store({"token": token, "org": org, "minted_at": time.time(),
                          "expires_at": float(exp), "source": "visitor"})
        except RuntimeError as e:
            return False, str(e)
        log.info("cabelas: accepted visitor-refreshed token, valid until %s",
                 _iso(exp))
        return True, "token accepted — thanks, it's now shared by everyone"


def public_status() -> dict:
    """What the frontend needs to run the crowd-refresh loop. No token value
    ever leaves the server this way."""
    store = _read_store()
    exp = store.get("expires_at") or 0
    left = int(exp - time.time()) if store.get("token") else 0
    fresh = _fresh(store)
    return {
        "fresh": fresh,
        "expires_in_s": max(0, left),
        "needs_refresh": (not fresh) or left < REFRESH_AHEAD_S,
        "mint_url": MINT_URL,
    }


def invalidate(failed_token: str | None = None):
    """Drop the shared token so the next request re-mints / re-collects.
    Called when Coveo rejects a token that looked fresh (clock skew, early
    revocation).

    Pass the exact token that was rejected: if the store has already moved on
    to a different (freshly collected) token, we leave it alone — otherwise a
    lagging 401 from an old in-flight request would nuke a perfectly good new
    token and trigger a needless refresh."""
    with _lock:
        store = _read_store()
        if not store.get("token"):
            return
        if failed_token and store["token"] != failed_token:
            return  # store already rotated to a newer token; keep it
        store.pop("token", None)
        store["expires_at"] = 0
        try:
            _write_store(store)
        except RuntimeError:
            pass


def status() -> dict:
    """Non-mutating snapshot of the token store for diagnostics/health."""
    store = _read_store()
    if not store.get("token"):
        return {"source": "none", "fresh": False}
    exp = store.get("expires_at") or 0
    return {
        "source": store.get("source") or "minted",
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
    import statstore
    statstore.start()   # the token store lives in the app DB now
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
    statstore.ENGINE.flush()
    sys.exit(0 if ok else 1)
