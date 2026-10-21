# Property intelligence platform

One market-wide view of Czech real estate built from the ads nine portals publish. Code and tables
still carry some older names; the terms below are the ones to use in prose, UI text, documents and
anything newly named.

## Language

### The two primary terms

**Property**:
One real-world unit for sale or rent as the platform knows it: the group of every ad that advertises
that same unit. Every ad belongs to exactly one property.
_Avoid_: card, group, cluster, record, listing group

**Ad**:
One advertisement as one portal publishes it, under that portal's own id. The thing the scrapers
collect and keep history for.
_Avoid_: listing, advert, offer, inzerát (in English prose)

### Around a property

**Singleton**:
A property with exactly one ad.
_Avoid_: standalone, unmerged property

**Canonical ad**:
The one ad whose facts a property shows: price, area, description, photos and link; its location is
the location ad's. Chosen by a fixed order, never by hand.
_Avoid_: representative ad, repr, speaking ad, main ad, headline ad

**Location ad**:
The ad whose resolved location places a property: its map pin and precision, its address label, its
town, quarter, street and KÚ codes, and what is computed from them (MF reference rent, city proximity,
whether it is served). Every property surface reads it: Browse and its map, the property page, the
watchdog, notifications, an estimate's subject and comparables, and the extension's panel. Chosen
by a fixed order, never by hand: an ad with a map point first (then one determined foreign), then
the most precise location whatever the ad's status, ties by the canonical order (active ads first).
Often the canonical ad, not always. A surface about one ad keeps that ad's own location.
_Avoid_: place, pin ad, anchor ad, located ad, representative location

**Duplicate**:
Two ads that advertise the same unit and therefore belong in one property.
_Avoid_: dupe, match, twin

**Re-list**:
A new ad for a unit whose earlier ad ended, on the same portal or another. A re-list is a duplicate.
_Avoid_: repost, relisting, re-listing

**Revival**:
The same ad, same portal id, showing again after it had disappeared.
_Avoid_: re-list (that is a new ad), reactivation

**Active ad / inactive ad**:
An ad is active while its portal still shows it and inactive once it has disappeared. Inactive ads
are kept forever.
_Avoid_: live, dead, delisted, deleted, removed, expired

**Portal**:
One of the advertising websites the platform collects from.
_Avoid_: site, source (in prose)

**Snapshot**:
One kept version of an ad's content. A new snapshot exists only when the content changed.
_Avoid_: version, revision, scrape

**Sweep**:
A day on which the platform first saw a portal category's existing stock all at once, rather than ads appearing one by one: the first walk of that category, or a catch-up after a gap. Not the property sweep, the daily job that recomputes every property's figures.
_Avoid_: backfill day, horizon, go-live day

**Newly listed ad**:
An ad first seen on an ordinary day, not in a sweep, so its time on market counts from the day it appeared. An ad first seen in a sweep was already on the market, and its time on market is a lower bound.
_Avoid_: uncensored ad, fresh ad, left-censored (for the opposite)

**Valuo valuation**:
A price for one property bought from Valuo, an outside valuation service: what was sent, where each
value came from, which Valuo environment answered, and Valuo's answer in its own words. Shown beside
our own estimates, never counted as one of them.
_Avoid_: Valuo estimate, Valuo run, external estimate

**Recalculation** (UI: "Náš přepočet"):
Our figure over the comparables of a Valuo valuation that the user leaves ticked, worked out in the browser from
Valuo's own per-m² prices the way Valuo works out its average. Never stored, never counted as one of our estimates
(a run of ours) and never presented as Valuo's answer.
_Avoid_: our estimate, Valuo recalculation, náš odhad

### Changing what belongs together

**Merge**:
Joining two or more properties into one, so that all their ads form one property.
_Avoid_: dedup (for the act), link, join, group (verb), unify, absorb

**Split**:
Taking one or more ads out of a property, into another property or into a new one.
_Avoid_: unmerge, detach, undo, separate

