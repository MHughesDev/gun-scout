# Data Providers Plan — Ammo & Parts

> Where the Ammo and Parts engines get their data, and exactly how we acquire it.
> Grounded in live probing (July 2026) and mapped onto the same acquisition
> techniques the 8 gun clients already use. See `search_engines_spec.md` for the
> engine architecture and `clients/` for the proven techniques referenced below.

## The one big finding

**The retail gun-industry runs on a handful of hosted commerce/search stacks we
already know how to read.** Probing the top ammo and parts sellers, the same
three surfaces from the guns work reappear everywhere:

| Surface | Guns client that proved it | Who else uses it (probed) |
|---|---|---|
| **Algolia** (public search index, creds in page, `*.algolia.net` is NOT Cloudflare-gated) | `gunscom.py` | ammo.com, Lucky Gunner, BulkAmmo, Academy, **GunMagWarehouse** — all Magento+Algolia |
| **Magento + Amasty** (catalogsearch redirect, category walk, product-page JSON price) | `psa.py` | Primary Arms, Target Sports, SGAmmo, Brownells storefront |
| **Coveo / hosted search behind a token** | `cabelas.py` | Cabela's/Bass Pro (ammo & parts share the catalog) |
| **Aggregator with structured feed** | `gunmade.py` (SvelteKit `__data.json`) | AmmoSeek, AmmoBase, gun.deals, WikiArms (XML) |

**Proven live this session:** ammo.com's Algolia index `prod_ammonet_products`
answers a plain POST from Python (no Cloudflare, no token) with 640 hits for
"9mm", each carrying structured `caliber`, `jacket_type` (→ bullet type),
`in_stock`, `price {USD}`, `image_url`, and a title we already parse for
grain/round-count ("Winchester 9mm Ammo - 50 Rounds of 147 Grain FMJ"). Price ÷
round-count = the cost-per-round the Ammo engine sorts on. The price-sorted
replica `prod_ammonet_products_price_default_asc` exists, so we can price-cursor
walk past Algolia's 1,000-hit window exactly like `gunscom.py` band-walks.

**Consequence:** most of these are *small* clients — a shared
`clients/algolia.py` helper (scrape creds → paginate/price-walk → map fields)
turns each Algolia site into ~40 lines of per-site config. One technique, many
providers.

---

## Acquisition techniques (reused from guns)

1. **Direct Algolia** — GET the site homepage/category once, scrape
   `algoliaConfig` (`applicationId`, search `apiKey`, index prefix) from the
   inline JS, then POST `https://{appId}-dsn.algolia.net/1/indexes/{prefix}_products/query`.
   Facet filters on structured fields (`caliber`, `jacket_type`, `in_stock`),
   numeric filters on price. Exhaustive via the `_price_default_asc` replica +
   price cursor. **No Cloudflare on algolia.net.** (Mirrors `gunscom.py`.)
2. **Magento/Amasty scrape** — `/catalogsearch/result/?q=` (follow the Amasty
   redirect, paginate the canonical), or walk category trees; prices from card
   `data-price-amount` or the product page's analytics JSON. (Mirrors `psa.py`.)
   Many of these *also* expose Algolia — prefer Algolia when present.
3. **Coveo token** — mint via the existing `cabelas_minter`; ammo/parts are the
   same catalog with `isgun=0` / different `class_name`. (Mirrors `cabelas.py`.)
4. **Extend an existing guns client to a new vertical** — same host, different
   category endpoint, a second registered client under the new vertical.
   (gun.deals, Sportsman's, GunMade, GunBroker, GunsAmerica, PSA, Cabela's all
   sell all three.)
5. **Aggregator feed** — gun.deals category/ammo tiles; WikiArms nightly XML
   offer feed (vendor program, paid); AmmoSeek/AmmoBase (Cloudflare-gated, JSON
   behind challenge — lowest priority, direct retailers already give us breadth).

---

## AMMO — top 10 providers

Ranked by value (breadth × data quality × ease of acquisition).

