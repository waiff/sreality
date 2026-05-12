# Operator workflow track

User-facing features that don't fit the analytical, estimation, UI,
map, or scraper tracks. Operator-scoped (single shared identity, no
per-user accounts — matches today's bearer-token model).

## Done

### Phase U2.6: Collections + tags + notes
Operator watchlists, freeform coloured tags, and per-listing journal
notes — end-to-end.
- Migrations 022 (`collections` + `collection_listings`), 023
  (`listing_notes`), 024 (`tags` + `listing_tags`, palette pinned
  to eight named colours by CHECK), 025 (`*_public` views +
  `listings_with_tags(tag_ids)` RPC with AND-semantics, capped at
  5000 rows).
- API: `api/curation.py` exposes CRUD over `/collections`,
  `/listings/{id}/notes`, and `/tags`; routes wired in `api/main.py`
  around line 612+. All bearer-gated per CLAUDE.md toolkit rule #8.
  Tag colour mirrored in `api/schemas.TagColor` (eight-name Literal).
- Frontend:
  - `/collections` index with inline new-collection form, listing
    counts, soft-delete with confirm.
  - `/collection/:id` detail with rename/description edit, delete,
    and a slim member-listings table reusing the Browse/ListingTable
    visual language (sreality_id link, district / disposition / area
    / price / last seen / status / added_at + remove button).
  - `ListingDetail` gains a `CurationBlock` sitting between
    KeyFactsBlock and TimestampsBlock: every collection rendered as a
    toggle (✓/+ chip), tag chips with an autocomplete picker that
    can create a new tag inline (eight-colour palette), and a
    collapsing notes journal (textarea + chronological list).
  - Browse `Filters.tsx` Curation group exposes a tags facet —
    AND-semantics, delegates to the `listings_with_tags` RPC.
- New tokens: `--color-tag-{copper,sage,brick,ochre,slate,plum,teal,sand}`
  + `-soft` pair, scoped at the bottom of `globals.css` per the
  "new tokens by domain-name" rule; the four pre-existing semantic
  colours alias their global token, the four new ones (slate, plum,
  teal, sand) ship with new swatches and light/dark variants.
- Future hook (out of scope, but the schema supports it): the agent
  reads collections as seed examples — "estimate this listing using
  only comparables from collection X."
- Follow-ups landed:
  - Migration 033 adds `tag_ids bigint[]` to `browse_stats` with the
    same AND-semantics as `listings_with_tags`. The Browse Stats
    tab now agrees with Map / Table when the operator filters by
    tag.
  - `PATCH /tags/{tag_id}` (`api/curation.update_tag` +
    `api/schemas.UpdateTagIn`) supports in-place rename + recolour;
    listing attachments are preserved because `listing_tags` joins
    by `tag_id`, not by name. A shared `TagEditPopover` wires
    rename / recolour / delete into both tag pickers — the
    CurationBlock matches list and the Browse Filters "Add" rows.

## Next

_No queued items. New operator-workflow features land here as bullets
under their own `### Phase ...` heading._
