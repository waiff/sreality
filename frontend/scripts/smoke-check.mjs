#!/usr/bin/env node
// Production smoke-check: logs into the live SPA as the dedicated admin test
// account, confirms Browse renders, and opens+closes Merge mode without ever
// touching a card checkbox — the two mutating buttons (Merge, Link as same
// building) stay `disabled` until >=2 cards are selected, so this script can
// never trigger a production write. See CLAUDE.md "Autonomy and the safety
// net" and the `prod-smoke-check-accounts-and-recipe` memory.
//
// Normal setup:     npx playwright install chromium   (once)
//                    npm run smoke-check:prod
// Restricted/no-sudo sandbox: see the memory above for the apt-get/dpkg-deb
// workaround, then set SMOKE_CHECK_CHROMIUM_PATH to the extracted binary.
//
// FIXTURE THIS SCRIPT DEPENDS ON (hydration sprint W0): the admin test account
// must own a few pipeline cards. It owned none, so the board's hydration chain
// short-circuited on an empty result and five of its six hops were never
// exercised by any automated check — the class of regression this sprint is
// about was structurally invisible. Recreate the fixture with:
//
//   insert into property_pipeline (property_id, stage_id, account_id, board_position)
//   select v.pid,
//          (select id from pipeline_stages
//            where account_id = :acct and archived_at is null
//            order by case when v.slot = 0 then 0 else 1 end, position
//            limit 1),
//          :acct, v.slot + 1
//   from (values (5,0),(6,0),(18,2),(28,3)) as v(pid, slot)
//   on conflict (property_id, account_id) do nothing;
//
// with :acct = the admin test account (resolve it from account_members, don't
// hardcode). The four properties are active, priced, and carry photos, so the
// cover and broker enrichments have something real to fetch.

import { chromium } from 'playwright';
import { readFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '../..');

function loadRootEnv() {
  try {
    const text = readFileSync(path.join(REPO_ROOT, '.env'), 'utf8');
    for (const line of text.split('\n')) {
      const m = line.match(/^([A-Z0-9_]+)=(.*)$/);
      if (m && !(m[1] in process.env)) process.env[m[1]] = m[2];
    }
  } catch {
    // no root .env — fine as long as credentials are already in the
    // environment (e.g. injected as CI secrets)
  }
}
loadRootEnv();

const BASE_URL = process.env.SMOKE_CHECK_BASE_URL || 'https://sreality-production-1bb6.up.railway.app';
const SHOT_DIR = path.join(__dirname, '.smoke-shots');
mkdirSync(SHOT_DIR, { recursive: true });

const email = process.env.SMOKE_TEST_ADMIN_EMAIL;
const password = process.env.SMOKE_TEST_ADMIN_PASSWORD;
if (!email || !password) {
  console.error(
    'Missing SMOKE_TEST_ADMIN_EMAIL / SMOKE_TEST_ADMIN_PASSWORD (repo-root .env or environment).'
  );
  process.exit(2);
}

/* Time-to-first-card on /pipeline. The board's own north-star target is one
 * round trip to interactive cards; this is the guardrail, not the goal. */
const BUDGET_FIRST_CARD_MS = Number(process.env.SMOKE_BUDGET_FIRST_CARD_MS || 12000);

