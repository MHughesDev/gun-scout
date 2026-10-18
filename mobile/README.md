# Gun Scout — mobile app

React Native (Expo SDK 57) client for the Gun Scout backend. iOS and Android
from one codebase; the web export exists for quick checks, not as a product.

The long-term plan this implements — including the app-store policy
constraints that shape it — is in [`../docs/mobile-platform-plan.md`](../docs/mobile-platform-plan.md).

## Run it

```bash
npm install
npm start           # then press i / a, or scan with a dev client
```

Point it at a backend with `EXPO_PUBLIC_API_URL` (default
`http://localhost:8777`, i.e. `python app.py` in the repo root):

```bash
EXPO_PUBLIC_API_URL=https://gun-scout.onrender.com npm start
```

Two notes for local backends:

- iOS simulator reaches your machine at `localhost`; an Android emulator needs
  `http://10.0.2.2:8777`, and a physical device needs your LAN IP.
- Only the browser-based web export is subject to CORS. If you use it, add its
  origin to `GS_CORS_ORIGINS` on the server — native builds need nothing.

```bash
npm run typecheck   # tsc --noEmit
npm run lint
```

## How it's put together

```
src/
  api/          the wire contract with /api/v1 — types, fetch client, queries
  hooks/        live-search polling, recent searches (device-local)
  components/   presentational pieces (schema-driven form field, result card)
  app/          expo-router routes; (tabs) = Search / Market / About
```

Three decisions explain most of the code:

**The server describes the search form.** `/api/v1/<vertical>/schema` returns
the fields, presets and columns for each engine, and `components/schema-field.tsx`
renders them. Adding a filter — or a whole vertical — server-side reaches
builds that shipped months earlier, which matters because an installed binary
can be arbitrarily old.

**Capability is server-flagged, not compiled in.** `/api/v1/meta` carries the
feature flags. The important one is `listing_links`: whether the app may show
a link out to a retailer's listing page is an app-store policy question
(plan §2), so it is a switch the server owns and enforces — with it off, the
URLs never reach the device.

**Nothing about the user leaves the phone.** No account, no analytics, no
permissions. Recent searches live in `AsyncStorage` as input values. That's
what the About tab says, and what the Play data-safety form and Apple privacy
label must keep saying.

## Builds and releases

EAS profiles live in `eas.json`; `app.config.ts` reads the API URL from the
build profile so a production binary can't ship pointing at a laptop.

```bash
eas build --profile preview   --platform all     # internal distribution
eas build --profile production --platform all
eas submit --profile production --platform ios   # / android
```

Build numbers are owned by EAS (`appVersionSource: remote`, `autoIncrement`);
never hand-edit an iOS `buildNumber` or Android `versionCode`. The marketing
version is `VERSION` in `app.config.ts` — bump it in the release commit.

`app.config.ts` also sends that version as `X-Client-Version` on every request,
which is what lets the server retire a broken build: it answers `426`, and the
app is expected to treat that as "update required" rather than an error.
