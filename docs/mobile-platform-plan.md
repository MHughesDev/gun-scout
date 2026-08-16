# Gun Scout — mobile platform plan

How Gun Scout gets from "a Flask app on Render with a web UI" to "an iOS and
Android app in both stores, on a backend that can be moved to Cloud Run when
it needs to scale," and in what order, and what could stop it.

Written as a working document: phases have exit criteria, and the decisions
that are already made say so, so this can be argued with rather than merely
read.

---

## 1. Where we're starting from

One Flask process (`app.py`) serves a static web UI and a JSON API. Three
search engines — guns, parts, ammo — are described by `verticals/`, and 12+
site clients under `clients/` scrape or query one retailer each. Two facts
about that process shape everything below:

- **All state is in memory.** In-flight searches and their result rows
  (`store.py`), the remote-job queue (`remote.py`), and the entire market-stats
  fact set (`statstore.py`) live in the process. SQLite is a write-behind
  *mirror* of the fact set, not the source of truth. This is why the README
  says "run one process/worker," and it is the single constraint that governs
  the whole scaling section.
- **Some retailers refuse datacenter IPs.** Four sources 403 anything hosted,
  so `worker.py` runs on the operator's home connection, claims jobs, and posts
  listings back — with `guard.py` treating the server as the untrusted end. No
  cloud platform changes this. Moving to Cloud Run does not recover those
  sources; GCP's ranges are judged at least as harshly as Render's.

The web UI's JSON endpoints are not a public contract: they ship with the
frontend and can change together. A mobile binary can't. That asymmetry is why
this change adds `/api/v1` rather than pointing the app at the existing routes.

---

## 2. The constraint that shapes the product

**Both app stores restrict apps in this category, and the restriction is
aimed squarely at what Gun Scout does.** This is not a formality to be handled
at submission time — it decides what the app is, so it comes first.

**Apple**, App Review Guideline 1.1.3, lists as objectionable content:

> "Depictions that encourage illegal or reckless use of weapons and dangerous
> objects, or facilitate the purchase of firearms or ammunition."

**Google Play**'s Inappropriate Content policy ("Dangerous products") does not
allow apps that facilitate the sale of explosives, firearms, ammunition, or
certain firearm accessories — the named accessories being those that simulate
or enable automatic fire, and magazines over 30 rounds.

The precedent is direct: GunBroker's app was removed from both stores in 2018
under exactly these policies.

### 2.1 What that means for us

Gun Scout sells nothing, takes no payment, and hosts no listings. It reads
public listings and reduces them to market statistics. That is a materially
different thing from a marketplace app — but the operative verb in both
policies is *facilitate*, and a tap that takes the user to a retailer's
purchase page is the part a reviewer can point at.

So the product decision is:

> **Gun Scout ships as a firearms market *research* tool.** Its centre of
> gravity is what things cost — medians, price ranges, condition tiers,
> cost-per-round, and how a given listing compares — not where to buy them.
> Whether a result links out to the retailer is a **server-side switch**, off
> by default.

This is already implemented, in `api_v1.py`: with `GS_MOBILE_LISTING_LINKS`
off, listing URLs are stripped **server-side**, so the app cannot open a
purchase page even if a build tried. The switch lives on the server precisely
because the answer may differ per platform and may change after a review
conversation — and a server flag moves in seconds, where an app update takes
days.

Everything else the app shows — title, price, condition, caliber, site name,
market comparison — is unaffected. Withholding the link removes the
transaction path, not the information.

### 2.2 Submission posture

| Item | Position |
|---|---|
| Category | Reference / Utility — not Shopping |
| Age rating | 17+ (Apple) / Mature 17+ (Play), declared honestly |
| Store listing copy | "Compare firearm, ammunition and parts prices across retailers." No "buy", "shop", "order", "deals" |
| Screenshots | Lead with the Market tab, not a results list |
| In-app disclosure | About tab (shipped): no transactions, no affiliation, prices are informational, purchases are subject to law and dealer transfer |
| Data safety / privacy label | "No data collected" — true, and worth protecting: no account, no analytics SDK, no permissions, history device-local |
| Reviewer notes | State plainly what the app is, that no transaction occurs in it, and that listing links are disabled |

