# Gun Scout

Local web app that searches gun marketplaces from one place. Set filters, hit
**Search**, and watch the table populate live as each site client reports in.
Everything runs on your machine. Search results live in process memory only;
the sole thing persisted (`gun_scout.db`) is the anonymous market-stats fact
store.

## Run it

```
run.bat
```

(or `pip install -r requirements.txt && python app.py`) then open
http://127.0.0.1:8777.

## Host it publicly (free tier friendly)

The app is built to be shared: state is either global-by-design (stats, site
tokens) or per-visitor and transient (searches).

### Render (the configured path)

`render.yaml` is a ready Blueprint. One-time setup: push to GitHub, then in
the Render dashboard **New → Blueprint**, pick this repo, Apply. From then on
**every push to the default branch auto-deploys**. Optionally front it with
Cloudflare (free) for your domain/TLS/caching. Free-plan trade-offs: the app
sleeps after 15 min idle (first visit takes ~30 s to wake) and has no
persistent disk, so stats reset on each deploy — the upgrade path to
always-on + durable stats (~$7/mo) is commented in `render.yaml`.

### Any other host

To deploy anywhere else (Railway / Fly / a VPS):

- Start command: `python app.py`. When the host injects a `PORT` env var the
  app binds `0.0.0.0:$PORT` and serves through waitress automatically.
  **Run one process/worker** — the stats engine lives in process memory.
- Point `GUN_SCOUT_DB` at a persistent volume (e.g. `/data/gun_scout.db`) so
  the stats fact store survives redeploys; it's the only file that matters.
- Keys are server-side and shared by every visitor: set `GUNBROKER_DEV_KEY`
  for GunBroker, and `CABELAS_COVEO_TOKEN` for Cabela's (the browser-based
  token minter can't run on a headless host, so paste a token minted locally;
  it lasts ~4 h — re-set it or run the minter on a box that can reach the
  deploy's `cabelas_token.json` volume).
- Search history never reaches the server (it lives in each visitor's browser
  sessionStorage, inputs only), and results evaporate from RAM ~15 minutes
  after a search finishes — there's nothing user-identifying to store or leak.

## Filters

Keyword/model, new/used/both, listing type (fixed price vs auction), action
type, manufacturer, caliber, barrel length range, price range, capacity range,
an accessory filter (hides magazines/holsters/parts, on by default), and which
sites to hit. For auctions the price range applies to the current bid.

## Data model (streaming stats, no stored listings)

- **Searches, results, client status and health are RAM-only** (`store.py`).
  The UI polls results in while a search runs; finished searches evaporate
  after ~15 minutes. Nothing about what you searched for is ever written to
  disk server-side — the "recent searches" list is your browser's
  sessionStorage, holds input values only, and just prefills the form.
- **The only persistent data is the stats fact store** (`statstore.py`).
  Every listing a search observes is reduced to a compact anonymous *fact* —
  just the dimension values stats group and filter by (brand, model, caliber,
  action, gun type, condition, grain, bullet type, part category, fitment,
  site, …) plus one price basis per engine (firm price for guns/parts,
  cost-per-round for ammo). No URLs, titles, images or price history are
  stored; facts are keyed by a 64-bit URL hash purely so re-observations
  update in place instead of double counting.
- **Stats update live, as searches happen**: `observe()` is an O(1) in-memory
  upsert on the search threads' hot path; every mutation bumps a version
  counter; the stats API computes from the RAM fact set and memoizes per
  (query, version); a write-behind flusher batches dirty facts to SQLite
  every ~2 s. The stats page polls and re-renders only when the version moves.
- **Firm prices and bids never mix**: a live auction's current bid is a lower
  bound, not a price, so it never enters price stats. When a GunBroker
  auction ends, a background poller re-fetches the item and folds the **final
  hammer price** — the only true market-clearing price — into its fact.
- Price-stat hygiene flags ride on each fact: `trade_in` (LE trade-ins are
  their own price tier) and `is_bundle` (gun + optic/mags packages skew
  price, excluded from price stats by default).

## Site clients

| Site | Status | How |
|------|--------|-----|
| Guns.com | Working | Search pages embed the full result JSON (specs, price, condition) in a `:initial-data` attribute; fetched via system `curl` because their bot protection 403s Python's TLS stack. |
| GunsAmerica | Working (limited) | Angular SSR embeds results in an `ng-state` JSON blob. Only single-word keywords get server-rendered and pagination is client-side, so we search the strongest token (~1 page ≈ 12 result groups + duplicate seller offers) and filter the rest locally. |
| Sportsman's Warehouse | Working | Classic server-rendered HTML; product cards carry `data-product-name` / `data-brand` attributes. New guns only. |
| gun.deals | Working | Deal aggregator (dozens of retailers) — search tiles carry schema.org microdata (`itemprop="name"` / `itemprop="price"`). New retail deals only; great for the cheapest current price. |
| GunBroker.com | Needs a free API key | Site is behind Cloudflare, but they offer an official API. Get a dev key (gunbroker.com → Help → API), then set `GUNBROKER_DEV_KEY` or paste the key into `gunbroker_key.txt` next to `app.py`. |

## Structure-change detection

Scrapers rot when sites redesign. Gun Scout tells you *when and where*:

- Every client raises a dedicated `StructureError` (instead of silently
  returning nothing) when a page fetches fine but its expected markup/JSON is
  gone — e.g. guns.com dropping the `:initial-data` attribute, the `firearms`
  key vanishing, or listing objects losing fields. The error message says
  exactly what went missing and which file to update.
- During a search, that surfaces as a purple **STRUCTURE CHANGED** pill on the
  affected site (other sites keep searching normally) and a warning line with
  the details, plus a `WARNING` in the server log.
- The **Check site health** button runs a canary search ("glock") against
  every site in parallel and reports per site: `ok` (with hit count and
  latency), `degraded` (parses but 0 hits or mostly-missing prices/titles —
  the drift wasn't loud enough to crash the parser), `schema_changed`,
  `blocked`, or `error`. The latest result per site is kept in memory and
  shown on page load (for the current server session).
- API: `POST /api/health` runs the checks, `GET /api/health` returns the last
  result per site.

### Adding a site

Create `clients/yoursite.py`, subclass `SiteClient`, implement
`search(criteria, emit)` calling `emit(Listing(...))` per match, decorate the
class with `@register`, and import the module in `clients/__init__.py`.
The UI and health system pick it up automatically. Conventions:

- Use `self.passes(criteria, listing)` for the standard filter checks.
- Raise `ClientBlocked("why")` if the site refuses you (403/captcha/key).
- Raise `StructureError("what exactly went missing")` when the page loads but
  your selectors/JSON keys no longer match — that's what powers the
  structure-change alerts. Only raise it on the first page so "no more
  results" isn't mistaken for breakage.
- `clients/titleparse.py` has shared helpers to pull caliber/barrel/capacity
  out of listing titles, plus the accessory detector.

## Notes

- A **NEW** badge marks listings the stats engine has never seen before
  (suppressed on a fresh install's first search).
- Only the market-stats facts persist (in `gun_scout.db`); searches, results
  and health checks live in process memory. A pre-existing multi-table
  `gun_scout.db` is migrated automatically on first start: every stored
  listing becomes a stat fact, then the legacy tables are dropped.
- Auction search currently needs the GunBroker API key (GunsAmerica's
  server-rendered results only carry fixed-price dealer listings).
- Clients sleep ~0.8 s between page fetches; keep it polite.
