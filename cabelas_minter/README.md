# Cabela's Coveo token minter

Cabela's search runs on Coveo, which needs a short-lived (~4 h) bearer token.
That token can only be obtained by a **real browser** (the site is behind
Akamai Bot Manager, which blocks server-side/headless requests). This tiny
Node/Playwright tool drives a headed Microsoft Edge session to capture the
anonymous token the site's own frontend requests, and prints it as JSON.

`../cabelas_token.py` calls this automatically and caches the result — you
normally never run it by hand. It exists as a separate Node tool only because
the browser automation lives in the Playwright/Node ecosystem.

## One-time setup

Requires Node.js (https://nodejs.org) and Microsoft Edge (preinstalled on
Windows).

```
cd cabelas_minter
npm install
npx playwright install msedge     # or: npx playwright install chromium
```

Then verify the whole pipeline (mints a token and queries Coveo with it):

```
cd ..
python cabelas_token.py           # prints "verified": true on success
```

## How it's used

- The Cabela's client calls `cabelas_token.get_valid_token()`, which returns a
  cached token or re-mints via `mint.js` when it's missing/expired.
- Manual override (no browser/Node needed): set `CABELAS_COVEO_TOKEN` to a
  token you copied from browser devtools (Network → `getCoveoToken` → `token`).

## Notes / caveats

- **Headed only.** Akamai detects headless browsers, so `mint.js` uses a
  visible browser window (it needs an interactive desktop session — it won't
  work on a headless server without a virtual display). It requires no
  interaction; the window opens and closes on its own in ~60 s.
- The `profile/` directory holds a persistent browser profile (warms the
  Akamai trust so re-mints stay reliable). It and `node_modules/` are
  git-ignored.
- `CABELAS_MINT_HEADLESS=1` will try headless first and fall back to headed;
  headless currently does not pass Akamai, so it's off by default.