/* Per-route budgets. `baseline` is what this very script measured on
 * 2026-08-24 against production, after the W-1a/W-1b hotfixes and with the
 * smoke account's pipeline cards seeded — so a future reader can tell a
 * tightened budget from an invented one. Enforced numbers sit modestly above
 * the baseline: enough headroom for instance weather, tight enough that a
 * STRUCTURAL regression (a new waterfall level, a read that starts firing
 * per-card, a duplicated bootstrap) trips them.
 *
 * These are RATCHETS, not targets. Every wave that removes requests lowers the
 * number it earned, in its own PR, with the new measurement quoted here. The
 * ratchet history:
 *   2026-08-24 W0  — first measurement, post-W-1a/W-1b, smoke cards seeded.
 *   2026-08-24 W2a — every route dropped 4. The entitlements x3 + plans x3
 *                    bootstrap collapsed to one of each (auth.tsx keys on
 *                    user.id now, not the session object) and the unread badge
 *                    is gated on its own nav entry: /collections 9->5,
 *                    /watchdog 10->6, /notifications 9->5, /brokers 11->7,
 *                    /browse 31->27, /pipeline 27->23. W1 shipped in between
 *                    and changes ORDER, not count — the board paints after 2
 *                    reads instead of waiting on all 6 (time-to-first-card
 *                    1,427ms -> 951ms measured live).
 *   2026-08-24 W2b — /browse 27->22, /pipeline 23->19. Every fetchAllRows call
 *                    site now requests count:'exact', so any exhaustive read
 *                    whose whole result fits on page 1 (most of them: curated
 *                    cities, index definitions, pipeline members, pipeline
 *                    board, collection membership, …) skips the old
 *                    terminating empty-page probe outright — that is most of
 *                    the drop, spread across many small reads rather than one
 *                    big one. /collections, /watchdog, /notifications,
 *                    /brokers are unchanged — this wave didn't touch them.
 *   2026-08-24 W5  — /pipeline 19->18. pipeline_board_public (migration 417)
 *                    moved the pipeline/property join server-side, so the
 *                    board's structural read is one request instead of two
 *                    sequential ones — the second used to wait on the
 *                    first's ids. Bigger win than the request count shows:
 *                    time-to-first-card measured 412ms live (was in the
 *                    1,000-1,700ms range across W1-W10a), since the second
 *                    request no longer serializes behind the first.
 *   2026-08-24 fix — /collections 16->10, /watchdog 18->12, /notifications
 *                    16->10, /brokers 18->12. These four never actually got
 *                    the ceilings W2a earned them: that ratchet PR (#1127)
 *                    shipped only its comment, because the script editing this
 *                    block aborted on a later assertion before writing the
 *                    file, and the commit message was trusted instead of the
 *                    diff. They sat at ~3x slack (measured /collections 5
 *                    against a ceiling of 16), so a doubling could have
 *                    regressed unseen. Corrected to what is measured today.
 *   2026-08-24 W6  — /pipeline 18->17. Migration 419 put primary_email /
 *                    primary_phone on listing_broker_public, so the board's
 *                    broker decoration reads identity AND contact in ONE call
 *                    instead of chaining GET /brokers?ids= behind
 *                    POST /brokers/by-listings for a contact pair the first
 *                    read's join had already touched (the second read was 207
 *                    execution + 436 planning buffers of pure duplication, plus
 *                    a second Railway floor, serialized because its broker_ids
 *                    came out of the first response). Listing detail lost the
 *                    same second call — not visible here, this sweep has no
 *                    /listing route. Measured live post-deploy: 17 req / 0.9s,
 *                    time-to-first-card 368ms.
 * Still ahead: W9b appends columns to listings_public for the listing-detail
 * chain; W7a moves Browse + comparables onto the shared hydration layer.
 *
 * Counting rule: only PostgREST + Railway API calls (see isAppDataRequest).
 * Routes are the operator's daily path plus the two that hid defects. */
const ROUTE_BUDGETS = [
  { path: '/browse',        baseline: '22 req / 4.9s', maxRequests: 27, maxMs: 20000 },
  { path: '/pipeline',      baseline: '17 req / 0.9s', maxRequests: 22, maxMs: 15000 },
  { path: '/collections',   baseline: '5 req / 0.9s',  maxRequests: 10, maxMs: 12000 },
  { path: '/watchdog',      baseline: '6 req / 1.3s',  maxRequests: 12, maxMs: 12000 },
  { path: '/notifications', baseline: '5 req / 1.0s',  maxRequests: 10, maxMs: 12000 },
  { path: '/brokers',       baseline: '7 req / 1.6s',  maxRequests: 12, maxMs: 20000 },
];

/* An "app data request" is a read of OUR data: PostgREST on the Supabase host
 * or the Railway API. Deliberately excludes static assets, fonts, R2 image
 * bytes and third-party basemap tiles — /browse fires ~66 OpenFreeMap tile
 * requests, which would swamp any budget and say nothing about our code. */
