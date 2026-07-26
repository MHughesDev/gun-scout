# Gun Scout

Local web app that searches gun marketplaces from one place. Set filters, hit
**Search**, and watch the table populate live as each site client reports in.
Everything runs on your machine; results are stored in SQLite (`gun_scout.db`).

## Run it

```
run.bat
```

(or `pip install -r requirements.txt && python app.py`) then open
http://127.0.0.1:8777.

## Filters

Keyword/model, new/used/both, listing type (fixed price vs auction), action
type, manufacturer, caliber, barrel length range, price range, capacity range,
an accessory filter (hides magazines/holsters/parts, on by default), and which
sites to hit. For auctions the price range applies to the current bid.

## Data model (built for price analytics)

- **One row per unique listing URL** (`listings`), with lifecycle tracking:
  `first_seen_at` / `last_seen_at` give days-on-market; a listing that stops
  being re-seen probably sold.
- **`price_observations`** is a per-listing time series; a row is added only
  when the observed price/bid actually changed, so watch-mode re-runs don't
  bloat it. `search_results` records which search surfaced which listing.
- **Firm prices and bids never mix**: `price` is only ever an asking/buy-now
  price; a live auction's current bid sits in `current_bid` (it's a lower
  bound, not a price). When an auction ends, a background poller re-fetches
  the item and records the **final hammer price** (`final_price`) — the only
  true market-clearing price in the data.
- Segmentation columns for future stats: `condition_grade`
  (new/excellent/very-good/good/…), `trade_in` (LE trade-ins are their own
  price tier), `is_bundle` (gun + optic/mags packages skew price), `upc`.

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
  `blocked`, or `error`. Results persist to the `health_log` table, and the
  last known state is shown on page load.
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

- A **NEW** badge marks listings never seen in any previous search
  (suppressed on your very first search).
- Search history, listings, price history, and health checks live in
  `gun_scout.db`.
- Auction search currently needs the GunBroker API key (GunsAmerica's
  server-rendered results only carry fixed-price dealer listings).
- Clients sleep ~0.8 s between page fetches; keep it polite.
