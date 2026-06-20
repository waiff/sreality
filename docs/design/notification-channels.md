# Notification channels — pluggable delivery layer (Sprint N)

> **Status: DESIGN PROPOSAL (2026-06-20), NOT YET BUILT.** The delivery half
> of the unified-notifications work. Consumes the shared event model defined in
> [`notifications-unified.md`](./notifications-unified.md) (read that first for
> the producers, the in-app feed, and the cross-sprint decisions). This doc
> covers only the **delivery layer**: transports, the audited send ledger, the
> outbox runtime, the channel picks, and unification with broker-outreach email.

## 0. The finding that drives the design

Today "in_app" is not a delivery channel — it's the absence of one. The matcher
in `api/notifications.py` `INSERT`s a `notification_dispatches` row with
`status='sent', channel='in_app'` and the feed reads it. There is **no outbound
send anywhere**; `status` is born `'sent'`, and `failed`/`error_message` are
never written by any code path.

Migration 057's comment, ROADMAP, and CLAUDE.md rule #16 all assert email is "a
one-line ALTER, not a rewrite." **That is false** (verified against the
migrations). Migration 096 set the dedup key to `UNIQUE(subscription_id,
property_id, change_kind)` with **channel deliberately omitted**. Widening the
CHECK to `('in_app','email')` and having the matcher `INSERT` a second row for
email hits `ON CONFLICT … DO NOTHING` and **silently drops the email for every
match already recorded in-app**. The CHECK grows trivially; the *grain* cannot
absorb a second channel. So delivery gets its own ledger — it is not bolted onto
the event row. (The unified `notifications` table — shared contract — replaces
`notification_dispatches`; this doc treats it as the upstream event source.)

The house precedent we mirror: `estimation_runs` (the event) vs `llm_calls` (the
audited per-call ledger); `api/providers/` (a Protocol + `_build_providers()` DI
registry) wrapped by `LLMClient` (the audit orchestrator). A pluggable, audited
external integration is exactly this shape.

## 1. The send ledger — `channel_sends`

One append-only row per send **attempt** — the `llm_calls` of delivery. The only
thing that knows about channels, attempts, status, cost, and errors.

```sql
create table channel_sends (
  id            bigserial primary key,
  created_at    timestamptz not null default now(),   -- = queued_at

  consumer      text not null check (consumer in ('watchdog','collection_monitor','outreach')),
  -- exactly one origin FK (notifications.id is UUID; outreach_messages.id is bigint)
  notification_id      uuid   references notifications(id)        on delete set null,
  outreach_message_id  bigint references outreach_messages(id)    on delete set null,
  -- denormalized for cheap telemetry + to survive an event/source delete:
  source_kind   text,                  -- 'watchdog' | 'collection_monitor' | 'outreach'
  source_id     text,                  -- subscription_id / collection_id / campaign id

  channel       text not null check (channel in ('email','telegram')),  -- widen by ALTER
  recipient     text not null,
  category      text not null default 'transactional'
                  check (category in ('transactional','commercial')),

  transport     text,                  -- vendor: 'resend' (transactional email) | 'telegram' | <eu vendor> (outreach)
  provider_message_id text,
  status        text not null default 'queued'
                  check (status in ('queued','sent','failed')),  -- +delivered/bounced in the webhook PR
  error_message text,
  attempts      int  not null default 0,
  next_attempt_at timestamptz,         -- backoff; non-optional for a clean retry story
  cost_usd      numeric(10,6),         -- NULL = unknown, 0 = known-free (document the convention)
  duration_ms   int,
  sent_at       timestamptz,

  dedupe_key    text not null unique,  -- 'notif:{notification_uuid}:{channel}'
  check ( (consumer in ('watchdog','collection_monitor')
             and notification_id is not null and outreach_message_id is null)
       or (consumer='outreach'
             and outreach_message_id is not null and notification_id is null) )
);

create index channel_sends_created_idx  on channel_sends (created_at desc);
create index channel_sends_retry_idx    on channel_sends (status, next_attempt_at)
  where status in ('queued','failed');
create index channel_sends_notif_idx    on channel_sends (notification_id);
create index channel_sends_source_idx   on channel_sends (source_kind, source_id, created_at desc);
```