### 2.3 If it's rejected anyway

Rejection is a plausible outcome, not a remote one. Ordered fallbacks, all
reachable without re-architecting:

1. **Links already off** — if the rejection cites facilitation despite that,
   ask which surface, and remove it (site names, images, per-listing rows).
2. **Statistics-only build.** The Market tab plus the ballistics calculator
   (already in `static/ballistics_engine.js`, a well-precedented app category)
   is a complete, defensible product on its own. The search screen becomes a
   filter over aggregate data rather than a listing feed.
3. **Android first.** Play's review is faster and its appeal path more
   concrete; a Play approval is useful evidence in an Apple appeal.
4. **Web app fallback.** The mobile web UI already works. Not a store presence,
   but not zero either — and the API built here serves it equally.

**Do not** attempt to work around a rejection by obscuring what the app does.
A second rejection for misrepresentation costs the developer account, not just
the app.

### 2.4 Decision needed from the product owner

The one question this plan can't answer for you: **is a statistics-first app
worth shipping if listing links must stay off permanently?** If yes, the plan
below is right as written. If the product is only worth it with links, then
Phase 3 should be reordered to submit a minimal build *early*, purely to learn
the review outcome before more is built on the assumption.

---

## 3. Roadmap

Each phase ends with something verifiable, not with "done".

### Phase 0 — Foundations *(this change)*

| Delivered | Where |
|---|---|
| Versioned mobile API with feature flags, version gate, probes | `api_v1.py` |
| Contract tests, including the compliance flag | `tests/test_api_v1.py` |
| Container that runs identically on Render and Cloud Run | `Dockerfile` |
| Cloud Run service + Cloud Build pipeline | `deploy/` |
| React Native app: search, live results, market stats, about | `mobile/` |
| CI for both halves | `.github/workflows/ci.yml` |

**Exit:** `npm run typecheck && npx eslint .` and `python -m unittest discover
-s tests` both green; app runs against local Flask.

### Phase 1 — Mobile MVP on Render (2–3 weeks)

Nothing in this phase touches Cloud Run. Render is production and stays that
way.

- Deploy the API changes (Render auto-deploys the default branch).
- `eas init`, configure credentials, first `preview` builds for both platforms.
- Test on real devices against the Render deployment — especially the search
  flow on cellular, where the polling loop's backoff and background handling
  actually matter.
- Wire the App Store Connect and Play Console app records; reserve the bundle
  IDs (`com.gunscout.app`). Doing this early surfaces account/agreement
  problems while they're cheap.
- Ship to TestFlight internal + Play internal testing.

**Exit:** an installable build on both platforms that runs a search end to end
against production and shows market stats.

### Phase 2 — Production hardening (3–4 weeks)

The work that makes a public app survivable, all of it backend-side.

- **Rate limiting per client** on `/api/v1/*/search`. Today a search is capped
  in depth (`GS_MOBILE_MAX_PAGES`) but not in frequency; every mobile search
  spends the operator's home bandwidth (§1) and there is no per-caller limit at
  all. This is the single largest gap before a public launch.
- **Structured logging + error reporting.** Request logs with timing, and a
  crash reporter in the app (Sentry via `expo-insights` or equivalent) — chosen
  with the "no data collected" privacy label in mind, so: crash traces only, no
  identifiers, disclosed if it changes the label at all.
- **Search abandonment.** A phone that force-quits mid-search leaves the search
  running server-side until its clients finish. Harmless at today's volume;
  worth an explicit cancel endpoint before it isn't.
- **Backpressure when the worker is offline.** `meta.features.worker_online` is
  already surfaced in the app; add server-side shedding so a busy period
  doesn't queue jobs nobody will claim.

