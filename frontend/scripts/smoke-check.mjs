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
