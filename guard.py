"""Egress guard — the safety envelope around the local worker.

The worker runs scrapers on a home connection on behalf of a PUBLIC website,
so every request it makes is attributable to the operator's own IP. That makes
this module a security boundary, not a nicety, and it is deliberately built on
one principle:

    THE SERVER IS THE UNTRUSTED END.

The hosted app tells the worker *what to search for*, never *what URL to
fetch*. Every restriction below is enforced here, inside the worker process,
so a compromised server (or a bug in the app, or a leaked worker key) cannot
widen them. Concretely this stops:

  * arbitrary-URL relay — the classic open-proxy hole. Hosts must be on
    ALLOWED_HOSTS, which is a constant in this file, not configuration.
  * pivoting into the home LAN (router admin pages, NAS, printers, IoT) —
    every resolved address must be publicly routable, so `192.168.x.x`,
    localhost and link-local are refused even if DNS points at them.
  * redirect laundering — an allowed host answering `302 http://192.168.1.1/`
    is refused BEFORE the connection is made, never merely detected after.
  * bandwidth/data exhaustion — per-response size caps plus a token-bucket
    rate limit on both requests and bytes.

Everything it allows is written to an audit log, so if the operator's IP ever
turns up in someone's abuse report there is a local record of exactly what
this process did.

Disabled by default: only the worker calls enable(). The hosted app and normal
local runs are unaffected.
"""
import ipaddress
import logging
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("gun_scout.guard")

# Hostnames the worker may talk to: the eight retailers plus the two
# search-as-a-service hosts their own frontends query (guns.com -> Algolia,
# Cabela's -> Coveo). Suffix-matched, so "www.guns.com" and "guns.com" both
# pass while "evil-guns.com" does not. A constant on purpose — if this were
# configurable the server could widen it, which is the whole thing we are
# preventing.
ALLOWED_HOSTS = (
    "guns.com",
    "gunsamerica.com",
    "sportsmans.com",
    "gun.deals",
    "gunmade.com",
    "palmettostatearmory.com",
    "cabelas.com",
    "basspro.com",
    "api.gunbroker.com",
    # search backends the sites themselves call
    "algolia.net",
    "algolianet.com",
    "org.coveo.com",
)

MAX_RESPONSE_BYTES = 12 * 1024 * 1024   # no single page should approach this
MAX_REDIRECTS = 3

# Token-bucket limits (per worker process). Sized for "a handful of people
# searching", not for a scraping farm: the point is that no amount of public
# traffic can turn the home connection into a firehose.
MAX_REQUESTS_PER_MIN = 240
MAX_BYTES_PER_MIN = 150 * 1024 * 1024

AUDIT_LOG = Path(__file__).parent / "worker_egress.log"
AUDIT_MAX_BYTES = 5 * 1024 * 1024


class BlockedByGuard(Exception):
    """A request was refused by the egress guard. Never retried."""


_enabled = False
_control_host = ""      # the worker's own deployment (see set_control_host)
_lock = threading.Lock()
_window_start = 0.0
_reqs_in_window = 0
_bytes_in_window = 0


def enable():
    """Turn the guard on for this process (worker only)."""
    global _enabled
    _enabled = True
    log.info("egress guard active: %d allowed host suffixes, %d req/min, %d MB/min",
             len(ALLOWED_HOSTS), MAX_REQUESTS_PER_MIN, MAX_BYTES_PER_MIN // (1024 * 1024))


def is_enabled() -> bool:
    return _enabled


def set_control_host(host: str):
    """Exempt the worker's own control-plane host — the deployment it polls.

    Safe because this comes from LOCAL operator config (--url / GS_PUBLIC_URL),
    never from anything the server says: the server cannot nominate itself, so
    the 'server can't widen the allowlist' property still holds. Exempt from
    the public-address check too, so a worker can be pointed at
    http://127.0.0.1:8777 for testing.
    """
    global _control_host
    _control_host = (host or "").lower().rstrip(".")


def is_control_host(host: str) -> bool:
    return bool(_control_host) and (host or "").lower().rstrip(".") == _control_host


def _host_allowed(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    return any(host == a or host.endswith("." + a) for a in ALLOWED_HOSTS)


def _addresses_public(host: str) -> None:
    """Resolve `host` and refuse if ANY answer is not publicly routable.

    This is what keeps the worker out of the home network: a hostname that
    resolves to 192.168.1.1, 127.0.0.1 or a link-local address is rejected
    before we connect. We check every answer, not just the first, so a
    round-robin record can't sneak a private address through.

    (A determined DNS-rebinding attack could still flip the record between
    this check and the connection. Doing better needs connect-time address
    pinning, which the curl subprocess path can't express; the allowlist is
    the real protection here, since it means an attacker would have to own a
    major retailer's DNS to get that far.)
    """
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedByGuard(f"cannot resolve {host}: {e}") from e
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])  # strip zone id
        except ValueError:
            raise BlockedByGuard(f"{host} resolved to unparseable address {addr}")
        if not ip.is_global or ip.is_multicast:
            raise BlockedByGuard(
                f"{host} resolves to non-public address {ip} — refusing "
                "(possible attempt to reach the local network)")


