# Unified notifications — event model + delivery channels (shared contract)

> **Status: DESIGN PROPOSAL (2026-06-20), NOT YET BUILT. Two decisions
> resolved, two pending operator ratification (§6).** This document is the
> **shared contract** between two parallel sprints that meet at one place — the
> notification *event model*:
>
> - **Sprint C (collections + in-app notifications):** ungrey collections,
>   a default "monitoring" collection, add-to-collection on Browse card /
>   Listing Detail / Chrome extension, a collection-monitor change detector,
>   the unified in-app **Notifications** area + unread red badge, per-collection
>   monitoring on/off + channel selection.
> - **Sprint N (notification channels):** the pluggable delivery layer —
>   email (Brevo) + a mobile-native channel (Telegram), an audited
>   `channel_sends` ledger, the outbox runtime, and unification with the
>   broker-outreach email path. Detailed in
>   [`notification-channels.md`](./notification-channels.md).
>
> Neither sprint should build the `notifications` table unilaterally. **PR A
> (§7) — the unified `notifications` migration — is joint and ships first.**
> No schema or code has landed.

## Context

Today the "Watchdog" is the only notification producer. `api/notifications.py`'s
matcher writes one `notification_dispatches` row per `(subscription, property,
change_kind)` match with `status='sent', channel='in_app'`, and the feed UI
reads it. There is **no real send** anywhere — "in_app delivery" = the row
existing. (See [`notification-channels.md` §0](./notification-channels.md) for
the full conflation analysis and why "email is a one-line ALTER" — asserted in
migration 057's comment and CLAUDE.md rule #16 — is **false**.)

Two sprints now expand this at once:

1. **A second producer** — collection-monitoring — fires when a listing the
   operator put in a monitored collection changes (price up/down, or goes
   inactive).
2. **One unified in-app feed** showing *both* watchdog matches and monitoring
   events, with an unread count badge.
3. **The same events fan out to new channels** (email / Telegram).

The risk if uncoordinated: two notification tables, two delivery paths, two
"what counts as a change" definitions — the exact patchwork both sprints are
chartered to avoid. This document pins the interface so each sprint builds
independently against a stable contract.

## Architecture: producers → one event model → feed + delivery

```
PRODUCERS  (detect events)                                   ── Sprint C owns the new one
  • Watchdog matcher        → 'new', 'price_drop'            (exists today)
  • Collection monitor      → 'price_drop','price_rise','inactive'   (NEW, Sprint C)
        │
        ▼
NOTIFICATION EVENT MODEL — unified `notifications` table      ── SHARED CONTRACT (this doc, §3)
  one row per (source, subject, change); seen_at (badge);
  provenance (trigger price / prev / snapshot_id);
  target_channels[]  ← producer stamps which channels to deliver on
        │
        ├──────────► IN-APP FEED + unread red badge           ── Sprint C owns
        │
        ▼
DELIVERY LAYER — channel_sends ledger + transports + outbox   ── Sprint N owns (channels doc)
  source-agnostic: drains `notifications × unnest(target_channels)`;
  ALSO serves broker-outreach (shares only the transport, not the event model)
        │
        ▼
CHANNELS: in_app (implicit) · email (Brevo) · telegram · …WhatsApp later
```

**The decoupling primitive is `notifications.target_channels`.** Producers
decide which channels an event goes to; the delivery layer just delivers. The
outbox never joins to watchdog subscriptions or collection settings — it reads
one array column. This is what lets the two sprints stay independent.

## 1. Ownership boundary (who builds what)

| Concern | Owner | Notes |
|---|---|---|
| Collections: ungrey, default "monitoring" collection, add-to-collection (card / detail / extension) | **Sprint C** | writes via the bearer-gated API (rule #18); the extension gets a new `POST`, not new infra (it has no collection write today) |
| Collection-monitor change detector (writes `notifications`) | **Sprint C** | reuse the watchdog's snapshot-diff primitives — do **not** reimplement "what is a price change" (§5) |
| Unified in-app Notifications area + unread badge | **Sprint C** | one query over `notifications`; badge = `count(*) where seen_at is null` |
| Per-collection `monitoring_enabled` + `notify_channels[]` | **Sprint C** | the collection's channel choice; folded into `notifications.target_channels` at event creation |
| The unified `notifications` table (the contract, §3) | **JOINT — PR A** | generalizes today's `notification_dispatches`; both sprints depend on it |
| `channel_sends` ledger + transports + outbox runtime + Brevo/Telegram | **Sprint N** | [`notification-channels.md`](./notification-channels.md) |
| Operator destination endpoints (email, telegram chat_id) | **Sprint N** | `app_settings` keys; recipient is always the operator (no recipient tables) |
| Broker-outreach email unification | **Sprint N** | shares the transport only; consent/suppression stay outreach-owned |

## 2. Glossary

- **Event / notification** — one row in `notifications`: a specific change to a
  specific subject detected by a specific source. The unit of the feed, the
  unread count, and delivery fan-out.
- **Subject** — the listing/property the event is about (`sreality_id` +
  `property_id`).
- **Source** — what produced the event: a watchdog subscription, or a monitored
  collection. (`source_kind` + the matching nullable FK.)
- **Delivery** — one attempt to push an event to one channel: a row in
  `channel_sends`. `in_app` needs no delivery row (the feed reads `notifications`
  directly).

## 3. The shared contract — `notifications`

Generalize the existing `notification_dispatches` (build on what exists) into a
source-agnostic event table. Sketch (final column names settle in PR A review):

```sql
-- generalizes notification_dispatches; one row per (source, subject, change) event
create table notifications (
  id                 uuid primary key default gen_random_uuid(),

  -- SOURCE (exactly one source FK set, matching source_kind)
  source_kind        text not null check (source_kind in ('watchdog','collection_monitor')),
  subscription_id    uuid   references notification_subscriptions(id) on delete cascade,  -- watchdog
  collection_id      bigint references collections(id)               on delete cascade,  -- monitor

  -- SUBJECT (carries both grains; see §4)
  sreality_id        bigint not null,
  property_id        bigint references properties(id) on delete cascade,

  -- EVENT
  change_kind        text not null
                       check (change_kind in ('new','price_drop','price_rise','inactive')),
  -- PROVENANCE (data quality: survives latest-wins erasing the trigger)
  trigger_price_czk  int,
  prev_price_czk     int,
  trigger_snapshot_id bigint,

  -- DELIVERY ROUTING (producer-stamped; the decoupler)
  target_channels    text[] not null default '{}',   -- non-in_app channels for this event

  -- FEED STATE
  dispatched_at      timestamptz not null default now(),
  seen_at            timestamptz,                     -- null = unread (drives the badge)
  estimation_run_id  bigint references estimation_runs(id) on delete set null,

  -- IDEMPOTENCY (single key; see §6 decision 2)
  dedupe_key         text not null unique,

  check ( (source_kind='watchdog'          and subscription_id is not null and collection_id is null)
       or (source_kind='collection_monitor' and collection_id   is not null and subscription_id is null) )
);

create index notifications_feed_idx   on notifications (dispatched_at desc);
create index notifications_unread_idx on notifications (seen_at) where seen_at is null;
create index notifications_subject_idx on notifications (property_id);
create index notifications_source_idx  on notifications (source_kind, subscription_id, collection_id);
```

Contract guarantees the delivery layer (Sprint N) relies on:

- `id` is **uuid** (today's `notification_dispatches.id` already is). `channel_sends`
  FKs it as uuid.
- `target_channels` lists the non-`in_app` channels this event should be
  delivered on. Empty = in-app only. The outbox is driven entirely by this.
- `dedupe_key` is globally unique and deterministic, so re-running any matcher
  over an overlapping window never creates a duplicate event.

## 4. Grain — monitor at the property grain (recommended)

Collections are **listing-grain** (`collection_listings(collection_id,
sreality_id)`, migration 022). The notification/matcher/price model is
**property-grain** (`properties_public`, rules #15/#16). Recommendation: the
collection-monitor resolves each member `sreality_id → listings.property_id` and
watches the **property's** price/active rollup, so:

- a multi-portal duplicate doesn't fire twice,
- "price change" matches what Browse/Stats show,
- it's consistent with the watchdog (which already fires at property grain).

The `notifications` row keeps **both** ids, so the feed can still deep-link the
exact listing the operator added. Edge case: a freshly-added member whose
`property_id` is still NULL (pending the ~5-min attach job) — monitoring engages
once attached.

## 5. Data-quality rules for the collection-monitor producer (Sprint C)

- **Reuse the watchdog's snapshot-diff logic** (`match_changes_once` /
  `_recent_price_drop_property_ids` in `api/notifications.py`) for price events,
  plus an `inactive` detector off `properties.is_active` / `inactive_at`. Do not
  fork a second definition of "a price change" — that's the rule-#16 disease one
  layer down.
- **Stamp provenance at creation** (`trigger_price_czk`, `prev_price_czk`,
  `trigger_snapshot_id`) — the matcher has these in scope; they make the feed
  message and the audit honest after latest-wins overwrites the row.
- **Fold the collection's channel choice into `target_channels`** at event
  creation (from `collections.notify_channels`, gated by `monitoring_enabled`).

## 6. Decisions

Status as of 2026-06-20. Two resolved, two pending.

1. **PENDING — Generalize `notification_dispatches` → `notifications`**
   (recommended) vs keep two tables + a union view. Generalizing gives one feed
   query, one badge, one delivery path. *Coordinated migration: renames/reshapes
   the existing watchdog table and updates its matcher + tests.* Operator
   reviewing a product-level explanation of both options before sign-off.
2. **PENDING — Dedup grain for change events: per-snapshot (recommended) vs
   once-ever.** Today the watchdog fires `price_drop` **once ever** per
   `(sub, property)` via `UNIQUE(subscription_id, property_id, change_kind)`.
   Monitoring needs to fire on **every** change. A single per-event `dedupe_key`
   that includes the snapshot transition for change events handles both and
   *fixes* the watchdog's latent "once ever" limitation:
   - `new`: `wd:{sub}:{property}:new` (once ever — unchanged)
   - change: `cm:{collection}:{property}:price_drop:{snapshot_id}` /
     `wd:{sub}:{property}:price_drop:{snapshot_id}` /
     `cm:{collection}:{property}:inactive:{inactive_at_epoch}`
   *This replaces the existing composite UNIQUE — the one place watchdog behavior
   changes; it needs a test update.* Note: event **identity** (this grain) is
   orthogonal to **materiality** (whether a change is big enough to notify — a
   per-source threshold filter added without touching the grain), so per-snapshot
   identity does not imply higher notification volume.
3. **RESOLVED ✓ — Monitor at the property grain** (§4). Collection members
   (`sreality_id`) resolve to `property_id`; the monitor watches the property's
   price/active rollup.
4. **RESOLVED ✓ — `target_channels[]` stamped by producers.** Producers fold the
   source's channel choice into the event row at creation; the delivery layer
   reads only that column (no join to subscriptions/collections).

## 7. Sequencing

Because the `notifications` table is foundational to *both* the in-app feed and
the delivery layer, it ships first.

- **PR A — foundation, owned by the notification-channels session (Sprint N).**
  The unified `notifications` table (generalize `notification_dispatches`:
  `source_kind`, nullable `subscription_id`, new `collection_id`, `change_kind`
  enum growth, provenance cols, `target_channels`, single `dedupe_key`). Migrate
  the watchdog matcher onto it. **Built once, here** — Sprint C builds against it,
  does not re-create it. Unblocks both sprints.
- **Sprint C PRs** — collections ungrey + default "monitoring" + add-to-collection
  (card/detail/extension); the collection-monitor matcher writing `notifications`;
  the unified Notifications area + unread badge; per-collection
  `monitoring_enabled` / `notify_channels`.
- **Sprint N PRs** — `channel_sends` + transports + outbox (ships dark) → Brevo
  email → Telegram → outreach unification. See
  [`notification-channels.md`](./notification-channels.md). These carry
  collection-monitor events for free the day they read `target_channels`.

## Out of scope (deferred)

- WhatsApp (heavyweight template/number machinery — deferred until third-party
  client alerting; see channels doc §9).
- A mobile **app** — the `target_channels` + `ChannelTransport` model already
  accommodates Web Push / native push as future channels, so it's a leaf
  addition.
- Multi-recipient / per-user identity — single-operator platform; user
  management stays out of scope. Channels select *how* the operator is reached,
  not *who*.
