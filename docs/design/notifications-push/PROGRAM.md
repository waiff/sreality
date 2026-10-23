# Notifications: one event, one feed, one push path

> **Status: APPROVED 2026-10-21 (all eleven rulings taken, recorded in § 6). Build in progress, W1 first.** The audit this program answers is in
> [AUDIT.md](./AUDIT.md) (28 confirmed findings, cited below as F1–F28). Build sequencing:
> § 5 Waves; every wave's PR updates the docs and skills it changes (CLAUDE.md rule) and the
> roadmap track. Operator brief: push through the iPhone app (the installed PWA) for watchdog,
> collection monitoring and shares; channels from now on are the in-app feed, email (kept, inactive)
> and iPhone push; no Telegram or other channels; subtraction is the deliverable.


*Operator proposal, 2026-10-21, revised after three critics; origin/main 8242bfad.*

**Terms.**
- **PWA:** the website installed on the Home Screen, our "iPhone app".
- **Push subscription:** the secret address Apple gives one installed app; our server posts to it.
- **VAPID:** the signature proving a push comes from us.
- **Migration:** a numbered database change. *Additive* ones we apply ourselves; *destructive* ones pause for your OK and a backup.
- **RLS:** the rule that each account sees only its own rows.
- **Lease:** a "mine for 2 minutes" stamp, so two workers never send one row and a crashed job comes back.

## 1. North star

**One real-world change becomes one immutable row, owned by one account, with one identity, decided in one place on one clock, shown in one feed and counted by one number. Each iPhone the account already had gets one ledger row for it unless it was read first, ending sent, skipped (with the reason) or failed. Whatever does not serve that sentence is deleted.**

1. **Detect, never route.** Producers write facts; recipients come from the event's account at send time; settings hold no channels, recipients, cadences or switches.
2. **One identity, nothing erased by our code.** One unique tuple decides "same event?" for producers and merges; a merge marks duplicates; deleting a watchdog retires it. Only today's cascades (account erasure, collection deletion) remain, unless you choose R2 (b).
3. **One number, one link, no orphan code.** Bell, badge and push count one visible set; every tap takes one route; every addition names its paying deletion.

## 2. What exists today and what is wrong

Live: 943 alerts (902 system_health, none read), 0 watchdogs, 2 monitored collections, 7 accounts, 5 users; nothing has ever left the app.

- **Routing leaks (F3, F7, F8):** three stores route alerts; the outbox would send everyone's to one operator address.
- **Delivery loses and duplicates (F2, F10, F15, F23, F26):** no lease, on an API Railway kills instantly; an FK contradicts its CHECK.
- **Wrong host and clock (F12, F20):** a stranded lease delays alerts ~17 min per deploy; price and delisting run daily.
- **Misses and floods (F1, F14, F22):** the cursor skips late facts (≤ 16%); resumes flood; blinks send "Delisted", then "Relisted".
- **Two identities, erased history (F6, F13, F21):** `dedupe_key` embeds a property id merges change; the carrier deletes distinct "twins"; deleting a watchdog erases its alerts.
- **Mutable links (F11, F19, F24, F28):** 6 of 39 rows open /listing/null.
- **Security (F4, F16, F17, F25):** Estimate rent leaks runs; any user runs platform scans; shares carry sender text, uncapped.
- **No phone bell (F9); system alerts drown it (F5):** "99+", and "Mark all read" mutes the canary (red on 14 of the last 24 runs).
- **Dead weight (F27):** Telegram, webhook, unsubscribe, suppression.

## 3. The target architecture

```
 lane "notifications" (300 s): watchdog_new | price | monitor      API: shares.grant
   -> notification_dispatches  UNIQUE(account, source, watchdog, collection,
        kind, property, event_ref, actor) WHERE duplicate_of IS NULL
   VISIBLE -> feed, unread_count -> bell + app badge
 lane "push" (15 s; brake = no VAPID key)
   fan-out (event x live device, same account) -> claim (2-min lease)
   -> 1 push per device -> api/push.py -> *.push.apple.com -> sent|gone|retry|failed
 tap: <origin>/n/{id}?d={delivery} -> seen + opened_at -> dispatchTarget(row)
```