**Two orthogonal idempotency keys.** Detection (`dedupe_key` on `notifications`,
upstream) and delivery (`dedupe_key` on `channel_sends`, per event+channel).
Adding a channel can never re-fire an event or suppress another channel's send.

`status` stays a 3-value enum until the feedback webhook (PR 5) adds
`delivered`/`bounced` — so the enum never claims observability it doesn't have.

## 2. Transport abstraction — mirror `api/providers/` + `LLMClient`

```
api/transports/base.py         # ChannelTransport Protocol + RenderedMessage/SendResult dataclasses + TransportError
api/transports/email_resend.py # Resend: requests-only, single api-key header (transactional / self-notification)
api/transports/telegram.py     # Telegram Bot API: requests-only, one POST to sendMessage
api/channel_client.py          # ChannelClient: audited send orchestrator (the LLMClient analog)
```

> Email is a *channel* with a pluggable *vendor*. Resend serves the
> transactional (self-notification) stream; the broker-outreach (commercial)
> stream — deferred — will add a second EU-hosted vendor impl behind the same
> Protocol (Resend's AUP forbids cold outreach + stores account data in the US;
> see §6/§9). `channel_sends.transport` records which vendor served each send, so
> mixing vendors per stream is the abstraction working, not patchwork.

```python
class ChannelTransport(Protocol):
    name: str          # 'email' | 'telegram'
    transport: str     # vendor: 'brevo' | 'telegram'
    def is_configured(self) -> bool: ...
    def send(self, *, recipient: str, message: RenderedMessage) -> SendResult: ...
```

- Concrete impls read their own secret from env in `__init__`, **raise only
  inside `send()`** (the providers' "missing key fails at request, not boot"
  posture). `is_configured()` gates the outbox so an unconfigured deploy degrades
  to no-send, never a crashed lifespan task (`image_storage.is_configured()`
  precedent).
- `requests`-only (Brevo and Telegram are each a single JSON `POST`) — **no new
  dependency**. Reuse `scraper/geocoding`'s retry/backoff and `api/maps`'s
  degrade-don't-500 posture.
- Registered in `dependencies._build_transports()` as a `{name: transport}`
  singleton (the `_build_providers()` mirror); `get_channel_client(conn)` mirrors
  `get_llm_client(conn)`.

`ChannelClient.send(...)` claims via `INSERT … ON CONFLICT (dedupe_key) DO
NOTHING RETURNING id` (restart-safe, double-send-proof, no advisory locks), calls
the transport, and `UPDATE`s the ledger with status/cost/duration/error — exactly
how `LLMClient` writes `llm_calls`. A later `record_status_update(provider_message_id,
status)` lands webhook bounce/delivered feedback (PR 5).

## 3. Delivery runtime — a separate, source-agnostic outbox loop

A **second FastAPI lifespan asyncio task** (gated by its own
`OUTBOX_DRAIN_DISABLED` flag, mirroring `NOTIFICATIONS_MATCHER_DISABLED`), doing
network sends via `asyncio.to_thread` so a flaky provider can never block
matching. **Not** inline in the matcher (would block all matching); **not** a
GitHub Actions cron (would duplicate secrets and add latency to a speed-sensitive
feature).

The matcher(s) are **unchanged** — the outbox *derives* work by joining the
event table against the ledger (this is the correctness fix: the matchers do a
set-based `INSERT … SELECT … ON CONFLICT` with no per-row `RETURNING`, so they
cannot hand ids to an enqueuer):

```sql
select n.id, ch
from notifications n
cross join lateral unnest(n.target_channels) ch
left join channel_sends cs on cs.dedupe_key = 'notif:'||n.id||':'||ch
where cs.id is null
  and n.dispatched_at > now() - interval '7 days'      -- bound the scan
order by n.dispatched_at
limit :n;
```

Each is claimed, sent, recorded. Failed rows retry where `next_attempt_at <=
now() AND attempts < MAX` with exponential backoff; give up after MAX (the
`listing_fetch_failures.given_up` posture). Each task opens its **own** autocommit
connection (never share a psycopg conn across two asyncio tasks). The abuse cap is
**DB-derived** (`count(*)` over `channel_sends`, like `LLMClient._check_daily_cost`),
so it survives Railway redeploys. No send endpoint is exposed to the browser (the
`VITE_API_TOKEN`-in-bundle reality makes that a spam/reputation risk).

