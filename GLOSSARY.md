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
The one ad whose facts a property shows: price, area, description, photos and link. Chosen by a fixed
order, never by hand.
_Avoid_: representative ad, repr, speaking ad, main ad, headline ad

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

### Changing what belongs together

**Merge**:
Joining two or more properties into one, so that all their ads form one property.
_Avoid_: dedup (for the act), link, join, group (verb), unify, absorb

**Split**:
Taking one or more ads out of a property, into another property or into a new one.
_Avoid_: unmerge, detach, undo, separate

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

### What users add

**Curation**:
Everything a user adds to a property by hand: notes, a pipeline card, collection membership, tags,
a dismissal. Curation belongs to the user's account and follows the property.
_Avoid_: operator state, user-defined information, user-created state, carried state

**Pipeline card**:
A property's place in one account's deal pipeline. At most one per property per account.
_Avoid_: deal, card (alone)

**Collection**:
A named set of properties an account keeps, optionally watched for changes.
_Avoid_: list, folder, favourites, saved list

**Dismissal**:
An account's decision to hide a property from its own Browse.
_Avoid_: hide, mute, ignore, archive

**Watchdog**:
A saved filter that alerts an account when a property newly matches it or drops its price.
_Avoid_: alert (for the saved filter), subscription, notification
