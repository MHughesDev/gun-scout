# Multi-Vertical Search Engine — Design Spec

> Turning Gun Scout from a single gun-listing aggregator into **three sibling
> search engines** — **Guns**, **Parts**, and **Ammo** — that share one engine,
> one results UI, and one storage/lifecycle model, while each carries its own
> search inputs, result columns, relevance rules, and data clients.
>
> Scope of this spec: **data model, UI, and backend structure only.** Data
> providers / site clients per vertical are deliberately out of scope here
> (they slot into a registry the moment they exist).

---

## 1. Goal

Today the app is one vertical (guns) with everything hardcoded to it: the
search form, the result columns, the DB columns, the "guns only" filter, the
stats groupers. We want two more engines (Parts, Ammo) that *feel identical to
use* — left search panel, right results panel with sort + grid/list, a set of
online sources feeding them — but that ask for and display fundamentally
different fields.

The wrong way to do this is to fork `index.html` three times and add
`parts_listings` / `ammo_listings` tables with their own copy of the
lifecycle/price plumbing. That triples the surface area of every future change
(a sort-order fix, a watch-mode tweak, a new price-history feature) and guarantees
the three drift apart.

The right way — and what this spec proposes — is **one engine parameterized by a
"vertical" descriptor.** Everything that isn't intrinsically about *what kind of
product this is* stays shared; everything that is gets declared, once, per
vertical.

---

## 2. Core concept: the Vertical descriptor

A **Vertical** is a small, declarative object (Python on the backend, JSON
served to the frontend) that fully describes one search engine:

```
Vertical
├─ id            "guns" | "parts" | "ammo"
├─ label         "Guns", "Parts & Accessories", "Ammo"
├─ path          "/", "/parts", "/ammo"          (URL the shell is served at)
├─ inputs[]      the search-form field schema     → renders the LEFT panel
├─ columns[]     the result-column schema         → renders the RESULTS table/grid
├─ presets{}     dropdown values per input        (calibers, brands, categories…)
├─ relevance()   "does this listing belong in THIS vertical?"  (generalizes _is_firearm)
├─ enrich()      title→fields enrichment pipeline for this vertical
├─ passes()      vertical-specific filter predicate (the non-shared half of base.passes)
├─ stats{}       groupers + price basis + quality fields for the Stats page
└─ clients{}     REGISTRIES[id] — the site clients that feed this vertical
```

Both the backend request path and the frontend render path read from the same
descriptor, so the form, the filter logic, the stored columns, and the results
table can never fall out of sync — adding a field is one edit in one place.

This is the single most important idea in the spec. Sections 4–7 are just "what
each part of the current app looks like once it reads from a Vertical instead of
hardcoding guns."

---

## 3. Shared vs. per-vertical — the reuse map

Concrete inventory of the current code, tagged by what happens to it.

### Lifted as-is (zero or near-zero change)
| Current file / feature | Why it's already generic |
|---|---|
| `price_observations`, `search_results`, `client_status`, `health_log` tables | All key off `listings.id`; contain no gun concept. |
| `close_poller.py` (auction hammer prices) | Operates on `listing_type IN ('auction',…)` + `ends_at`; vertical-agnostic. |
| `search_manager.py` (thread-per-client, stream to DB) | Only needs to pick the *right registry* by vertical (one line). |
| `clients/base.py` `SiteClient`, `@register`, `health_check()` | Contract is `search(criteria, emit)`; unchanged. |
| Results UI machinery in `index.html`: table⇄grid toggle, sort select + dir, `#quickFilter`, watch mode, CSV export, lightbox, status pills, `#hscroll` sticky scrollbar, history tiles, right-click delete, `makeCombo()` combobox | None of it knows a column is "caliber"; it iterates a column list. |
| `/api/health`, `/api/clear`, `/api/search/<id>` polling + cursor model | Generic. |

