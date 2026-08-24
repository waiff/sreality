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
 * sprint's own arrows:
 *   /pipeline 27 -> ~12  (W1 splits the enrichment out of the blocking chain,
 *                         W2a kills 6 duplicated bootstrap reads, W2b gates
 *                         the city-quality paging)
 *   /browse   31 -> ~20  (W2a's 6, plus W2b gating the 8-page city-index walk)
 * Six of every route's requests are the entitlements x3 + plans x3 bootstrap
 * (auth.tsx re-runs per auth event) — W2a deletes those app-wide at once.
 *
 * Counting rule: only PostgREST + Railway API calls (see isAppDataRequest).
 * Routes are the operator's daily path plus the two that hid defects. */
const ROUTE_BUDGETS = [
  { path: '/browse',        baseline: '31 req / 2.8s', maxRequests: 40, maxMs: 20000 },
  { path: '/pipeline',      baseline: '27 req / 1.7s', maxRequests: 34, maxMs: 15000 },
  { path: '/collections',   baseline: '9 req / 0.8s',  maxRequests: 16, maxMs: 12000 },
  { path: '/watchdog',      baseline: '10 req / 0.9s', maxRequests: 18, maxMs: 12000 },
  { path: '/notifications', baseline: '9 req / 0.9s',  maxRequests: 16, maxMs: 12000 },
  { path: '/brokers',       baseline: '11 req / 1.7s', maxRequests: 18, maxMs: 20000 },
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