| # | Provider | Platform / surface | How we acquire | Priority |
|---|---|---|---|---|
| 1 | **ammo.com** | Magento **Algolia** (`prod_ammonet`) | Direct Algolia — **proven live**, structured caliber/jacket/in_stock/price | **P0 (build first)** |
| 2 | **Lucky Gunner** | Magento **Algolia** | Direct Algolia (real-time in-stock is their selling point) | P0 |
| 3 | **BulkAmmo** | Magento **Algolia** | Direct Algolia | P1 |
| 4 | **Academy Sports** | **Algolia** | Direct Algolia (mainstream pricing, huge catalog) | P1 |
| 5 | **Palmetto State Armory** | Magento/Amasty — *client exists* | Extend `psa.py` → ammo categories (`/ammo/...`); used/surplus too | **P0 (reuse)** |
| 6 | **gun.deals (ammo)** | Drupal/Solr — *client exists* | Extend `gundeals.py` → `/category/ammo` + `result_type=deal` | **P0 (reuse)** |
| 7 | **SGAmmo** | Magento (Cloudflare on storefront) | Amasty scrape via curl-TLS; cheapest-bulk reputation | P2 |
| 8 | **Target Sports USA** | ASP.NET custom (100M+ rounds) | HTML category scrape (per-round price shown natively) | P2 |
| 9 | **Cabela's / Bass Pro** | Coveo — *client exists* | Extend `cabelas.py` → `isgun=0` ammo class via existing token | P1 (reuse) |
| 10 | **GunMade / GunsAmerica (ammo)** | aggregator / API — *clients exist* | Extend `gunmade.py` (category=Ammo) + GunsAmerica family 19 | P2 (reuse) |

Aggregator alternates (breadth, but gated): **AmmoSeek** (Cloudflare + Magento),
**AmmoBase** (500+ retailers), **WikiArms** (paid XML feed). Hold unless the
direct-retailer set leaves gaps.

## PARTS & ACCESSORIES — top 10 providers

| # | Provider | Platform / surface | How we acquire | Priority |
|---|---|---|---|---|
| 1 | **GunMagWarehouse** | Magento **Algolia** (appId `0ZJA7NXACC`) | Direct Algolia — **creds confirmed** in page; magazines/parts | **P0 (build first)** |
| 2 | **Brownells** | Magento + **HawkSearch/Bloomreach** | HawkSearch JSON API (largest parts catalog in the world) | **P0** |
| 3 | **Primary Arms** | Magento (accessible to curl) | Amasty scrape (`psa.py` technique); optics + AR parts | P1 |
| 4 | **Palmetto State Armory** | Magento — *client exists* | Extend `psa.py` → parts categories (uppers/lowers/optics/mags) | **P0 (reuse)** |
| 5 | **MidwayUSA** | Custom (Cloudflare 403 to curl) | Sitemap + product JSON, or curl-TLS; deep parts catalog | P2 |
| 6 | **OpticsPlanet** | Custom/JS (blocks curl) | Headless or hosted-search endpoint; optics breadth | P2 |
| 7 | **gun.deals (parts)** | Drupal/Solr — *client exists* | Extend `gundeals.py` → `/category/optics,magazines,parts` | **P0 (reuse)** |
| 8 | **Cabela's / Bass Pro** | Coveo — *client exists* | Extend `cabelas.py` → parts classes via existing token | P1 (reuse) |
| 9 | **Aero Precision / Rainier Arms** | Magento/BigCommerce | Per-platform scrape; maker-direct AR parts | P2 |
| 10 | **Sportsman's / GunMade (parts)** | Hybris / aggregator — *clients exist* | Extend `sportsmans.py` + `gunmade.py` category scoping | P2 (reuse) |

Marketplaces (private-party parts): **Armslist** already flows through
`gunmade.py`; **eBay** has a real API and large parts inventory (a clean P2 add).

---

## Build order (what I'm implementing now)

**Phase A — prove both new engines with real data (this session):**
- `clients/algolia.py` — shared Magento-Algolia helper (config scrape +
  paginated + price-cursor exhaustive walk + generic hit→Listing mapping).
- `clients/ammo_com.py` — Ammo P0, on the helper (proven index/fields).
- `clients/gunmagwarehouse.py` — Parts P0, on the helper (creds confirmed).

**Phase B — harvest the reuse (fast, proven hosts):**
- Extend `psa.py`, `gundeals.py`, `cabelas.py` to register ammo + parts variants
  (a second client class per vertical; the fetch/parse plumbing already works).

**Phase C — remaining Algolia retailers:** Lucky Gunner, BulkAmmo, Academy
(same helper, per-site config) → the Ammo engine has real breadth.

**Phase D — Brownells (HawkSearch) + Primary Arms (Amasty)** → Parts breadth.

**Phase E — gated/aggregator sources** as needed: SGAmmo, Target Sports,
MidwayUSA, OpticsPlanet, AmmoSeek, eBay parts.

Each client stays small because the acquisition technique is shared, the
enrichment (`ammoparse.py` / `partsparse.py`) is shared, and `base.passes` +
the vertical descriptors already handle filtering, relevance, storage, stats,
and UI. Adding a provider is: pick the technique, map the fields, `@register`.
