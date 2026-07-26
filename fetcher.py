"""HTTP fetch helper.

guns.com (Akamai) fingerprints Python's TLS stack and 403s `requests`, but the
curl binary that ships with Windows 10+ passes. So: try curl.exe first, fall
back to requests for anything curl can't do / non-Windows machines.
"""
import shutil
import subprocess

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

CURL = shutil.which("curl") or shutil.which("curl.exe")


class FetchError(Exception):
    def __init__(self, status: int, msg: str = ""):
        self.status = status
        super().__init__(msg or f"HTTP {status}")


def fetch(url: str, timeout: int = 30, data: str | None = None,
          headers: dict | None = None, impersonate: str | None = None) -> str:
    """Fetch url, return body text. GET by default; if `data` is given, POST it.
    curl.exe first (passes the Akamai/Cloudflare TLS fingerprinting that blocks
    Python's `requests` on guns.com/gunsamerica.com), falling back to requests.
    Raises FetchError with .status on HTTP errors.

    `impersonate` (e.g. "chrome124") routes the request through curl_cffi, which
    forges a full browser TLS+HTTP2 fingerprint. Some hosts (sportsmans.com moved
    behind a Cloudflare JS challenge in 2026) 403 the Windows Schannel curl but
    pass a genuine Chrome fingerprint. Falls back to the normal path if curl_cffi
    isn't installed or errors before returning a response."""
    hdrs = {"User-Agent": UA, **(headers or {})}
    status = None   # HTTP status once some path has produced one
    if impersonate:
        try:
            from curl_cffi import requests as _cffi
        except ImportError:
            _cffi = None
        if _cffi is not None:
            if data is not None:
                resp = _cffi.post(url, data=data, headers=headers or None,
                                  impersonate=impersonate, timeout=timeout)
            else:
                resp = _cffi.get(url, headers=headers or None,
                                 impersonate=impersonate, timeout=timeout)
            if resp.status_code >= 400:
                raise FetchError(resp.status_code)
            return resp.text
        # curl_cffi unavailable -> fall through to the curl/requests path
    if CURL:
        cmd = [CURL, "-s", "-L", "--compressed", "--max-time", str(timeout),
               "-A", UA, "-w", "\n%{http_code}"]
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
            body, _, code = proc.stdout.decode("utf-8", errors="replace").rpartition("\n")
            status = int(code or 0)
            if status < 400:
                return body
            # an HTTP error from curl is final for this path — do NOT re-run
            # the same request through requests, which looks even less like a
            # browser. Falls through to the 403 retry / raise below.
        else:
            status = None  # curl itself failed (network etc.) -> try requests

    if status is None:
        if data is not None:
            resp = requests.post(url, data=data, headers=hdrs, timeout=timeout)
        else:
            resp = requests.get(url, headers=hdrs, timeout=timeout)
        if resp.status_code < 400:
            return resp.text
        status = resp.status_code

    if status == 403 and not impersonate:
        # Last resort: retry once with a full forged Chrome TLS+HTTP2
        # fingerprint. Plain curl/requests are obvious non-browsers to
        # Cloudflare, which is far stricter from datacenter IPs than from a
        # home connection — so a fetch that works locally can 403 once hosted.
        # Only ever runs on a 403, so working sites are untouched.
        try:
            return fetch(url, timeout=timeout, data=data, headers=headers,
                         impersonate="chrome124")
        except Exception:
            pass  # impersonation didn't help either; report the original 403
    raise FetchError(status)