**Exit:** a load test of 50 concurrent searches that neither exhausts memory
nor saturates the worker link.

### Phase 3 — Store submission (2–4 weeks, mostly waiting)

- Privacy policy and support URLs (required by both stores; static pages, can
  be served from the existing Flask app).
- Store listings per §2.2; screenshots for every required device size.
- Submit to Play closed testing first, then Apple review.
- Iterate on rejections against the §2.3 ladder.

**Exit:** approved on at least one store, with the review's actual position on
listing links recorded — that answer drives the flag settings from then on.

### Phase 4 — Cloud Run readiness (4–6 weeks, only when needed)

Triggered by evidence, not by date. The triggers: sustained memory pressure on
one instance, search latency dominated by queueing, or a need for zero-downtime
deploys that Render's plan doesn't give. Until one of those is real, this phase
is a liability — it trades a working single-process design for a distributed
one.

The work itself is §4.2.

**Exit:** two instances serving the same searches and the same market stats,
verified by starting a search against one and polling it from the other.

### Phase 5 — Beyond parity

Ordered by value, and every one of them is safely inside the §2 posture:

1. **Saved searches with push alerts** — "tell me when a Gen5 19 lists under
   $450". The `is_new` flag already distinguishes first-seen listings. This is
   the feature that makes the app worth keeping installed; it needs a device
   token store (the first user-identifying data the backend would hold, so it
   changes the privacy label and deserves its own design).
2. **Ballistics calculator** — port `static/ballistics_engine.js`. Self-contained,
   works offline, entirely uncontroversial with reviewers.
3. **Price history** — the fact store currently keeps one price per listing
   (`statstore.py` upserts by URL hash). Real history means a schema change;
   worth doing after Phase 4 has moved stats to a real database.
4. **Barcode/UPC lookup** — `Listing.upc` already exists; the camera makes it
   an in-store "is this a fair price?" tool, which is the app's best case for
   existing at all.

---

## 4. Backend architecture

### 4.1 Today, and why it's fine

One process, one instance, everything in RAM. This is not technical debt at
current scale — it's why stats are O(1) on the search hot path and why the
whole system is one deployable unit. It is only a problem the moment a second
instance is needed, and then it is a total one.

### 4.2 State externalization — the actual scaling work

Nothing here is required for the mobile app. It's required for a *second
instance*. Four pieces of state, in the order they should move:

| # | State | Today | Target | Why this order |
|---|---|---|---|---|
| 1 | Market facts | `statstore.ENGINE._facts`, mirrored to SQLite | **Cloud SQL (Postgres)**, with the in-memory index kept as a read cache per instance and invalidated by the version counter | The largest memory consumer, the only durable data, and already has a version counter to build cache invalidation on |
| 2 | Search state + results | `store._searches` | **Memorystore (Redis)**, keyed by search id, TTL'd at the existing 15 min | Naturally TTL'd and naturally key-value; the row-append pattern maps to a Redis list; results are transient by design |
| 3 | Remote job queue | `remote._jobs` | **Redis** (same instance) — a list per site plus a claimed-set with visibility timeout | Must move with #2: `remote.complete()` writes search state directly |
| 4 | Shared tokens | `statstore` kv table | Moves with #1 | Small; comes along for free |

Two things that do **not** need to move:

- **The close poller** (`close_poller.py`) and the **job sweeper** — background
  loops that must run exactly once, not once per instance. On Cloud Run they
  become a **Cloud Scheduler** job hitting an authenticated endpoint, which is
  cleaner than the leader election the alternative would need.
- **The worker protocol.** `worker.py` speaks HTTP to whatever the deployment
  is; a Cloud Run URL is a URL. `guard.py`'s security model — server is
  untrusted, host allowlist is a constant, criteria not URLs — is unchanged and
  should stay exactly as it is.