### Refactored to be vertical-driven (same logic, reads a descriptor)
| Current | Becomes |
|---|---|
| `models.SearchCriteria` (gun fields inline) | Generic criteria: common fields + a `filters{}` dict validated per vertical (§5.1). |
| `models.Listing` (gun fields inline) | Core fields + `attributes{}` for vertical-specific data (§4, §5.1). |
| `base.passes()` (all gun filters inline) | `base.passes_common()` (condition/price/type/age/relevance) + `vertical.passes()` (§5.4). |
| `base._is_firearm()` / `hide_accessories` | `vertical.relevance(listing)`, applied automatically by vertical — no user toggle, no checkbox (§5.3). |
| `stats.py` (gun groupers, `kind='firearm'`) | `stats.py` parameterized by `vertical.stats` (§5.6). |
| `app.py` routes (`/api/options`, `/api/search`, `/api/stats`) | Vertical-scoped: `/api/<vertical>/…` (§8). |
| `static/index.html` (form + columns hardcoded) | One shared `search.html` + `search.js` shell driven by `/api/<vertical>/schema` (§7). Served at `/`, `/parts`, `/ammo`. |

### New, per vertical (the genuinely different parts)
- `verticals/guns.py`, `verticals/parts.py`, `verticals/ammo.py` descriptors.
- Enrichment: `clients/ammoparse.py` (grain, bullet type, count, cost/round),
  `clients/partsparse.py` (part category, fitment/platform). `clients/calibers.py`
  is **reused across all three** (guns, ammo, caliber-specific parts);
  `clients/brands.py` is reused and extended with parts/ammo makers.
- The site clients themselves (out of scope here) — registered under
  `REGISTRIES["parts"]` / `REGISTRIES["ammo"]`.

**Net:** the storage/lifecycle layer and the entire results interaction layer are
shared. Only the *schema of a product* and the *sources* differ per vertical —
which is exactly the axis along which the three engines actually differ.

---

## 4. Data model

### 4.1 Principle

Keep **one** `listings` table (so `price_observations` / `search_results` /
`close_poller` keep working untouched) with:

1. A `vertical` discriminator column.
2. The **universal** listing columns every vertical has.
3. **Promoted** typed columns for vertical-specific fields we want to *sort by,
   range-filter, or run stats on*.
4. An `attributes` JSON column for the long-tail vertical fields that only ever
   need to be displayed, not queried.

**Promotion rule:** a vertical field becomes a real indexed column **iff** you
need to sort/range/aggregate it in SQL; otherwise it lives in `attributes`. This
keeps `stats.py`'s typed-column SQL pattern intact per vertical without a table
per vertical.

> Alternative considered: three separate `*_listings` tables, or core +
> per-vertical extension tables joined 1:1. Cleaner typing, but it forks (or
> joins) the price/lifecycle plumbing that is currently the app's best-tested
> code. For a local single-user SQLite app, one wide sparse table + a discriminator
> is the lower-risk, higher-reuse choice. Revisit only if a vertical needs its own
> lifecycle semantics.

### 4.2 `listings` columns

**Universal (all verticals):**
```
id, vertical (NEW), site, url UNIQUE, title,
manufacturer, model,            -- brand + model/line/part-name; all 3 have these
condition,                      -- new | used | ''  (ammo adds 'reman'|'surplus')
kind,                           -- relevance class for THIS vertical (see §5.3)
listing_type, price, current_bid, bid_count, ends_at, final_price, close_checked_at,
posted_at, upc, is_bundle, image, extra,
first_seen_at, last_seen_at
caliber_canon                   -- REUSED by guns AND ammo AND caliber-specific parts
```

**Guns-promoted (already exist — keep):**
```
caliber, action, gun_type, barrel_length, capacity, capacity_rounds,
condition_grade, trade_in
```

**Ammo-promoted (new, nullable):**
```
grain            INTEGER   -- bullet weight
bullet_type      TEXT      -- FMJ|JHP|SP|HP|match|frangible|lead|shot-##…
round_count      INTEGER   -- rounds in the listing (box or bulk case)
price_per_round  REAL      -- DERIVED: price / round_count  (headline metric)
case_material    TEXT      -- brass|steel|aluminum|nickel
in_stock         INTEGER   -- ammo goes OOS constantly; first-class flag
```