def _rate_limit(nbytes: int = 0) -> None:
    """Fixed-window token bucket over requests and bytes. Raises rather than
    sleeping: a blocked search should fail fast and visibly, not silently
    stall the whole worker behind one abusive caller."""
    global _window_start, _reqs_in_window, _bytes_in_window
    now = time.time()
    with _lock:
        if now - _window_start >= 60:
            _window_start, _reqs_in_window, _bytes_in_window = now, 0, 0
        if _reqs_in_window >= MAX_REQUESTS_PER_MIN:
            raise BlockedByGuard(
                f"worker rate limit: {MAX_REQUESTS_PER_MIN} requests/min reached")
        if _bytes_in_window >= MAX_BYTES_PER_MIN:
            raise BlockedByGuard(
                f"worker rate limit: {MAX_BYTES_PER_MIN // (1024*1024)} MB/min reached")
        _reqs_in_window += 1
        _bytes_in_window += nbytes


def check_url(url: str) -> str:
    """Validate a URL about to be requested. Returns the hostname.
    Raises BlockedByGuard if it fails any rule. No-op when disabled."""
    if not _enabled:
        return urlsplit(url).hostname or ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedByGuard(f"refusing non-HTTP scheme: {parts.scheme!r}")
    host = parts.hostname or ""
    if is_control_host(host):
        return host          # our own deployment; not scraping traffic
    if not _host_allowed(host):
        raise BlockedByGuard(f"host not on the worker allowlist: {host!r}")
    _addresses_public(host)
    _rate_limit()
    return host


def check_redirect(from_url: str, to_url: str, hop: int) -> None:
    """Validate a redirect target BEFORE following it."""
    if not _enabled:
        return
    if hop > MAX_REDIRECTS:
        raise BlockedByGuard(f"too many redirects (>{MAX_REDIRECTS}) from {from_url}")
    check_url(to_url)


def note_response(url: str, status: int, nbytes: int) -> None:
    """Record a completed response: counts its bytes toward the rate limit,
    enforces the size cap, and appends to the audit log."""
    if not _enabled or is_control_host(urlsplit(url).hostname or ""):
        return
    _audit(url, status, nbytes)
    with _lock:
        global _bytes_in_window
        _bytes_in_window += nbytes
    if nbytes > MAX_RESPONSE_BYTES:
        raise BlockedByGuard(
            f"response from {url} exceeded {MAX_RESPONSE_BYTES} bytes ({nbytes})")


def _audit(url: str, status: int, nbytes: int) -> None:
    """Append one line per request to a size-capped local log."""
    try:
        if AUDIT_LOG.exists() and AUDIT_LOG.stat().st_size > AUDIT_MAX_BYTES:
            AUDIT_LOG.replace(AUDIT_LOG.with_suffix(".log.1"))
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with AUDIT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp}\t{status}\t{nbytes}\t{url[:400]}\n")
    except OSError:
        pass  # auditing must never break a search


def install_requests_guard() -> None:
    """Route `requests` through the guard too.

    Most clients fetch via fetcher.fetch(), but the GunBroker and Cabela's
    clients call `requests` directly. Rather than trusting each client (and
    every client added later) to opt in, wrap the library's own choke point
    so nothing in this process can reach the network unchecked.
    """
    import requests.adapters

    original = requests.adapters.HTTPAdapter.send

    def guarded_send(self, request, **kwargs):
        check_url(request.url)
        resp = original(self, request, **kwargs)
        body = resp.content or b""
        note_response(request.url, resp.status_code, len(body))
        return resp

    if getattr(requests.adapters.HTTPAdapter.send, "_gs_guarded", False):
        return
    guarded_send._gs_guarded = True
    requests.adapters.HTTPAdapter.send = guarded_send
    log.info("requests library routed through the egress guard")