The sequencing that matters: **#1 alone is worth doing before any of the
others**, because it also fixes the free-tier stats loss on every redeploy. It
can ship to Render, on Render's Postgres, with no Cloud Run involvement at all.
That is the highest-value item in this entire document, and it is independent
of mobile.

### 4.3 Render and Cloud Run, side by side

Render remains production. Cloud Run is a standby that must not rot, which is
the whole reason for one shared `Dockerfile` — both platforms deploy the same
image from the same commit, so the standby is exercised rather than
theoretical.

```
                       ┌──────────────────────────┐
   App / Web ─────────▶│  Render (production)     │◀── operator's worker
                       │  gun-scout.onrender.com  │    (home connection)
                       └──────────────────────────┘
                                   ▲
                        same image, same commit
                                   ▼
                       ┌──────────────────────────┐
                       │  Cloud Run (standby)     │
                       │  max-instances = 1       │
                       └──────────────────────────┘
```

Cutover, when a trigger from Phase 4 fires: deploy to Cloud Run, point the
worker at it (one env var) and confirm jobs flow, move the mobile app's API URL
via a `preview` build, then move DNS. Rollback is the same steps reversed, and
Render keeps running throughout.

Three Cloud Run specifics that are easy to get wrong and are already handled in
`deploy/cloudrun/service.yaml`:

- **`cpu-throttling: false`.** Cloud Run's default freezes CPU between
  requests. Gun Scout has three daemon threads that do work between requests —
  the stat flusher above all. Under the default, dirty facts would sit
  unflushed and dead worker jobs would never be released. This setting is not
  an optimization; it is a correctness requirement.
- **`maxScale: 1`,** until §4.2 lands. Two instances = two divergent markets.
- **SQLite over a GCS fuse mount is an interim measure only.** It works solely
  because there is exactly one writer. It must not outlive the `maxScale: 1`
  pin — which is another way of saying #1 in §4.2 is the real unlock.

### 4.4 Cost

| | Render (now) | Render (Starter) | Cloud Run (Phase 4) |
|---|---|---|---|
| Compute | $0 (sleeps after 15 min) | ~$7/mo | ~$25–40/mo at min-instances=1, CPU always on |
| Durable state | none (stats reset on deploy) | 1 GB disk, ~$0.25/mo | Cloud SQL smallest tier ~$10–25/mo |
| Notes | first request after idle ~30 s | always on | scales, but only after §4.2 |

The honest summary: **Render Starter plus Postgres is the right spend for the
foreseeable future.** Cloud Run's value is elasticity, and this app's ceiling
is the operator's home bandwidth long before it's CPU.

### 4.5 Observability

Minimum worth having before a public launch, in priority order: request logs
with latency and status; the existing per-site health checks exposed as a
metric rather than only an operator page; alerting on `readyz` failing and on
worker-offline exceeding a threshold; app-side crash reporting. A "structure
changed" alert (`StructureError`) deserves to page someone — it means a scraper
is silently returning nothing.

---

## 5. Mobile app architecture

Stack, and why each piece:

| Choice | Reason |
|---|---|
| **Expo (SDK 57), managed** | EAS Build removes the need for a Mac in the loop for iOS builds, and OTA updates let a JS-only fix skip review. The escape hatch (`expo prebuild`) exists if a native module ever demands it |
| **expo-router** | File-based routing, typed routes, and deep links for free — the latter matters for push notifications in Phase 5 |
| **TanStack Query** | Caching, retries and request cancellation for read-only server state, with retry policy tuned for cellular |
| **Hand-rolled polling hook** | Live search accumulates rows across responses behind a cursor; that is not a shape React Query models, and pretending otherwise would be worse than 100 explicit lines |
| **No state library** | There is no client state worth a store. Server state is Query's; screen state is `useState` |
| **TypeScript, strict** | The wire types are hand-mirrored from `models.py`; strictness is what makes that mirror hold |

Three structural decisions, each documented at the code that implements them:

1. **The server describes the search form** (`/api/v1/<vertical>/schema`).
   Adding a filter server-side reaches builds shipped months earlier. Given
   store review latency, every capability that can live on the server should.
2. **Capability is server-flagged** (`/api/v1/meta`). §2's switch is the
   motivating case, but the pattern generalizes: the app asks what it may do.
3. **Nothing about the user leaves the phone.** No account, no analytics, no
   permissions; recent searches are device-local. This mirrors a property the
   web app already has, and it's what keeps the privacy declarations true.

Deliberately **not** built: authentication, payments, in-app purchases, chat,
listing submission. Each would add a review surface for no product value.

---

## 6. Release engineering

- **Versioning.** Marketing version in `app.config.ts`; build numbers owned by
  EAS (`appVersionSource: remote`, `autoIncrement`). Never hand-edited — a
  duplicate build number is the most common cause of a failed upload.
- **Channels.** `development` (local backend) → `preview` (internal, production
  backend) → `production`. The API URL comes from the build profile, so a
  production binary cannot ship pointing at a laptop.
- **OTA updates** are for JS-only fixes that don't change what the app does.
  Anything that changes behaviour or adds a capability goes through review —
  both stores prohibit using OTA to alter an app's reviewed purpose, and in
  this category that line is worth staying well clear of.
- **The kill switch.** Every request carries `X-Client-Version`; raising
  `GS_MIN_CLIENT_VERSION` on the server retires a build with a 426 and an
  upgrade prompt. Cheap insurance for the one class of bug that cannot be
  rolled back.
- **Backward compatibility.** `/api/v1` only ever grows additively. A breaking
  change is `/api/v2` served alongside it, with v1 retired only when telemetry
  shows nobody on it — which, with no analytics, means "after a very long
  time." Design v1 responses accordingly.

---

## 7. Risks

| Risk | Likelihood | Impact | Response |
|---|---|---|---|
| Store rejection under firearms policy | **High** | Existential for the store channel | §2.3 ladder; links off by default; statistics-first framing |
| Operator's home connection is the bandwidth ceiling | High | Search quality degrades under load | Per-client rate limits (Phase 2); `GS_REMOTE_SITES` narrowing; the cap already applied to mobile searches |
| A retailer redesigns and a scraper breaks | Certain, recurring | One source silently empty | `StructureError` already detects it; make it alert (§4.5); the app already shows per-site status rather than a bare empty state |
| Single-instance memory growth | Medium | Outage | §4.2 item #1; watch memory before it's urgent |
| Retailer objects to being scraped | Medium | Source removed | Sources are already independent and degrade individually; keep the polite-crawl posture (~0.8 s between pages) |
| Apple/Play policy changes again | Medium | Re-review | Server flags mean most responses ship in minutes |

---

## 8. Immediate next steps

1. Merge this branch; confirm Render deploys and `/api/v1/meta` answers in
   production.
2. `cd mobile && eas init`, then a `preview` build for both platforms.
3. Reserve `com.gunscout.app` in both consoles — early, because account issues
   are slow to resolve.
4. Answer §2.4: is a statistics-first app worth shipping if links stay off?
5. Start §4.2 item #1 (stats → Postgres) on Render. It's the highest-value
   backend work available, it's independent of everything mobile, and it makes
   the Cloud Run path real rather than aspirational.

---

## Appendix — new configuration

| Variable | Default | Purpose |
|---|---|---|
| `GS_MOBILE_LISTING_LINKS` | `false` | May the app receive listing URLs? §2's switch — enforced server-side |
| `GS_MIN_CLIENT_VERSION` | `0.0.0` | Oldest app build allowed to call `/api/v1`; older ones get 426 |
| `GS_CORS_ORIGINS` | *(empty)* | Browser origins allowed to call `/api/v1`. Native builds don't need it; every entry widens access |
| `GS_MOBILE_MAX_PAGES` | `5` | Page ceiling for mobile searches, applied regardless of what the client asks for |
