"""Cabela's token relay — run this on YOUR machine to keep the hosted app's
Cabela's search alive.

Why this exists: Cabela's Coveo search works fine from anywhere (including a
cloud host), but the token that authorizes it can only be minted by a real
browser sitting on a cabelas.com page — Akamai 403s server-side requests, and
the endpoint sends no CORS headers, so a visitor's browser can't mint it from
our origin either. Your machine can (headed browser + residential IP), so it
mints and donates the token to the deployment. Everyone using the public site
then shares it.

Usage:
    python push_token.py                       # mint if stale, push if needed
    python push_token.py --force               # mint a brand new one and push

Config (env vars, or edit the defaults below):
    GS_PUBLIC_URL       the deployment, e.g. https://gun-scout.onrender.com
    GS_TOKEN_PUSH_KEY   must match the same var set on the deployment

Keep it fresh automatically — tokens last ~4 h, so refresh every 3:
    Windows:  schtasks /create /tn "GunScout token" /sc hourly /mo 3 ^
                /tr "\"C:\\Path\\to\\python.exe\" \"C:\\Path\\to\\push_token.py\""
    cron:     0 */3 * * *  cd /path/to/gun_scout && python push_token.py
"""
import argparse
import json
import os
import sys

import requests

import cabelas_token
import statstore

DEFAULT_URL = "https://gun-scout.onrender.com"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="mint a new token even if the cached one is fine")
    ap.add_argument("--url", default=os.environ.get("GS_PUBLIC_URL", DEFAULT_URL),
                    help="deployment base URL")
    args = ap.parse_args()

    statstore.start()   # the local token cache lives in the app DB

    # Skip the ~1-minute browser mint when the deployment is already covered.
    if not args.force:
        try:
            remote = requests.get(f"{args.url}/api/cabelas/token", timeout=20).json()
            if not remote.get("needs_refresh"):
                print(f"deployment token still good for "
                      f"{remote.get('expires_in_s', 0) // 60} min — nothing to do")
                return 0
        except requests.RequestException as e:
            print(f"could not reach {args.url}: {e}", file=sys.stderr)
            return 1

    try:
        token = cabelas_token.get_valid_token(force=args.force)
    except cabelas_token.MintError as e:
        print(f"MINT FAILED: {e}", file=sys.stderr)
        return 1

    headers = {"Content-Type": "application/json"}
    key = os.environ.get("GS_TOKEN_PUSH_KEY", "")
    if key:
        headers["X-Push-Key"] = key
    try:
        r = requests.post(f"{args.url}/api/cabelas/token", headers=headers,
                          data=json.dumps({"token": token}), timeout=40)
    except requests.RequestException as e:
        print(f"push failed: {e}", file=sys.stderr)
        return 1

    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code == 200 and body.get("accepted"):
        print(f"pushed OK — shared token now valid for "
              f"{body.get('expires_in_s', 0) // 60} min")
        statstore.ENGINE.flush()
        return 0
    print(f"REJECTED (HTTP {r.status_code}): {body.get('message') or body.get('error') or r.text[:200]}",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