### 3.1 Routing
One join, `ps.account_id = d.account_id AND ps.ended_at IS NULL`; the trigger already sets the account. SYSTEM has no members; push routes never act as another account, so a delegate's phone gets only the delegate's alerts; monitoring never reaches share recipients (ruling 5c). Share push overturns ruling 5(e) and rule 16's "in-app only" (rewritten in W4). "Push on" = a live device row.

### 3.2 Schema
**notification_dispatches: 21 columns → 15.**
- **`event_ref text`**, the discriminator within (source, kind, property): price = snapshot id; inactive = epoch of `inactive_at`; reactivated = the epoch of the inactive dispatch it answers (*as built in W2a, 2026-10-21: the key grammar already in production; not the dispatch id*); new_source = the introducing `listing_id` (sreality ids mapped once); shared_dashboard = dashboard id; shared_pipeline = mode; new, shared_collection = NULL; system history = old key, opaque.
- **`actor_account_id`** (ON DELETE CASCADE: a share notification cannot outlive the sharer's account, and SET NULL would let two owners' pipeline shares collide under the identity), who caused it (the sharer; NULL for detectors), replacing `message`: the bell renders the same sentence from `share_party_emails(actor)` and the live name, so no sender text is stored and the address drops out when a share ends (F17, R11).
- **`duplicate_of`**, the row this one duplicates.
- **One unique index** on the diagram's tuple, `NULLS NOT DISTINCT WHERE duplicate_of IS NULL` (Postgres 15+; prod and CI run 17), mirrored in Python as `EVENT_IDENTITY` with a pin test.
- **`change_kind`:** 'shared' splits into `shared_collection | shared_dashboard | shared_pipeline` (2 rows relabelled); `broker_change` (never emitted) goes; `system_alert` stays as closed history (R2).
- **Drop (W6):** `dedupe_key`, `trigger_snapshot_id`, `channel`, `status`, `error_message`, `target_channels`, `sreality_id` + index, `estimation_run_id` (F4), `message` (kept if R2 = c).
- **History (F21):** deleting a watchdog sets `retired_at`: it stops matching and leaves the list, and its alerts leave every read. FKs and trigger stay: the trigger fires on `UPDATE OF subscription_id, collection_id` (641:283-299), so "SET NULL" would fail.
- **Carrier:** LOCK/RESEND/DELETE become one UPDATE that re-points and sets `duplicate_of` where the survivor holds the identity; a merge deletes nothing. A split leaves alert rows in place (rule 18: alerts write no carry rows), so a split-off property under 24 h old may get 'new' once more: accepted, splits are rare.
- **Indexes:** one `(account_id, dispatched_at desc, id desc)` serves feed, count and fan-out, replacing the unread index, both dispatched_at indexes (057:46-47, 199:19-20) and, after EXPLAIN, source_kind_idx.

**notification_subscriptions:** drop `channels` and the cursor; add `active_since` (trigger, migration-230 pattern) and `retired_at`. **collections:** drop `notify_channels`.

**push_subscriptions (new), one row per installed app per account:** `id`, `account_id` (cascade), `endpoint` (unique; CHECK `'^https://[a-z0-9-]+(\.[a-z0-9-]+)*\.push\.apple\.com/'`), `p256dh` (65 bytes), `auth` (16 bytes), `origin` (https, the Origin header checked against `CORS_ALLOW_ORIGINS`: absolute links without `SPA_BASE_URL`, F11), `user_agent`, `created_at` (never updated), `ended_at`, `end_reason` (gone | signed_out | switched_off | replaced). Ending blanks the three secrets but keeps the row, for churn. A row's account never changes; another account registering a live endpoint ends the old row 'replaced'.