Watchdog and collection-monitor events drain through the identical path — the
outbox is fully source-agnostic.

## 4. Recipient & config — minimal, no recipient tables

Notifications go to the **operator**, always. Destination endpoints live in
`app_settings` (history-tracked via the migration-020 trigger):

- `notification_email_to`, `notification_telegram_chat_id`

Transport secrets live in Railway env, referenced by name (never `VITE_`):

- `RESEND_API_KEY`, `EMAIL_FROM`, telegram `BOT_TOKEN`, and a genuinely-missing
  `SPA_BASE_URL` (no SPA-origin var exists today).

**Which** channels an event uses is decided per-source (a watchdog
subscription's `channels`, a collection's `notify_channels`) and folded into
`notifications.target_channels` by the producer. So the delivery layer needs no
recipient/routing tables. A real second recipient (e.g. a client) would be an
additive migration *then*, and the `ChannelTransport` Protocol makes recipient
resolution a leaf change — deferring costs nothing. This is "extend via ALTER,
not a rewrite" applied honestly; a normalized recipient/consent model now would
be multi-tenant scaffolding the roadmap doesn't justify.

## 5. Message rendering

`compose_notification_message(notification_row) -> RenderedMessage`, next to the
feed read. Content comes from columns already joined (`properties_public` +
`estimation_runs` + the provenance cols on `notifications`): price/area/
disposition/locality, MF gross yield, the estimate, and `trigger_price_czk` /
`prev_price_czk` for change events. One extra query for a cover photo. Deep-link
to `{SPA_BASE_URL}/listing/{sreality_id}` (let the detail page do any external
portal hop — avoids porting `portalListingUrl` JS into Python). Templates live in
`app_settings` (`notification_email_subject_template`, `…_body_template`,
`notification_telegram_template`) — operator-tunable like the LLM prompts, no
deploy to reword an alert. Compose from `properties_public` (not raw `listings`)
so the alert text matches Browse. Degrade gracefully (`locality → district → 'id
{sreality_id}'`; MF/estimate blocks optional).

## 6. Unification with broker-outreach email — one transport, two consumers

`api/notifications.py` (watchdog + collection_monitor) and `api/outreach.py`
(broker outreach, on `origin/main`, "sends" via `mailto:` today with a planned
"real email provider") both route real email through the **same** transport
*Protocol* + the **same** `channel_sends` ledger + the **same** outbox runtime.
What stays **per-consumer** is the real asymmetry — **consent/suppression** — and,
because of vendor policy, **the concrete email vendor**:

| Shared (the layer) | Per-consumer (never centralized) |
|---|---|
| `ChannelTransport` Protocol + `ChannelClient` | consent / suppression / targeting |
| `channel_sends` audit ledger + outbox loop | outreach's `broker_outreach_suppression` (broker-keyed GDPR) |
| retry/backoff, cost capture, key reading | message composition (different voice) |
| `RenderedMessage` neutral type | `category` (`commercial` for outreach) |
|  | **email vendor**: `resend` (transactional) vs an EU-hosted vendor (commercial) |

The transport must **never** apply a single suppression check — doing so would
either wrongly gate operator self-mail or wrongly bypass broker GDPR suppression.
**Vendor split (intentional, not a compromise):** Resend's AUP forbids cold
outreach and its account data is US-resident, so it serves the *transactional*
stream only; the deferred outreach stream adds an EU-hosted, outreach-permissible
vendor as a second `ChannelTransport` impl. The Protocol, ledger, outbox, and
audit are shared; only the leaf vendor impl differs, and `channel_sends.transport`
records which served each send. Outreach wiring (when built):
`outreach.send_message` → `ChannelClient.send('email', …, consumer='outreach',
category='commercial')`; stamps `sent_via='email'` + `provider_message_id`;
activates RFC 8058 `List-Unsubscribe` + the suppression pre-send gate for
`commercial`. `mailto` stays a fallback. Note: `outreach_messages.status` reserves
`bounced`/`replied` but not `delivered` — an additive CHECK ALTER if
delivered-feedback is wanted there.