**Letter**:
The mark the user gives each item of a statement; items with the same letter are one property
afterwards. In a split, every ad of one property gets a letter and one letter keeps the property's
number and page. In Browse's merge mode, each card (a property) the user marks gets a letter and one
click merges every letter into its oldest property; a letter says nothing about the cards it does not
hold, and the letters are forgotten when the mode closes.
_Avoid_: unit, part, side, group, selection, tick

**Survivor**:
The property that keeps its identity: after a merge, the one that remains; after a split, the one
that keeps its number and its page.
_Avoid_: keeper, winner, target

**Ruling**:
A user's recorded verdict that two ads are the same unit or different units. Rulings bind the engine.
_Avoid_: label, verdict, decision (for this), judgement

**Engine**:
The automatic duplicate detector that proposes merges and performs them inside its allowed area.
_Avoid_: dedup, matcher, autodedup (in prose), new dedup

**Removed engine**:
The earlier automatic detector. Its merges remain in the data; its logic was deleted and is never
consulted.
_Avoid_: old engine, legacy engine, legacy dedup

**Apply scope**:
The area, a set of towns and Prague quarters with the deals it covers, inside which the engine may
merge. The operator sets it; outside it the engine may still decide, but merges nothing.
_Avoid_: allowlist, rollout area, trial blocks, shadow (for what lies outside it)

**Generation**:
One complete store of the engine's decisions over an area. The live generation, begun by a seed, is
the one the engine keeps current as ads arrive and change and its realtime lane merges from; a
fresh seed empties it and starts again.
_Avoid_: version, run, batch, snapshot, shadow

**Build**:
The engine deciding, for the first time, every ad of an area it has just been seeded on. While it
builds it merges nothing (your merges go on), so a build pauses its merges; it ends once every
town and quarter of the area has been read and what is left undecided fits in one pass.
_Avoid_: bootstrap (in prose), rebuild, backfill, seeding (that is how a build starts)

**Lease**:
The one lock that lets a single engine pass, seed or batch run work at a time. Each takes it for a
fixed time (40 minutes for a pass, 5 hours for a seed or a batch run) and releases it when done;
one its holder never released blocks the engine until that time runs out.
_Avoid_: lock (alone), mutex, claim, latch

### What users add

**Curation**:
Everything a user adds to a property by hand: notes, a pipeline card, collection membership, tags,
a dismissal. Curation belongs to the user's account and follows the property.
_Avoid_: operator state, user-defined information, user-created state, carried state

**Curated set**:
Tags, collections or pipeline stages read as one kind of filter: each tag, collection or stage is an
element of its set, and the filter asks "in it", "not in it" or "in none of it"; the database judges
membership under the caller's own row-level security, so shared collections count and nothing else does.
_Avoid_: scope (alone), group, unit, bucket

