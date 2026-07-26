/*
 * Cabela's Coveo token minter.
 *
 * Cabela's is behind Akamai Bot Manager, which demands the `_abck` sensor
 * cookie that only a real browser running Akamai's JS produces. So we drive a
 * real browser (Playwright + stealth + a persistent profile, using the
 * installed Edge for a genuine fingerprint), let it load the site, and capture
 * the anonymous Coveo search token the site's own frontend requests. The token
 * then works from plain HTTP against Coveo's cloud for ~4 hours.
 *
 * Prints a single JSON line to STDOUT on success: {"token":"...","mintedAt":<ms>}
 * All diagnostics go to STDERR so stdout stays machine-parseable.
 * Exit 0 = token captured, 2 = ran but no token (blocked), 1 = crash.
 *
 * Env:
 *   CABELAS_MINT_HEADLESS=1   run headless (works once the profile is warmed;
 *                             falls back to headed automatically if it fails)
 *   CABELAS_PROFILE_DIR=path  persistent profile dir (default ./profile)
 */
const path = require('path');
const fs = require('fs');
const { chromium } = require('playwright-extra');
const stealth = require('puppeteer-extra-plugin-stealth')();
chromium.use(stealth);

const PROFILE_DIR = process.env.CABELAS_PROFILE_DIR || path.join(__dirname, 'profile');
const HOMEPAGE = 'https://www.cabelas.com/';
const PRODUCT = 'https://www.cabelas.com/shop/en/101989377';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const err = (...a) => console.error('[mint]', ...a);

async function human(page) {
  const pts = [[140, 200], [360, 320], [600, 260], [820, 440], [520, 560], [280, 400]];
  for (const [x, y] of pts) {
    await page.mouse.move(x + Math.random() * 40, y + Math.random() * 40, { steps: 10 });
    await sleep(150 + Math.random() * 200);
  }
  await page.mouse.wheel(0, 700); await sleep(400);
  await page.mouse.wheel(0, -300); await sleep(300);
}

let activeCtx = null;   // so the watchdog can tear the browser down on timeout

async function attempt(headless) {
  err(`launching edge headless=${headless} profile=${PROFILE_DIR}`);
  // If the profile is locked (orphaned browser) this throws; the caller handles
  // it. Everything AFTER creation is wrapped so the context is always closed.
  const ctx = await chromium.launchPersistentContext(PROFILE_DIR, {
    headless,
    channel: 'msedge',
    viewport: { width: 1280, height: 800 },
    locale: 'en-US',
    args: ['--disable-blink-features=AutomationControlled'],
  });
  activeCtx = ctx;
  let token = null;
  try {
    ctx.on('response', async (resp) => {
      if (resp.url().includes('getCoveoToken') && resp.status() === 200) {
        try { token = (await resp.json()).token || token; } catch {}
      }
    });
    const page = ctx.pages()[0] || await ctx.newPage();

    // The token is fired by a BACKGROUND fetch after the app JS boots, so we
    // never wait for the page to fully load (it often hangs under automation).
    // We commit the navigation, then poll the response listener while nudging
    // the Akamai sensor with a little interaction.
    const pollFor = async (secs) => {
      for (let i = 0; i < secs && !token; i++) await sleep(1000);
    };
    const go = async (url) => {
      try { await page.goto(url, { waitUntil: 'commit', timeout: 30000 }); }
      catch (e) { err('goto', url, e.message.slice(0, 50)); }
    };

    await go(PRODUCT);          // a product page reliably requests the token
    await pollFor(12);
    if (!token) { await human(page); await pollFor(12); }
    if (!token) { await go(HOMEPAGE); await human(page); await pollFor(20); }
  } finally {
    activeCtx = null;
    await ctx.close().catch(() => {});
  }
  return token;
}

// Cooperative watchdog: if we ever run away, close the browser and exit BEFORE
// Python's hard timeout has to kill the tree, so we don't orphan Edge / lock
// the profile. Unref'd so it never keeps the process alive on the happy path.
const WATCHDOG_MS = 270000;   // < cabelas_token.py MINT_TIMEOUT_S (300s)
setTimeout(async () => {
  err(`watchdog: exceeded ${WATCHDOG_MS}ms, tearing down`);
  try { if (activeCtx) await activeCtx.close(); } catch {}
  process.exit(2);
}, WATCHDOG_MS).unref();

(async () => {
  const startHeadless = process.env.CABELAS_MINT_HEADLESS === '1';
  let token = null;
  try {
    token = await attempt(startHeadless);
    if (!token && startHeadless) {
      err('headless failed; retrying headed');
      token = await attempt(false);
    }
  } catch (e) {
    err('FATAL', e.message);
    process.exit(1);
  }
  if (!token) { err('no token captured'); process.exit(2); }
  // flush stdout before exiting so the token line can't be lost on any platform
  process.stdout.write(JSON.stringify({ token, mintedAt: Date.now() }) + '\n',
    () => process.exit(0));
})();