**Parts-promoted (new, nullable):**
```
part_category    TEXT      -- optic|red-dot|magazine|trigger|barrel|bcg|handguard|
                           --   stock|muzzle-device|grip|sights|lower|upper|holster|
                           --   light|sling|bipod|… (primary axis, like gun_type)
fitment_canon    TEXT      -- normalized platform: AR-15|AR-10|Glock|1911|AK|Rem700|870…
mpn              TEXT      -- manufacturer part number (exact-match lookups)
```
(`caliber_canon` and `capacity_rounds` are reused for caliber-specific parts —
barrels, BCGs, magazines.)

**`attributes` JSON (display-only long tail), examples:**
- ammo: `muzzle_velocity`, `purpose` (target/defense/hunting/match), `reman`,
  `sold_as` ("single box" vs "case").
- parts: `subtype` (e.g. LPVO vs prism), `moa`, `color/finish`, `weight`,
  `thread_pitch`, `hand` (L/R).

### 4.3 What does NOT change
`price_observations`, `search_results`, `client_status`, `health_log`,
`searches` — except `searches` gains a `vertical` column so history/stats scope
correctly. The poll-cursor model (`search_results.id` as the incremental
cursor) is unchanged.

### 4.4 Migration
`db.init_db()` already does additive `ALTER TABLE ADD COLUMN` migrations
(models.py precedent). Add: `vertical` (default `'guns'` so existing rows stay
valid), the ammo/parts columns, and `attributes`. Add per-vertical indexes
(`idx_ammo ON listings(vertical, caliber_canon, price_per_round)`,
`idx_parts ON listings(vertical, part_category, fitment_canon)`). No data loss;
existing gun corpus keeps working.

---

## 5. Backend architecture

### 5.1 Generic criteria + listing

```python
@dataclass
class SearchCriteria:
    vertical: str = "guns"
    keyword: str = ""
    condition: str = "both"
    listing_type: str = "any"
    max_age_days: Optional[float] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    in_stock_only: bool = False     # ammo/parts
    # NOTE: no "only_relevant"/hide_accessories field. Relevance to the active
    # vertical is ALWAYS applied automatically (§5.3) — it's never a user choice.
    sites: list = field(default_factory=list)
    max_pages: Optional[int] = None # unchanged: None=exhaustive, health canary=1
    filters: dict = field(default_factory=dict)  # vertical-specific values
```

`filters` holds the vertical-specific inputs (`caliber`, `action`, `barrel_*`
for guns; `caliber`, `grain_*`, `bullet_type`, `cpr_max` for ammo;
`part_category`, `fitment`, `caliber` for parts). `from_dict` validates keys
against the vertical's declared `inputs`, coercing numeric ranges as today.