**Pipeline card**:
A property's place in one account's deal pipeline. At most one per property per account. As a
filter: in the pipeline (or in chosen stages), not in chosen stages, or in none of it ("Mimo
pipeline": no pipeline card at any stage, closed ones included).
_Avoid_: deal, card (alone)

**Stage**:
One step of one account's pipeline, drawn as a board column; every account gets its own copy of the default board at signup.
_Avoid_: fáze (in code), column

**Collection**:
A named set of properties an account keeps, optionally watched for changes. It may be shared
read-only with other accounts. As a filter: in any chosen collection, in none of the excluded
ones, or in no collection at all ("V žádné kolekci"); own and shared-with-me count alike.
_Avoid_: list, folder, favourites, saved list

**Share**:
A read-only grant of one collection or one dashboard to one account. The owner revokes it, the
recipient may leave it; the recipient can never change what was shared.
_Avoid_: invite, membership, access (alone), collaborator

**Pipeline share**:
A grant of one account's whole pipeline, and with it that account's curation, to another account:
to view the board only, or to edit the board and the curation. The owner revokes it or changes its mode, the recipient may leave it. Only
the owner shares their own pipeline.
_Avoid_: board share, team board, membership, delegation (alone)

**Saved set**:
A named row an account owns, lists, loads, updates and reorders. Its two kinds are the filter preset
and the dashboard; only a dashboard may be shared, read-only.
_Avoid_: saved item, saved view, favourite

**Dashboard**:
A named set of Stats charts an account saved, optionally with the Browse filter it was built on. Loading
one copies it into the working board; it changes only when its owner saves changes. It may be shared
read-only; a recipient loads a copy and may save it as their own.
_Avoid_: report, view (alone), saved board

**Working board**:
The charts the Stats tab shows right now: one per account, never shared, never listed among dashboards.
_Avoid_: default dashboard, scratch, draft

**Acting as**:
Working on another account's curation as that account, through a pipeline share: while acting, the
pipeline, notes, collections, tags, files, artifact links and dismissals you see and write are the
owner's and none of your own; everything else (estimations, watchdogs, presets, billing,
notifications, shares) stays yours. A view-only share acts for reading the board only.
_Avoid_: impersonate, switch account, log in as, view as; "acting account" for the caller's own
account (merge and split code say so; write "the caller's account")

**Delegated edit**:
One change someone made while acting as another account, recorded with who made it and when; only
the owner sees the record, shown as "edited by" on the note, card or stage until the owner changes it.
_Avoid_: audit log, activity, change history

**Shared note**:
A note its author marked for the people they share its property with: readable by the other party
of any share the author is party to whose collection holds that property, including shares made
after the note was marked. Unmarked notes stay private.
_Avoid_: public note, team note

**Tag**:
A coloured label an account puts on a property; a property carries any number. A tag filter
has two sides: must carry every chosen tag, must carry none of the excluded ones; or carry no
tag at all ("Bez štítků").
_Avoid_: štítek (in code), label, category, flag

**Dismissal**:
An account's decision to hide a property from its own Browse.
_Avoid_: hide, mute, ignore, archive

**Lens**:
A filter over the account's own curation that sits outside a preset's identity: the three curated-set
scopes, the broker scope and the dismissed reveal; it never dirties a preset, and a preset click resets it.
_Avoid_: view, mode, preset filter, overlay

**Fold**:
An item of one account's curation that gives way in a merge: the losing one of two of a kind (two
pipeline cards, collection entries, tags or dismissals), or a dismissal that a live deal lifts. It
is kept in the carry record, never lost.
_Avoid_: absorb, collapse, drop, dedupe

**Carry record**:
What a merge writes down about each item of curation it moved or folded, so a split can send it
back where it came from.
_Avoid_: carry log, curation ledger, snapshot

**Watchdog**:
A saved filter that alerts an account when a property newly matches it or drops its price.
_Avoid_: alert (for the saved filter), subscription, notification

**Broker contact record**:
A broker's email and phone that one account captured from one ad's portal page after showing
interest in that ad. It belongs to that account and stays tied to the ad, not the property.
_Avoid_: lead, contact (alone), broker contact (alone), revealed contact

### AI work

**Assistant**:
A user's own AI chat account, such as Claude or ChatGPT, acting for that user.
_Avoid_: chatbot, bot, AI platform

**Workflow**:
A named piece of AI-assisted work a user starts to get a result, such as an area takeoff, a yield
valuation or a rent estimation.
_Avoid_: job, automation, task

**Skill**:
The instructions an AI follows to carry out a workflow, whether it runs in a user's assistant or
inside the platform.
_Avoid_: prompt, agent profile, recipe

**Run**:
One execution of a workflow, with its inputs and its result. A run usually concerns one property.
_Avoid_: job, execution, session

**Area takeoff**:
The workflow that measures a property's rentable floor areas from its plans.
_Avoid_: offtake, area offtake

### Collecting ads

