"""HTTP fetch helper.

guns.com (Akamai) fingerprints Python's TLS stack and 403s `requests`, but the
curl binary that ships with Windows 10+ passes. So: try curl.exe first, fall
back to requests for anything curl can't do / non-Windows machines.

Every request passes through guard.check_url() first. That is a no-op in the
hosted app and in normal local runs, and a hard security boundary in the
worker — see guard.py. When the guard is active we also stop following
redirects automatically, so each hop can be validated BEFORE we connect to it
rather than after.
"""
import shutil
import subprocess
from urllib.parse import urljoin

import requests

import guard

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

CURL = shutil.which("curl") or shutil.which("curl.exe")

_REDIRECT_CODES = (301, 302, 303, 307, 308)


class FetchError(Exception):
    def __init__(self, status: int, msg: str = ""):
        self.status = status
        super().__init__(msg or f"HTTP {status}")


def fetch(url: str, timeout: int = 30, data: str | None = None,
          headers: dict | None = None, impersonate: str | None = None,
          _hop: int = 0) -> str:
    """Fetch url, return body text. GET by default; if `data` is given, POST it.
    curl.exe first (passes the Akamai/Cloudflare TLS fingerprinting that blocks
    Python's `requests` on guns.com/gunsamerica.com), falling back to requests.
    Raises FetchError with .status on HTTP errors.

    `impersonate` (e.g. "chrome124") routes the request through curl_cffi, which
    forges a full browser TLS+HTTP2 fingerprint. Some hosts (sportsmans.com moved
    behind a Cloudflare JS challenge in 2026) 403 the Windows Schannel curl but
    pass a genuine Chrome fingerprint. Falls back to the normal path if curl_cffi
    isn't installed or errors before returning a response."""
    guard.check_url(url)                 # raises BlockedByGuard in worker mode
    no_follow = guard.is_enabled()
    hdrs = {"User-Agent": UA, **(headers or {})}
    status = None
    body = ""
    redirect = ""

    if impersonate:
        try:
            from curl_cffi import requests as _cffi
        except ImportError:
            _cffi = None
        if _cffi is not None:
            kw = {"headers": headers or None, "impersonate": impersonate,
                  "timeout": timeout, "allow_redirects": not no_follow}
            resp = (_cffi.post(url, data=data, **kw) if data is not None
                    else _cffi.get(url, **kw))
            status, body = resp.status_code, resp.text
            redirect = resp.headers.get("location", "") if status in _REDIRECT_CODES else ""
            guard.note_response(url, status, len(body or ""))
            if status < 400 and not redirect:
                return body
        # curl_cffi unavailable -> fall through to the curl/requests path

    if status is None and CURL:
        cmd = [CURL, "-s", "--compressed", "--max-time", str(timeout),
               "-A", UA, "-w", "\n%{http_code}\t%{redirect_url}"]
        if not no_follow:
            cmd.append("-L")
        else:
            cmd += ["--max-filesize", str(guard.MAX_RESPONSE_BYTES)]
        # Prefer Brotli: some hosts (gunmade.com's __data.json) serve a payload
        # that is ~40% smaller under `br` than `gzip`. curl was built with
        # brotli and `--compressed` decompresses it transparently, so the body
        # we read is still plain text. Only set it when the caller hasn't, and
        # only for curl — the requests fallback below can't decode brotli, so
        # it keeps its default gzip/deflate negotiation.
        if not any(k.lower() == "accept-encoding" for k in (headers or {})):
            cmd += ["-H", "Accept-Encoding: br, gzip, deflate"]
        for k, v in (headers or {}).items():
            cmd += ["-H", f"{k}: {v}"]
        if data is not None:
            cmd += ["-X", "POST", "--data-binary", data]
        cmd.append(url)
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout + 10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode == 0:
            body, _, tail = proc.stdout.decode("utf-8", errors="replace").rpartition("\n")
            code, _, redirect = tail.partition("\t")
            status = int(code or 0)
            redirect = redirect.strip()
            guard.note_response(url, status, len(body))
            if status < 400 and not redirect:
                return body
            # an HTTP error from curl is final for this path — do NOT re-run
            # the same request through requests, which looks even less like a
            # browser. Falls through to the redirect / 403 retry / raise below.
        # curl itself failed (network etc.) -> leave status None, try requests

    if status is None:
        kw = {"headers": hdrs, "timeout": timeout, "allow_redirects": not no_follow}
        resp = (requests.post(url, data=data, **kw) if data is not None
                else requests.get(url, **kw))
        status, body = resp.status_code, resp.text
        redirect = resp.headers.get("location", "") if status in _REDIRECT_CODES else ""
        guard.note_response(url, status, len(body or ""))
        if status < 400 and not redirect:
            return body

    if redirect:
        # Only reachable with the guard on (otherwise curl/requests followed it
        # already). Validate the target before connecting to it.
        target = urljoin(url, redirect)
        guard.check_redirect(url, target, _hop + 1)
        return fetch(target, timeout=timeout, data=data, headers=headers,
                     impersonate=impersonate, _hop=_hop + 1)

    if status == 403 and not impersonate:
        # Last resort: retry once with a full forged Chrome TLS+HTTP2
        # fingerprint. Plain curl/requests are obvious non-browsers to
        # Cloudflare, which is far stricter from datacenter IPs than from a
        # home connection — so a fetch that works locally can 403 once hosted.
        # Only ever runs on a 403, so working sites are untouched.
        try:
            return fetch(url, timeout=timeout, data=data, headers=headers,
                         impersonate="chrome124", _hop=_hop)
        except FetchError:
            pass  # impersonation didn't help either; report the original 403
    raise FetchError(status)