`Listing` keeps its universal fields as dataclass attrs and gains
`attributes: dict`. `__post_init__` calls `vertical.enrich(self)` (dispatched by
the client's vertical) instead of hardcoding `titleparse.enrich`.

### 5.2 Client registries per (vertical, site)

`REGISTRY` → `REGISTRIES: dict[str, dict[str, type[SiteClient]]]` keyed by
vertical then site. `@register` gains the vertical (either a decorator arg or a
`vertical` class attr on the client). The same physical site (gun.deals,
sportsmans, gunbroker sell guns *and* parts *and* ammo) gets a **separate client
per vertical** because the query strategy differs — a gun search and an ammo
search on the same site hit different category endpoints. `search_manager`
selects `REGISTRIES[criteria.vertical]`; everything else in it is unchanged.

### 5.3 Relevance — automatic, never a user toggle

The current `hide_accessories`→`_is_firearm` is a specific case of a general
question: **does this listing belong in the vertical the user is searching?**
Because the answer is fully determined by *which engine you're on*, there is no
reason to ask the user. Relevance is **always applied**, chosen by the vertical —
there is **no "guns only / parts only / ammo only" checkbox in any form.** A
listing that doesn't match the active vertical is simply never surfaced there.

Generalize to `vertical.relevance(listing) -> bool`, always run inside
`base.passes_common`:
- **Guns:** keep complete firearms + serialized frames/receivers (today's
  `titleparse.classify == 'firearm'`, hard-parts override included).
- **Parts:** the *inverse* — keep accessories/components, **drop** complete
  firearms and ammo. The existing hard-parts tier (stripped lowers, BCGs,
  barrels) flips from "exclude" to **include** here. `titleparse.classify` is
  reused, just read with the opposite polarity, plus an ammo exclusion.
- **Ammo:** a new lightweight classifier — keep loaded cartridges / shotshells;
  drop guns, components, reloading brass/primers/powder (unless we later add a
  Reloading sub-category).

`kind` on the row stores the vertical's relevance class so stats can filter on
it exactly like `kind='firearm'` does now. (Because relevance is enforced at
search time, the corpus for a vertical is already clean — the `kind` column is
what lets stats stay clean even if a mis-tagged row slips through.)

### 5.4 Filtering

`base.passes()` splits:
- `passes_common(criteria, listing)` — relevance, condition, listing_type, age,
  price range, in-stock. (Lifted verbatim from today's `passes`.)
- `vertical.passes(criteria, listing)` — the vertical's own predicates:
  - guns: caliber, action, barrel range, capacity range, manufacturer.
  - ammo: caliber, grain range, bullet_type, case_material, cost/round range,
    quantity range, manufacturer.
  - parts: part_category, fitment, caliber (if applicable), manufacturer,
    capacity (mags).

`clients/calibers.py` `_caliber_match` and `clients/brands.py` `matches` are
reused unchanged by all three.

### 5.5 Enrichment pipeline

Per vertical, `enrich(listing)` fills empty fields from the title (same "only
fill blanks; structured client data wins" rule as today):
- **guns:** existing `titleparse.enrich` (manufacturer/model/action/caliber/
  barrel/capacity/gun_type) — unchanged.
- **ammo:** new `ammoparse.enrich` — caliber (reuse `calibers.find_span`),
  grain (`\d{2,3}\s*(gr|grain)`), bullet_type vocabulary, round_count
  (`\d+\s*(rds?|rounds?|ct|count)` with bulk/case handling), then
  `price_per_round = price / round_count`, case_material, purpose.
- **parts:** new `partsparse.enrich` — part_category (keyword→category map),
  fitment/platform (normalize "fits Glock 19 Gen5" → `Glock`, "AR15/AR-15/M4" →
  `AR-15`), caliber where present, mpn.

`calibers.py` is the shared spine — reused by guns, ammo, and caliber-specific
parts, so the 168-alias table pays off three times.

### 5.6 Stats, per vertical

`stats.py` becomes parameterized by `vertical.stats`:
```
stats = {
  relevance_kind: 'firearm' | 'part' | 'ammo',   # the kind= filter
  price_basis:   firm-else-final  (guns/parts)  |  price_per_round (ammo),
  groupers:      { … },        # per-vertical group_by functions
  quality:       [ fields to report fill-rate on ],
  default_sort:  ('price'|'price_per_round', asc),
}
```
- **guns:** unchanged (caliber/manufacturer/model/action/gun_type/…; price basis
  firm-else-final; bundles excluded from price stats).
- **ammo:** group by caliber / bullet_type / manufacturer / grain / case; the
  headline metric is **cost-per-round** percentiles (min/median/p95), not sticker
  price. "Cheapest 9mm FMR right now, by brand" is the flagship query.
- **parts:** group by part_category / fitment / manufacturer; price percentiles
  per category.

Same `/api/<vertical>/stats` shape; the Stats page gets a vertical switcher.

### 5.7 Health checks

Unchanged mechanism. Each vertical's clients declare their own `canary_keyword`
(guns "glock", ammo "9mm", parts "magazine"). `run_health_checks` iterates
`REGISTRIES[vertical]`.

---

## 6. The three vertical schemas

### 6.1 GUNS (existing — restated as a descriptor)
- **Inputs:** keyword/model · condition · listing type · max age · action
  (combo) · manufacturer (combo) · caliber (combo) · barrel range · price range
  · capacity range · sites. (No "guns only" toggle — firearm relevance is
  always on, §5.3.)
- **Columns:** img · listing · mfr · model · caliber · action · barrel · cap ·
  cond · price · ends · listed · site.
- **Relevance:** complete firearms + serialized receivers. **Default sort:** as
  received. **Special:** auction bids vs firm price; "send to Ballistics" tie-in
  (future).

### 6.2 PARTS & Accessories
- **Inputs:**
  - keyword / part name
  - **part category** (combo/multiselect — optic · red dot · scope/LPVO ·
    magazine · trigger · barrel · BCG · handguard/rail · stock/brace · muzzle
    device · grip · sights · lower · upper · holster · light/laser · sling ·
    bipod · cleaning)
  - **fitment / platform** (combo — AR-15 · AR-10 · Glock · 1911 · AK · SIG P320
    · Rem 700 · Mossberg 500 · 870 · …)
  - caliber (combo, only bites for caliber-specific parts — barrels/BCGs/mags)
  - manufacturer/brand (combo)
  - condition · price range · in-stock · listing type · max age · sites
  - capacity range (magazines)
  - (No "parts only" toggle — complete guns & ammo are always excluded, §5.3.)
- **Columns:** img · listing · brand · **category** · **fits** · caliber ·
  cond · price · in-stock · listed · site. (mpn/subtype in a details expander.)
- **Relevance:** components/accessories; drop complete guns & ammo (§5.3
  inverse). **Default sort:** as received. **Special:** fitment shown as a chip;
  category is the primary grouping axis.

### 6.3 AMMO
- **Inputs:**
  - keyword
  - **caliber / cartridge** (combo — primary axis, reuse `calibers`)
  - **grain weight** range
  - **bullet type** (combo/multiselect — FMJ · JHP · SP · HP · match ·
    frangible · lead · birdshot/buckshot/slug)
  - purpose (select — target/range · self-defense · hunting · match)
  - manufacturer / line (combo)
  - case material (select — brass · steel · aluminum · nickel)
  - **quantity** range (single box … bulk 1000-ct case)
  - price range **and cost-per-round max**
  - condition (new · reman · surplus) · in-stock · max age · sites
  - (No "ammo only" toggle — guns, components & reloading supplies are always
    excluded, §5.3.)
- **Columns:** img · listing · brand/line · caliber · grain · bullet type ·
  **rounds** · price · **¢/round** (headline, default sort asc) · case ·
  in-stock · listed · site.
- **Relevance:** loaded cartridges/shotshells; drop guns, components, reloading
  supplies. **Default sort:** cost-per-round ascending. **Special:**
  price-per-round is the whole game — computed at enrichment, promoted to a
  column, default sort, and the stats headline metric.

---

## 7. Frontend architecture

### 7.1 One shell, three configs

Replace the plan of "three copies of index.html" with **one shared search
shell** — `static/search.html` + `static/search.js` + `static/search.css` —
served at `/` (guns), `/parts`, and `/ammo`. On load it reads the vertical from
its path, fetches `/api/<vertical>/schema`, and builds the page from it. The
current `index.html` becomes the guns instance of this shell; the ~640 lines of
results JS (poll, renderRows, sort, grid/table, watch, CSV, lightbox, history,
combobox) move in **almost verbatim** — they already iterate generic data.

### 7.2 Schema-driven rendering

The shell contains **no hardcoded fields or columns.** Two renderers consume the
descriptor:

**Form renderer** (builds `#filters` from `inputs[]`):
```
InputField = { id, label, type, presetsKey?, placeholder?, default?, min/max ids }
  type ∈ text | select | combo | range | checkbox
```
`range` renders the existing two-combo min/max pair; `combo` reuses `makeCombo()`
verbatim with `presets[presetsKey]`. `criteria()` is generated from the schema
(loop `inputs`, read values into `filters{}`) instead of the hardcoded object.

**Column renderer** (builds table `<th>`s, grid cards, sort options from
`columns[]`):
```
Column = { key, label, sortable, render, grid? }
  render ∈ text | price | pricePerRound | image | date | ends | badge | chip | stock
  grid   ∈ title | subtitle | meta | badge   (role in a card)
```
`rowHtml`/`cardHtml` become generic loops over `columns`, dispatching on
`render` type (the special formatters — `priceHtml`, `endsIn`, `listedAgo`,
lightbox image, and a new `pricePerRound`/`stock` — are the only per-render-type
code, shared across verticals). Sort select is populated from
`columns.filter(sortable)`. **Grid/list toggle, sort, quick-filter, watch, CSV,
lightbox all stay exactly as they are** — they never referenced a specific
column name.

### 7.3 Navigation

Header nav becomes the three engines plus the cross-cutting pages:

```
Guns | Parts | Ammo | Stats | Ballistics
```

`Guns/Parts/Ammo` are the same shell at different paths. `Stats` is the existing
stats page with a vertical switcher (Guns/Parts/Ammo) driving `/api/<v>/stats`.
`Ballistics` stays as-is (guns/ammo-adjacent; a later "compare this ammo load"
hook is a natural tie-in, out of scope now). The current three-page split
(`/`, `/stats`, `/ballistics`) already duplicates the header per page — the
shared shell fixes that for the three search engines.

### 7.4 Per-vertical result affordances
- **Ammo:** `¢/round` column with default-sort-ascending; a subtle secondary
  line showing `$X for N rds`; in-stock badge.
- **Parts:** `fits <platform>` rendered as a chip; category as a labeled tag.
- **Guns:** unchanged (auction bid vs price, NEW badge, ends-in countdown).

History, watch mode, CSV, health panel, "clear all data", right-click-delete —
all shared, scoped to the active vertical via the `vertical` column on
`searches`.

---

## 8. API surface

Vertical-scoped, mirroring the current routes:

```
GET  /                      guns shell     (search.html)
GET  /parts                 parts shell
GET  /ammo                  ammo shell
GET  /stats                 stats shell (vertical switcher)
GET  /ballistics            unchanged

GET  /api/<vertical>/schema     inputs[] + columns[] + presets{} + defaults   (NEW; supersedes /api/options)
GET  /api/<vertical>/clients    sites registered for this vertical
POST /api/<vertical>/search     start a search (body = criteria incl. filters{})
GET  /api/search/<id>           poll (unchanged; row carries its vertical)
GET  /api/searches?vertical=…   history, filterable by vertical
GET  /api/<vertical>/stats      aggregates for this vertical
POST /api/health                unchanged (iterates all verticals' registries)
POST /api/clear                 unchanged
DELETE /api/search/<id>         unchanged
```

`SearchCriteria.vertical` is also carried in the body, so `/api/<v>/search` is
really sugar over one handler that reads the path segment.

---

## 9. Build plan (guns never breaks)

1. **Schema plumbing.** Add `vertical` to `searches` + `listings`, add
   ammo/parts columns + `attributes`, additive migration (default `'guns'`).
   Ship — guns app unaffected.
2. **Extract the Vertical abstraction.** Create `verticals/base.py` +
   `verticals/guns.py` describing today's guns engine. Route the existing app
   through it (criteria `filters{}`, `vertical.passes`, `vertical.relevance`,
   `vertical.enrich`, `stats` config). No behavior change — this is the
   refactor that proves the abstraction against the working vertical.
3. **Shared frontend shell.** Turn `index.html` into `search.html`/`search.js`
   driven by `/api/guns/schema`. Verify guns is byte-for-byte equivalent in
   behavior.
4. **Add Ammo.** `verticals/ammo.py` + `ammoparse.py` + ammo columns/schema +
   `REGISTRIES["ammo"]` (clients later). Cost-per-round end to end.
5. **Add Parts.** `verticals/parts.py` + `partsparse.py` + relevance inverse +
   fitment normalization + `REGISTRIES["parts"]`.
6. **Stats + nav.** Vertical switcher on Stats; three-engine header nav.

Steps 1–3 are pure refactor with the guns corpus as the regression test; 4–5 are
additive; the site clients for parts/ammo are the only genuinely new external
work and are explicitly deferred.

---

## 10. Decisions worth confirming before implementation

1. **One wide `listings` table** (recommended, §4.1) vs. per-vertical tables /
   extension tables. Recommendation stands unless a vertical needs distinct
   lifecycle semantics.
2. **Single shared shell** served at `/`, `/parts`, `/ammo` (recommended, §7.1)
   vs. keeping three static pages. Recommendation: consolidate — it's the
   biggest reuse win and kills the current header duplication.
3. **Ammo cost-per-round as default sort + stats headline** (recommended) — con­firm
   this is the metric you want front-and-center.
4. **Parts fitment normalization depth** — how granular ("Glock" vs "Glock 19
   Gen5")? Recommend normalize to *platform family* for filtering, keep the raw
   fitment string in `attributes` for display.
5. **Parts taxonomy** — the `part_category` enum in §6.2 is a first draft; worth
   a pass to match how you actually shop for parts.
```