**Walk**:
One pass through a portal's lists of ads, page by page, to see which ads it shows right now. A walk
reaches the end of a unit when it stops on the portal's own last page rather than on a limit of ours.
_Avoid_: crawl, scan, index run

**Unit (of a walk)**:
One property type and deal that a portal lists, such as flats for sale, as a walk covers it; a walk
may reach the end of one unit and not of another.
_Avoid_: category pair, category (alone); a slice or scope is one part of a unit, never a unit

**Declared total**:
The number of ads a portal itself says a unit holds, read from its own pages during a walk. Where a
portal shows no such number, the count of ads the walk saw stands in for it and is never read as one.
_Avoid_: result size, portal count, expected count

**Effective dwell**:
How long an ad must stay gone from its portal, in practice, before the platform makes it inactive:
24 hours today for an ad a walk nominates, because a walk re-checks no ad within 24 h of its last
check, although the rule asks for 12.
_Avoid_: dwell (alone), grace period, delisting delay

### Health

**Check**:
One fact about the platform's own health, measured on a fixed schedule and judged OK, Watch,
Problem or Not measured. Every check is either an alarm or a gauge.
_Avoid_: test, probe, monitor, health metric

**Cell**:
One verdict inside a check, about one part of what the check watches, such as one portal or one
scheduled job.
_Avoid_: row, item, sub-check

**Alarm**:
A check for something that should not happen, such as a portal that stops finding new ads. Its
Problem is told at once and reminded 6 hours, a day and three days after it began, then weekly,
until it recovers or is acknowledged.
_Avoid_: alert (that is the notice), trigger

**Gauge**:
A check of a standing condition the platform lives with while it is being fixed, such as ads that
still lack a town, shown with its trend and whether it is worse than a week ago. Its Problem is told
once, then reminded once a day until it recovers or is acknowledged.
_Avoid_: metric, indicator, KPI

**Trend**:
A gauge cell's last 30 days, one point per Prague day with that day's value and status, carried
forward from one measurement to the next, so the Health page can show whether a standing condition
is shrinking and whether today's status is worse than a week ago. Alarms have none.
_Avoid_: sparkline (that is the drawing), history, time series

**Incident**:
One unbroken spell of trouble in one cell, from its first Problem until a measured OK has held long
enough to count as recovery.
_Avoid_: outage, issue, ticket, problem (for the record)

**System alert**:
A notice about the platform's own health: an incident has started, is still open, or has recovered.
Since 2026-10-21 it reaches no notification feed (its old feed rows were deleted after a backup): the
Health page shows the incident, and the canary's failure email tells an unacknowledged alarm; a
watchdog's notice about properties is not one.
_Avoid_: alarm (that is the check), alert (alone), health notification

**Acknowledge**:
An admin's deliberate act, on the Health page, of marking a check's open incidents as known, which
stops their reminders and the canary's failure email about them; each still closes only on a measured
recovery.
_Avoid_: dismiss, mute, snooze, resolve, mark read

**Not measured**:
The verdict of a check that has no current measurement: it ran out of time, raised an error, found
nothing to judge, or is overdue. It never counts as OK, and it neither opens an incident nor counts
as its recovery.
_Avoid_: unknown (in prose), skipped, pending, OK

**Watcher**:
A check that judges whether the rest of the health system is alive: the realtime worker's
heartbeat, every alarm measured on time, the API's background loops, every measured Problem given an
incident. It runs on the database's own scheduler, outside the worker and the API it judges; the one
watcher is the liveness check, shown as "Watchers".
_Avoid_: monitor, meta-check, dead-man switch, heartbeat check