## 7. Data quality / observability

`channel_sends` is the telemetry surface (append-only, like `llm_calls`), with
`source_kind`/`source_id`/`category` denormalized for cheap `GROUP BY`:

- delivery rate, match→sent latency (`sent_at − notifications.dispatched_at`),
  per-channel failure, per-source notification volume ("noisiest watchdog/
  collection"), per-day spend, transactional-vs-commercial deliverability.
- Surfaces on the Health dashboard next to `detail_queue_lag` / `property_attach_lag`.

## 8. Compliance — minimal now, primitives ready

Self-notification (operator → own address) is unregulated (no ePrivacy / Czech
Act 480/2004 §7; SPF/DKIM/DMARC are mailbox-provider policy, not law). PR 1
carries only the cheap `category` discriminator. The consent / suppression /
`List-Unsubscribe` machinery lights up with the **commercial** outreach stream
(itself blocked on a Czech-qualified review, human-in-the-loop) — wired in the
outreach PR, not the foundation.

## 9. Channel picks (decided 2026-06-20)

- **Email → Resend.** Cleanest transactional API for the self-notification
  stream, fits `requests`-only (single api-key JSON POST, no SDK), free tier
  3,000/mo + 100/day (far beyond a personal alert feed), strong transactional
  deliverability. **Scope: transactional / self-notification only.** Resend's
  Acceptable Use Policy forbids cold/unsolicited outreach, and although it can
  *send* from an EU region it **stores account data/logs in the US** — both
  disqualifying for broker-outreach (third-party PII + cold B2B), neither an
  issue for emailing the operator. So the **outreach (commercial) email vendor is
  deferred and will be a separate EU-hosted, outreach-permissible provider**
  (e.g. Brevo / a dedicated outreach ESP) behind the same `ChannelTransport`
  Protocol (§6). **Fallback for the transactional stream: Postmark** (best inbox
  placement, same requests-only ergonomics).
- **Mobile-native → Telegram Bot API.** Free, one `requests.post`, native push,
  no number provisioning / template approval, near-zero maintenance. Setup =
  one DM to the bot to capture `chat_id`. Runner-up: Pushover (~$5 one-time).
- **WhatsApp → NO-GO (defer).** Blocker is standing platform machinery: a
  dedicated number (not your personal WhatsApp), a pre-approved template for
  *every* business-initiated message (the free 24h window never opens in a
  one-way feed), re-approval on wording edits, silent utility→marketing
  recategorization (4× cost), per-template quality ratings that can pause sends.
  Too much to self-ping. Slots in later as `api/transports/whatsapp.py` + a CHECK
  ALTER **if/when** alerting third-party clients (WhatsApp dominates CZ).

## 10. Rollout (Sprint N)

All branches off `origin/main` (carries the outreach CRM).

- **PR 1 — Foundation, ships dark.** `channel_sends` ledger; transports `base.py`
  + `channel_client.py` + DI; `compose_notification_message`; outbox lifespan
  loop (no transports registered → no-op); `SPA_BASE_URL`. Doc fix: correct the
  false "one-line ALTER" claim in **CLAUDE.md rule #16 + ROADMAP** (not by
  editing migration 057 — migrations are append-only; the correction rides in
  the new migration's comment). Tests for ledger idempotency + composer.
- **PR 2 — Email (Resend) live.** Transport + Railway env (`RESEND_API_KEY`,
  `EMAIL_FROM`) + SPF/DKIM/DMARC DNS (10-min operator step) + email templates +
  UI (Delivery section in watchdog/collection config, delivery-status column on
  the feed, Settings → Delivery).
- **PR 3 — Telegram.** One file + one registry line + CHECK ALTER + `chat_id`
  capture. Proves the abstraction.
- **PR 4 — Outreach unification.** §6.
- **PR 5 (optional) — Delivery feedback webhook.** Brevo bounce/delivered →
  `record_status_update` → flips `channel_sends` (+ `outreach_messages`). Scope
  it or explicitly defer; don't leave `delivered`/`bounced` as dead enum values.

Depends on the joint **PR A** ([`notifications-unified.md` §7](./notifications-unified.md))
landing first — the outbox reads `notifications.target_channels`.