**notification_deliveries (new), replacing `channel_sends` (0 rows):** `id`, `notification_id` (cascade), `push_subscription_id`, `status` (pending | sent | skipped | gone | failed), `attempts`, `next_attempt_at` (the lease), `created_at`, `sent_at`, `opened_at`, `provider_message_id` (apns-id), `reason` (Apple's reason, or seen | hidden | stale | device_ended | capped; never an exception string). UNIQUE(notification_id, push_subscription_id).

Both tables: RLS on, no browser grant, in `_ADMIN_ONLY_RELATIONS`; neither names a property (rule-18 census untouched). **Dropped (W6):** channel_sends, notification_suppression, resend_webhook_events, notification_matcher_lease; 9 notification settings → 0.

### 3.3 Runtime: two worker lanes
- **`notifications`, constant 300 s** (the health/text_extract shape: no settings row; the brake is a deploy). Steps report through `portal_errors`, so `lane/notifications/<step>` goes red alone.
  - **watchdog_new:** one INSERT per live watchdog over `first_seen_at > greatest(now() − 24 h, active_since)`, image gate fixed; the cursor, lookback and two settings classes go.
  - **price:** one canonical-ad fragment (today two), 7 days from `active_since`/`monitor_since`.
  - **monitor:** 'inactive' only after 24 h gone; 'reactivated' keyed on its inactive dispatch's epoch; new_source unchanged (merge anchoring would push weeks-old ads). Price and monitor windows are constants (7 days); the image-gate timeout is clamped to 23 h so a held property is released inside the 24 h window (W2a).
- **`push`, constant 15 s.** One brake: remove `VAPID_PRIVATE_KEY` from the worker; the lane reports "unconfigured" (a warn cell, §3.9).
- **No lock:** producers are idempotent on the identity index; the claim is a compare-and-set.
- **The API keeps** the health teller (a dead worker cannot silence its own alarms) and `shares.grant`; `NOTIFICATIONS_MATCHER_DISABLED` becomes `HEALTH_TELLER_DISABLED` (W2b).
- **Deleted:** matcher_loop, the lease, 4 readers, 3 wrappers, 2 daily gates, the outbox loop and its 4 switches, 621's "api" cell. The lane check reads a LANES entry missing from the heartbeat as Watch, then Problem, so an older process beating beside a new one (a deploy overlap) shows red; a lone worker on old code runs the old check too, so Railway's per-service commit status is its signal, which the W2b gate checks.

### 3.4 Push transport and delivery
**`api/push.py`, ~85 lines:** RFC 8291 aes128gcm (matches the RFC vector) and RFC 8292 VAPID ES256 on the installed `cryptography` + PyJWT.
- `TTL` = max(60, seconds left of 24 h), so BadTtl cannot fire; `Urgency: high`.
- `Topic` = 22-character base64url of sha256(device id ‖ smallest delivery id)[:16]: it collapses a crash re-send of one batch, never another event.
- VAPID `sub` = the device's stored https origin (no `VAPID_SUBJECT`); JWT cached ≤ 12 h per Apple origin.
- Before each POST the host is re-checked with `urllib.parse` (https, hostname ending `.push.apple.com`, no userinfo, no port); `allow_redirects=False`, 10 s timeout.

| Outcome | Result |
|---|---|
| 201 | sent; apns-id stored |
| 404, 410, VapidPkHashMismatch | gone; the device ends |
| 429 | the device's due rows wait for Retry-After |
| 5xx, timeout, connection error | retry in 2ⁿ min; failed after 5 |
| other 4xx | failed |

**Endpoint secrecy.** push.py catches `requests.RequestException` and returns a kind plus Apple's reason, never a URL; the lane catches per device, so no endpoint reaches `_record_pass_failed` or `LOG.exception`; PUT validates before SQL, so no CHECK prints a "Failing row" (IntegrityError → bare 422); DELETE sends the endpoint in a JSON body. A test drives a ConnectionError through the lane: no push.apple.com path in heartbeat or logs.

**Pass.**
1. **Fan-out:** a row per (VISIBLE unseen event ≤ 24 h, live device older than it). A share past the cap (one per sender and recipient per 24 h, R11) is skipped as capped: bell only.
2. **Claim:** `UPDATE … SET next_attempt_at = now() + 2 min, attempts = attempts + 1 WHERE id IN (SELECT … due … FOR UPDATE SKIP LOCKED) RETURNING`, at most 10 devices (10 × 10 s < lease).
3. **Re-check:** seen, hidden, > 24 h or device ended → skipped with that reason.
4. **Coalesce:** one push per device per pass (Apple ignores `tag`, throttles bursts). One row: its own text and `/n/{id}?d=`; several: "N nových upozornění", 3 titles, `/notifications?d=`.
5. **Finalize per device** right after its POST, checking the stop signal between devices: a deploy re-sends at most one push, which Topic replaces if undelivered.

**Payload:** `{"web_push":8030,"notification":{"title","body","navigate","lang":"cs","app_badge":N},"app_badge":N}`, declarative without `mutable` (iOS 18.4+ shows it itself, with no silent-push penalty); `app_badge` twice since WebKit moved it.

**sw.js (+30), iOS 16.4–18.3:** `push` always calls `showNotification` ("Nové upozornění" → /notifications if unparsable) and `setAppBadge(app_badge)` in one `waitUntil`; `notificationclick` opens `data.navigate`. A vitest harness asserts both on every path (three silent pushes remove every subscription). It ships 24 h before W5; its header notes that unregistering ends push below 18.4.

**Routes (four), all in `_NEVER_ACTS`:**
- `GET /push/key`: `require_token`, no connection; the public key derived from `VAPID_PRIVATE_KEY` (one shared variable, API + worker; no `VITE_`), 503 when unset.
- `PUT`/`DELETE /push/subscription`, `POST /push/opened`: `require_account_id`, then `deps.open_background_conn` (the share routes' shape and rail, named in tenancy.md). PUT checks host, key lengths and Origin (non-Apple → 422), keeps 10 live devices (built: the least recently seen beyond 10 'replaced'; 5 in the plan), and in one transaction ends another account's live row for the endpoint, then upserts the caller's without touching `created_at`. DELETE ends it (signed_out | switched_off). opened stamps `opened_at` on the caller's tapped push.

### 3.5 Renderer and link
**Push text:** one Czech renderer by `change_kind`, from frozen columns plus the display label; no current price or per-m² figure (rule 23). Shares read "Sdíleno s vámi" / "Sbírka" | "Dashboard" | "Pipeline": no sender text, no URL (F17). The bell keeps English labels; a test pins both maps to the CHECK.

**Links:** `/n/:dispatchId` calls mark-seen under RLS, which returns the merge-re-pointed row, then replaces itself with `dispatchTarget(row)` (property, collection, dashboard, `/pipeline`; 404 → /notifications). Both shapes send `d` to `/push/opened`; bell rows share `dispatchTarget` but never stamp `opened_at`. So taps and bell reads differ, merged properties land on the survivor, and `/listing/null` and the key parsers go.

### 3.6 Feed and phone UI
**VISIBLE**, one SQL fragment: not dismissed by the event's own account, `duplicate_of IS NULL`, watchdog not retired (RLS and the service role agree on all three). Under R2 (a)/(b) W1 drops the SYSTEM read arm and admin update policy, so bell, badge and push count one set once the badge ships. The unread route returns only `unread_count`.

**Phone (R4):**
- A bell with the count beside Účet; one unread poll in Shell serves both frames and `setAppBadge`.
- The account sheet's switch "Oznámení na tomto iPhonu": off, on, "Přidejte aplikaci na plochu", denied (Settings hint), "Oznámení zatím fungují jen na iPhonu".
- Opening the sheet runs `registration.update()` and fetches the key, compared only after a 200 (a 503 never unsubscribes); the tap calls `subscribe({userVisibleOnly: true, applicationServerKey})` at once.
- **Binding follows identity, not a button.** The auth.tsx identity-transition effect (it covers token-driven switches too) unsubscribes locally on any user change or SIGNED_OUT (no JWT needed; the row ends 'gone' at its next push) and clears the badge; explicit sign-out first sends a best-effort DELETE (≤ 3 s). No subscription outlives its user.
- Re-sync PUT on sign-in and on the existing hourly visibilitychange check (iOS never reports a changed subscription).
- /notifications gets a phone-width pass and "Load more"; tab names stay English. On desktop the picker, Settings › Delivery, System tab and /watchdog feed go (R1–R3).

### 3.7 Email (dormant)
Delete the unused Resend transport, webhook, unsubscribe and suppression (R5). Later, email becomes a second channel on `notification_deliveries`, to each account's sign-in address. Supabase Auth email is untouched.

### 3.8 system_health
- **R2 (a)/(b):** delete the emit INSERT, composers and `system_health_channels`; the SYSTEM trigger branch raises; W1 drops the two SYSTEM policy arms (a narrowing that deletes no row).
- **R2 (c):** the emitter stops writing routing columns (W1) and moves onto `event_ref` (W3), so W6 is safe either way.
- **Canary:** today's rhythm, minus the bell-read escape. It fails for an unacknowledged alarm whose onset or latest rung (6 h, 24 h, 72 h, weekly; `ops_incidents.alerted_at`) is between grace and 12 h old (R9), and reads no `channel_sends`, `seen_at` or `dedupe_key`.

### 3.9 Measures (data quality)
**`notifications` Health check (W4: module, CHECKS row, `owner_path`, test):** **stall**, a pending row due > 10 min; **recall**, a VISIBLE unseen event < 24 h, an older live device, and no delivery row 5 min after dispatch (lost pushes); **failures**, ≥ 3 failed in 24 h; **brake shown**, push unconfigured while a live device exists (warn). The three-strike rule is prevention-only: no server signal sees it.

**Gauges (design-doc SQL, reviewed after 7 days):** false-new ('new' where another ad was first seen > 1 day earlier; baseline recomputed in W2a, as 5.1% used > 7 days); double-new (`duplicate_of`); flap (Delisted → Relisted ≤ 48 h); detection and send latency; delivery and skip mix; rows per apns-id; open rate (`opened_at` ≤ 30 min after `sent_at`); churn by `end_reason`.

## 4. Additions vs deletions ledger

Deletions made whatever the rulings pay for every addition: Telegram (~200), outbox (372), channel_client (270), matcher_loop + lease (~250), cursor + duplicate detector (~240), carrier LOCK/RESEND (~40): ≈ 1,370 lines against ≈ 1,000 new.

| Wave | Adds | Removes | Net lines |
|---|---|---|---|
| W1 | Load more, dispatchTarget, canary rung check ~70 | Estimate rent ~310, matcher/run, outbox, channel_client, transports, webhook, unsubscribe, picker, Settings › Delivery, Watchdog.tsx feed ~730, System tab, stamping sites | ≈ −2,500 |
| W2 | lane ~150 | matcher, lease, readers, wrappers, gates, cursor, a detector, "api" cell | ≈ −400 |
| W3 | identity index + pin, carrier UPDATE | key writes and parsers, LOCK/RESEND/DELETE | ≈ −20 |
| W4 | push.py, lane, renderer, routes, Health check, sw.js ~425 | none | ≈ +425 |
| W5 | bell, observer, switch, binding, /n page ~200 | TopBar poll, duplicate unread query, 4 api.ts functions | ≈ +120 |
| W6 | — | 4 tables, 12 columns, 9 settings, 4 indexes, 1 function | cols 21 → 15 |
| Tests, docs, SQL | ~450, ~250, ~250 | ~1,200, ~735 | ≈ −985 |
| **Total** | **≈ 1,850** | **≈ 5,200** | **≈ −3,350** |

**Ruling-dependent:** R3 (b) keeps Watchdog.tsx (730); R1 (b) and R5 (b) keep a picker and email stack nothing can call (≈ 700, dead code); R10 (b) keeps one admin route; R2 (c) keeps `message`, the emitter and the System tab. **Env vars:** +VAPID_PRIVATE_KEY; −RESEND_API_KEY, EMAIL_FROM, RESEND_WEBHOOK_SECRET, NOTIFICATION_UNSUB_SECRET, TELEGRAM_BOT_TOKEN, OUTBOX_DRAIN_DISABLED.

## 5. Waves

Migration numbers are claimed at merge (648 is #1938's). Additive ones run through `apply_migration.yml` before their PR merges; W6's only after every reader is gone. Each PR carries its roadmap bookkeeping; CLAUDE.md edits replace text in place (299/300).

**W1: subtract the dead and the dangerous (ONE PR, #1944, with the four parts as commits: merge slots are one per window, so the plan's four PRs became four commits; this program PR #1942 merges first, or W1 carries its two files).**
- **W1a (R10):** delete Estimate rent (route, kickoff, join, column) and "Run matcher now"; revoke the browser's UPDATE on `estimation_run_id`.
- **W1b (R1, R5):** delete the picker, Settings › Delivery, Telegram, outbox, ChannelClient, transports, webhook, unsubscribe; producers stop writing channel, status, target_channels, sreality_id; the route contract loses the outbox emitters, the route-coverage allowlist /webhooks/resend and /u/{token}.
- **W1c (R3):** /watchdog lists watchdogs; Notifications gains "Load more"; rows link via `dispatchTarget`; one dismissal form.
- **W1d (R2, R9):** §3.8.
- **Docs:** CLAUDE.md 157-158, 188-191, 285, 288; toolkit-api; scraper-ops SKILL.md:427-428 and pipeline-verification.md; tenancy.md:63-66; architecture.md:2672-2680; roadmap item 11. Superseded rulings are marked superseded, not erased.
- **Gate:** CI green; `git grep -i telegram` = 0 in live code and config (CLAUDE.md and the skills may name the retired channel once, as history, so a future session does not rebuild it); Railway API + vite success; read-only, a non-sreality row opens `/property/:id`; the canary's 7-day red share ≤ today's 14/24, only on rung hours.
- **You:** R1–R3, R5, R9, R10; acknowledge the open alarms; green-light the merge window.

**W2: one clock in the worker, in two steps (new fields beside old).**
- **W2a:** the lane runs beside the API matcher; both write a byte-identical `dedupe_key` (pinned), so the 48 h overlap is a union, never a duplicate. Producers add `event_ref` and actor (target-less `ON CONFLICT DO NOTHING`); `shares.grant` writes the new kinds, with SPA labels. **Migration A:** the five columns, `active_since`'s trigger, the wider `change_kind`, backfills. **Gate (48 h):** lane cells green; a read-only replay of the 9 presets as watchdogs (7 days, 5-min buckets) shows 'new' ≥ the cursor's; EXPLAIN ANALYZE < 2 s; false-new baseline recomputed.
- **W2b:** delete the API matcher, lease, gates, readers, wrappers; rename the flag; add the missing-lane check. **Migration B:** `health_liveness()` without "api". **Gate:** Railway API + worker success, no matcher log lines; over 7 days inactive and reactivated ~60% down (7 of 11 reactivations came within 26 h), new ≥ replay.
- **Docs:** CLAUDE.md:34-35; scraper-ops description, body; toolkit-api:434, :463; architecture.md:2242-2285. **You:** R6.

**W3: one identity (switch over).**
- **Scope:** producers switch to `ON CONFLICT (identity) WHERE duplicate_of IS NULL` and stop writing `dedupe_key`, `trigger_snapshot_id`; one-statement carrier; VISIBLE everywhere; delete becomes retire; split_part goes; under R2 (c) the emitter moves too.
- **Migration C (after W2b deploys; non-destructive):** fill `event_ref` since A; mark exact repeats `duplicate_of` (earliest kept); the feed index; `dedupe_key DROP NOT NULL`; last, `CREATE UNIQUE INDEX CONCURRENTLY` with a `$post$` validity check (if a repeat slipped in: drop, re-mark, re-run).
- **Gate:** CI green; a merged 'new' row is marked, never re-fires, 0 deleted; a retired watchdog's alerts hidden, rows kept; 0 identity violations.
- **Docs:** CLAUDE.md rule 16 (key grammar, "collapsing a twin"); architecture.md:2179-2199; tenancy.md:161-164.

**W4: push plumbing, dark.**
- **Scope:** push.py, lane, renderer, routes, share cap; sw.js + vitest; `/n/:dispatchId` pinned in the route contract with its emitter; the Health check; `_NEVER_ACTS`, share-route rail, `_ADMIN_ONLY_RELATIONS`, tenancy.md shape; rule 16 and ruling 5(e) for shares. **Migration D (additive):** the two tables, no seeds.
- **Gate:** RFC vector, TTL floor, sanitised-error tests; CI-Postgres: concurrent claims never share a row, a killed claim returns after its lease, a 429 holds the device; lane "unconfigured", then "no devices"; `/push/key` 503 then 200; sw.js served ≥ 24 h before W5.
- **Docs:** architecture.md § service worker; scraper-ops (lane, check); toolkit-api env table.
- **You:** R7, R11. You run a one-line generator I give you that writes the key to a file only you can read and prints nothing, so it never enters this session; then Railway → Shared Variables → New → `VAPID_PRIVATE_KEY` → paste → attach to API and realtime-worker → delete the file.

**W5: phone surfaces and on-device proof.**
- **Scope:** bell, Shell observer, badge; switch, usePush, identity binding; phone-width feed; PhoneShell's "no bell" comment and test; GLOSSARY (Notification, Push device).
- **Gate (your hands):** (1) install, switch on; (2) a share from a second account arrives in ~30 s, a second that day only in the bell; (3) the tap opens the collection, marks it read, drops the badge; (4) switch off or sign out: no push; sign in: switch off; (5) delete the icon: the next send says "gone"; (6) tell me the iOS version. Read-only SELECTs check the ledger.
- **You:** R4, your iPhone, a second account.

**W6: prune (the one destructive window).**
- **Order:** merge the last reader removals; confirm all three Railway statuses; take and read back the `backup_before_drop.yml` backup; apply Migration E, whose pre-flight refuses while a view, function or policy names a dropped object.
- **Drops, by name:** the 4 tables of §3.2; 12 columns (the 9 dispatch columns of §3.2, `message` surviving under R2 c; `channels`; `last_matched_first_seen_at`; `notify_channels`); 9 settings (notification_email_to, notification_telegram_chat_id, notifications_match_window_listings, notifications_matcher_interval_seconds, notifications_monitor_interval_seconds, notifications_monitor_window_days, notifications_outbox_interval_seconds, notifications_watermark_first_seen_at, system_health_channels); the unread, both dispatched_at and (after EXPLAIN) source_kind indexes; `emit_verification_stale_alert()`; narrowed CHECKs; under R2 (b) only, the 902 system rows. Code inventory: `R2_CARRIERS` (listing_identity.py:67-69, its backfill script and test), `_property_ledger._NOTIFICATION_BACKED`.
- **Gate:** SELECTs match the design; drift check green; feed and bell load; gauge review recorded. **You:** R8, an explicit "yes, apply it", the backup confirmed.

## 6. Rulings needed now

Decided by your brief: shares are pushed; push is for the iPhone, an app rule (the switch shows only there). The server accepts any Apple endpoint, macOS Safari included; Android would need one more host.

1. **R1. Replace the Email/Telegram picker (watchdogs, collections) and Settings › Delivery with one switch per iPhone?** (a) Yes. (b) No; it would then control nothing (dead code). **Recommended: (a).** Nobody ticked it; switched on, it mails other users' alerts to you. **Ruled 2026-10-21: (a).**
2. **R2. Should system/health alerts leave the bell and live only on Health, with the GitHub email as the alarm?** This reverses your 2026-10-18 "bell only" ruling. (a) Yes; the 902 old rows stay as history the app never shows. (b) Yes, and delete them after a backup (amending rule 16's "append-only"). (c) No; they stay in a System tab, never pushed or counted (one special case). **Recommended: (a).** None was read; they bury your 8 real alerts. **Ruled 2026-10-21: (b).**
3. **R3. Should /watchdog drop its second alert list (with the MF-yield and portal columns) and just list your watchdogs, all alerts living in Notifications with "Load more"?** (a) Yes. (b) No. **Recommended: (a).** It copies the feed. **Ruled 2026-10-21: (a).**
4. **R4. On the phone: a bell with the count, that number on the app icon, and the switch in the account sheet?** (a) All three. (b) Bell and switch. (c) Switch only. **Recommended: (a).** Without a bell a push leads nowhere else. **Ruled 2026-10-21: (a).**
5. **R5. Delete the unused email code now, rebuilding email to each account's own address when you switch it on?** (a) Delete; git keeps it. (b) Keep it parked, uncallable. **Recommended: (a).** It sent nothing, and its one shared address is the leak. **Ruled 2026-10-21: (a).**
6. **R6. Timing?** (a) Every kind checked every 5 min; "Staženo z nabídky" after 24 h gone; a new, re-enabled **or edited** watchdog alerts only on what appears afterwards (*edited* added 2026-10-21 from the W2a review: a widened filter is a new start, so `active_since` restamps on every `filter_spec` change). (b) As (a), but a new watchdog first shows the last 24 h. (c) Today's behaviour. **Recommended: (a).** Daily is too slow, blinks fake "Delisted", resumes flood. **Ruled 2026-10-21: (a).**
7. **R7. Ship push now on today's railway.app address?** (a) Now; after the domain move each user re-adds the app and taps the switch once. (b) Wait. **Recommended: (a).** The move forces a reinstall anyway. **Ruled 2026-10-21: (a).**
8. **R8. Approve one destructive cleanup window in W6, backup first?** (a) Approve. (b) Leave the dead columns. **Recommended: (a).** All of it is empty, constant or unread. **Ruled 2026-10-21: (a).**
9. **R9. Should the GitHub alarm e-mail keep reminding at 6 h, 24 h, 72 h, then weekly, or hourly until you acknowledge?** (a) As today. (b) Hourly. **Recommended: (a).** Hourly would have mailed you on each of the last 24 runs. **Ruled 2026-10-21: (a).**
10. **R10. "Estimate rent" and "Run matcher now":** (a) delete both; (b) keep "Run matcher now" for admins. **Recommended: (a).** Both are security holes with 0 uses. **Ruled 2026-10-21: (a).**
11. **R11. Can one person ping your phone with shares at most once a day (the rest still reach the bell), and should the bell drop their email once a share ends?** (a) Yes. (b) No limit; keep the email. **Recommended: (a).** Signup is open; anyone could otherwise flood a stranger's lock screen. **Ruled 2026-10-21: (a).**

## 7. Risks, and what we will NOT do

**Risks.**
- **Three-strike rule:** always-show handler, harness, declarative payloads, a 24 h head start.
- **The domain move orphans subscriptions:** the cutover runbook ends all devices; users re-add the app.
- **Watchdog push is unexercised** (0 watchdogs): W2a's replay sizes it.
- **The worker is the only alert host:** heartbeat restarts, stall, recall, the missing-lane cell (an older process beating beside a new one) and the API's teller cover it; a lone worker left on old code shows only in Railway's per-service commit status.
- **W2b → W3:** until Migration C, an identity clash fails a step loudly, never tells twice.
- **VAPID rotation kills every subscription:** rotate only on a leak. **Rebases:** W2 rebases onto draft #1937. **iOS bug 263687:** fallback taps may open the start page.

**Where a critic's fix is not taken:**
- **The API keeps the private key, not only the public one.** Two separately set keys can disagree, and VapidPkHashMismatch would then end every device at once; an attacker on the API already holds the service-role database.
- **`event_ref` stays text:** the pipeline mode and 902 historical keys are not numbers; price casts once, in gauge SQL.
- **`message` becomes a typed `actor_account_id` rather than a bare drop:** the cap needs the sender, and a pipeline owner is stored nowhere else (columns net 0).
- **The policy drop does not remove R2 (c)'s special case:** under (c) the rows must stay readable in a System tab.
- **`collection_idx` stays:** this revision keeps the collection FK.

**We will NOT:** build a native app, Android or desktop push, or another channel; keep per-watchdog channels, preferences or cadence settings; add a matcher dedup (rule 15); keep per-attempt history; ship a VAPID key in the frontend; push system alerts or a backlog; add a dependency; drive any production write ourselves.

## 8. Audit findings: resolved and deferred

**Resolved:** F1; F2, F10 (leased ledger); F3, F7 (routing by account); F4; F5 (R2, R9); F6 (one identity); F8 (R1); F9 (R4); F11 (device origin); F12; F14 (dwell); F15, F23, F26 (new ledger); F16; F18 (identity-bound unsubscribe); F24, F28 (`/n/{id}`); F25 (R3); F27.

**Partly resolved:** F13 (measured; holding 'new' pushes for autodedup needs 30 days of gauges and a ruling); F17 (no stored sender text, a cap; the 422 email oracle goes to roadmap item 11); F19 (the bell's live price stays, a UX change); F20 (new_source deferred); F21 (a deleted collection still takes its alerts; retiring collections touches every curation carrier); F22 (precision measured).

**Deferred:** server-side agenda enforcement; pipeline-share taps on the shared board; presets vs watchdogs; one label map; collection retirement.