**Canary**:
The one monitoring workflow on GitHub, an hourly cron that GitHub delivers about five times a day:
it records the production runs of the other workflows (those run on schedule or by a push to the main branch, each failure
with its reason line), measures the checks hosted on GitHub, then asserts that the database answers, that the watcher is running and its Problems have incidents, and
that no alarm's incident has stood open and unacknowledged for 15 minutes to 12 hours since its onset or
latest reminder. A failing check never fails its run; the assertions do (so does a crash of the
poller or the harness, or the harness step overrunning its 6 minutes), so GitHub's failure email is the one path to
the operator that needs neither the database's scheduler nor Railway.
_Avoid_: monitoring lane, acute lane, health workflow, smoke test

**Health lane**:
The realtime worker's lane that measures the checks hosted on the worker: every minute it runs
those that are due, within an eight-minute (500 s) pass (a gauge that no longer fits waits for the
next minute), and once a day it deletes check results older than 35 days. The watcher judges whether it
is running.
_Avoid_: health worker, check lane, monitor lane

**Job cell**:
One cell of the jobs check, about one scheduled machine: a GitHub workflow, the GitHub poller, a
database cron job, a realtime-worker lane (or one portal inside its probe or drain lane), or a
derived artifact. A failing workflow's cell shows its run's reason line.
_Avoid_: job check, workflow status, task

**Reason line**:
The one log line that explains why a GitHub run failed: its last failing CHECK line, else its last
error that is not the runner's own setup output, else GitHub's last error annotation. It is stored
with the failure and shown in that job's cell.
_Avoid_: signature, error summary, failure reason

### Where users meet the platform

**Extension**:
The platform's Chrome extension, which shows a property's data and actions on portal pages.
_Avoid_: plugin, add-on

**Plugin**:
The package a user installs into their assistant to connect it to the platform: the skills plus the
connection to the platform's tools.
_Avoid_: connector, extension, integration

**Phone app**:
The platform's web app installed on a phone's home screen: the same app in a phone-sized shell.
_Avoid_: PWA (in prose), mobile app, native app, companion app

**Bell**:
The way into a user's notifications, with the count of their unread ones: the Notifications entry
in the desktop top bar, the bell in the phone app's header.
_Avoid_: inbox, notification centre

**App badge**:
The number on the phone app's home-screen icon: the same unread count the bell shows.
_Avoid_: icon badge, push badge

**Push device**:
One phone app that can receive a user's notifications as pushes: its push subscription, bound to
the user signed in on it, ended when that user signs out or another signs in.
_Avoid_: device token, subscriber, push subscription (in prose)

**Switch**:
"Oznámení na tomto iPhonu" in the phone app's account sheet: turns this phone into a push device
for the signed-in user, or stops it being one. There is no other push setting.
_Avoid_: push preference, channel picker, opt-in

**Reference overlay**:
A map layer drawn from an outside public dataset — metro routes, flood zones, kraj borders, the MF
rent map, university sites — that a user switches on or off; it changes what the map shows around properties, never
which properties.
_Avoid_: data layer, overlay data, map data, GIS layer

**Count mark**:
The number a map shows for one world tile when more properties are in view than the pin limit (a
number the user sets above the map, 200 by default): exactly that many, in a pill at the tile's
centre. At or under the limit every property is its own pin, at any zoom. One tile holds one mark;
the marks on a map add up to the mapped total.
_Avoid_: cluster, bubble, cell, bin, group

**Chart (Stats)**:
One saved view on the Browse Stats tab: up to six nested row dimensions, at most one column
dimension and one measure, drawn as a table, lines, bars or boxes over the properties the filter
picks. A user's charts together are their working board; the title is derived, never typed.
_Avoid_: card, widget, report, graph (for the saved thing)

**Sample ledger**:
The line every Stats chart shows first: how many properties the filter holds, how many the chart
stands on, and how many it left out because a chosen dimension or the measure has no value.
_Avoid_: coverage, n, data quality bar

### A property's working files

**Workbook**:
The Google Sheets file that holds a user's working figures for one property.
_Avoid_: spreadsheet, sheet, model

**Tab**:
One sheet inside a workbook, holding one kind of information.
_Avoid_: sheet, worksheet