function isAppDataRequest(url) {
  if (/\/assets\/|\.(js|css|woff2?|png|jpe?g|svg|ico|webp)(\?|$)/i.test(url)) return false;
  if (/openfreemap|tiles?\.|basemaps|fonts\.g/i.test(url)) return false;
  if (/r2\.cloudflarestorage\.com|\/images\//i.test(url)) return false;
  return /supabase\.co\/rest\/|supabase\.co\/auth\/|up\.railway\.app\//i.test(url);
}

const shortUrl = (u) => u.replace(/^https?:\/\//, '').split('?')[0].slice(0, 80);

const consoleErrors = [];
const pageErrors = [];
const failedRequests = [];
const steps = [];

function step(name, ok, detail) {
  steps.push({ name, ok, detail: detail || '' });
  console.log(`${ok ? 'PASS' : 'FAIL'} — ${name}${detail ? ' :: ' + detail : ''}`);
}

const launchOpts = { args: [] };
if (process.env.SMOKE_CHECK_CHROMIUM_PATH) {
  launchOpts.executablePath = process.env.SMOKE_CHECK_CHROMIUM_PATH;
  launchOpts.args.push('--no-sandbox');
}

(async () => {
  const browser = await chromium.launch(launchOpts);
  const page = await browser.newPage();

  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });
  page.on('pageerror', (err) => pageErrors.push(String(err)));
  page.on('requestfailed', (req) => {
    failedRequests.push(`${req.method()} ${req.url()} — ${req.failure()?.errorText}`);
  });

  try {
    await page.goto(`${BASE_URL}/login`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    step('load /login', true, page.url());
    await page.screenshot({ path: path.join(SHOT_DIR, '01-login.png') });

    await page.getByLabel('Email').fill(email);
    await page.getByLabel('Password').fill(password);
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();

    await page.waitForURL('**/browse', { timeout: 15000 });
    step('login redirects to /browse', true, page.url());

    await page.waitForLoadState('networkidle', { timeout: 20000 }).catch(() => {});
    await page.screenshot({ path: path.join(SHOT_DIR, '02-browse.png') });

    const mergeModeBtn = page.getByRole('button', { name: 'Merge mode', exact: true });
    await mergeModeBtn.waitFor({ state: 'visible', timeout: 15000 });
    step('Browse renders + "Merge mode" button visible', true);

    await mergeModeBtn.click();
    step('clicked "Merge mode" (local state only, no network call)', true);

    const mergeBtn = page.getByRole('button', { name: /^Merge($| \d)/ });
    const linkBtn = page.getByRole('button', { name: 'Link as same building', exact: true });
    await mergeBtn.waitFor({ state: 'visible', timeout: 5000 });

    const mergeDisabled = await mergeBtn.isDisabled();
    const linkDisabled = await linkBtn.isDisabled();
    step(
      'mutating buttons disabled pre-selection (no cards clicked)',
      mergeDisabled && linkDisabled,
      `merge disabled=${mergeDisabled}, link disabled=${linkDisabled}`
    );

    await page.screenshot({ path: path.join(SHOT_DIR, '03-merge-mode-active.png') });

    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    step('clicked "Cancel" — exited merge mode, zero cards ever touched', true);

    await page.screenshot({ path: path.join(SHOT_DIR, '04-after-cancel.png') });

    /* --- /pipeline: the board actually paints cards -------------------- *
     * The smoke account owns seeded pipeline cards precisely so this path
     * runs. Before that it owned none, so the board's hydration chain
     * (rows -> properties -> covers -> brokers) short-circuited on an empty
     * result and NOTHING here was ever exercised — a regression in it was
     * invisible to this script by construction. Read-only: the page is
     * loaded and inspected, never dragged (a drag is a production write). */
    const tFirstCard = Date.now();
    await page.goto(`${BASE_URL}/pipeline`, {
      waitUntil: 'domcontentloaded', timeout: 30000,
    });
    const cardLink = page.locator('a[href^="/listing/"]').first();
    await cardLink.waitFor({ state: 'visible', timeout: 30000 });
    const firstCardMs = Date.now() - tFirstCard;
    step(
      'pipeline board paints a card',
      true,
      `first card visible in ${firstCardMs}ms`,
    );
    step(
      `time-to-first-card under ${BUDGET_FIRST_CARD_MS}ms`,
      firstCardMs <= BUDGET_FIRST_CARD_MS,
      `${firstCardMs}ms`,
    );

    await page.waitForLoadState('networkidle', { timeout: 20000 }).catch(() => {});
    const priced = await page
      .locator('a[href^="/listing/"]')
      .filter({ hasText: /\d/ })
      .count();
    step('at least one card shows a price', priced >= 1, `${priced} priced cards`);

    const columns = await page.locator('ul[class*="min-h-24"]').count();
    step('stage columns rendered', columns >= 2, `${columns} columns`);
    await page.screenshot({ path: path.join(SHOT_DIR, '05-pipeline.png') });

    /* --- per-route budget sweep ---------------------------------------- *
     * One navigation per route, counting only APP data requests (PostgREST +
     * the Railway API) — third-party basemap tiles and static assets are
     * excluded, since 66 of /browse's 93 "requests" were OpenFreeMap tiles and
     * counting them would drown the signal.
     *
     * Budgets are ~2x the 2026-08-24 baseline: loose enough not to flap on
     * instance-load noise, tight enough to catch a STRUCTURAL regression — a
     * new waterfall level, a read that starts firing per-card, or a duplicated
     * bootstrap. The 5xx assertion is the sharp one: Browse's default view was
     * answering HTTP 500 for weeks and no automated check noticed. */
    for (const route of ROUTE_BUDGETS) {
      const seen = [];
      const onFinished = async (req) => {
        if (!isAppDataRequest(req.url())) return;
        let status = 0;
        try {
          const r = await req.response();
          status = r ? r.status() : 0;
        } catch { /* response never resolved; counted, status unknown */ }
        seen.push({ url: req.url(), status });
      };
      page.on('requestfinished', onFinished);
      const t0 = Date.now();
      let navOk = true;
      try {
        await page.goto(`${BASE_URL}${route.path}`, {
          waitUntil: 'networkidle', timeout: route.maxMs + 15000,
        });
      } catch {
        navOk = false;
      }
      const elapsed = Date.now() - t0;
      // Let late in-flight responses land before unhooking.
      await page.waitForTimeout(250);
      page.off('requestfinished', onFinished);

      const server5xx = seen.filter((r) => r.status >= 500);
      step(
        `${route.path} — no 5xx from app data requests`,
        server5xx.length === 0,
        server5xx.length
          ? server5xx.map((r) => `${r.status} ${shortUrl(r.url)}`).join(' | ')
          : `${seen.length} requests, all < 500`,
      );
      step(
        `${route.path} — at most ${route.maxRequests} app data requests`,
        seen.length <= route.maxRequests,
        `${seen.length} (baseline ${route.baseline})`,
      );
      step(
        `${route.path} — settles under ${route.maxMs}ms`,
        navOk && elapsed <= route.maxMs,
        `${elapsed}ms${navOk ? '' : ' (navigation timed out)'}`,
      );
    }
  } catch (err) {
    step('UNEXPECTED FAILURE', false, String(err));
  } finally {
    await browser.close();
  }

  console.log('\n--- console errors ---');
  console.log(consoleErrors.length ? consoleErrors.join('\n') : '(none)');
  console.log('\n--- page errors ---');
  console.log(pageErrors.length ? pageErrors.join('\n') : '(none)');
  console.log(
    '\n--- failed requests (informational — SPA navigation commonly aborts in-flight map/count requests) ---'
  );
  console.log(failedRequests.length ? failedRequests.join('\n') : '(none)');

  const allPassed = steps.every((s) => s.ok);
  console.log(`\n=== ${allPassed ? 'ALL STEPS PASSED' : 'SOME STEPS FAILED'} ===`);
  process.exit(allPassed ? 0 : 1);
})();
