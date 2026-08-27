-- 446_tag_annotation_provenance.sql
--
-- Provenance on every annotation, plus the append-only history behind it.
-- PURELY ADDITIVE: no DROP, no DELETE, no destructive ALTER. Safe to apply
-- autonomously. Nothing in this file removes a row.
--
-- 73,499 rows sit in image_tag_labels today and 72,000 of them -- 98 percent --
-- are not decisions anybody made. Migration 442's backfill manufactured them
-- from a one-hot assumption ("this image was labeled kitchen, therefore it is
-- negative for the other 50 tags"). Today they are separable from operator work
-- only by a free-text created_by -- which 442 stamped on its ~1,440 genuine
-- hand-label transcriptions too, so that string alone would condemn the ground
-- truth along with the fiction (see the backfill below). This migration gives
-- every annotation a controlled-vocabulary `source`, so the eventual (separate,
-- gated, backed-up) deletion is exact and so every consumer can exclude them
-- starting now.
--
-- Four things a label carries from here on:
--   source          -- who or what decided it, and whether a human checked
--   definition_id   -- which tag_definitions version (migration 445) it was
--                      decided under, so a definition change can requeue
--                      exactly the annotations made under the old wording
--   verified_at     -- when a human last put eyes on the cell
--   excluded_reason -- ONLY on state='excluded': genuinely AMBIGUOUS, or
--                      deliberately PRUNED from the training set. Same effect
--                      on training, opposite meaning for diagnostics; an
--                      ambiguity rate over roughly 15 percent on a tag means go
--                      fix that tag's DEFINITION, not keep labeling.
--
-- NEVER DESTROY AN ANNOTATION. image_tag_labels stays latest-wins current state
-- (like `listings`); image_tag_label_events below is the immutable history (like
-- `listing_snapshots`) -- CLAUDE.md rule 8's idiom, applied here. History is
-- captured by a TRIGGER, not by application code: there are already four write
-- paths and the machine-proposal loop will add more, and a log that every
-- future writer must remember to append to is a log with holes in it. The
-- trigger precedent in this repo is migration 392's properties_log_status_event,
-- whose event_at / created_at split this table copies.
--
-- image_tag_labels is a cold table -- the admin-gated labeling UI is its only
-- writer -- so the ALTERs below take their locks immediately.
--
-- Backend-only: RLS on, no _public view, no anon/authenticated grants. Same
-- posture as migration 442.

begin;

------------------------------------------------------------------
-- 1. provenance columns on the annotation itself
------------------------------------------------------------------

-- Added nullable, backfilled, then set NOT NULL. `source` deliberately gets NO
-- column default: a row inserted without naming its source would silently claim
-- to be a human decision, which is the exact lie this migration exists to make
-- impossible. A forgetful writer gets a NOT NULL violation instead.
alter table image_tag_labels add column source          text;
alter table image_tag_labels add column definition_id   bigint
  references tag_definitions (id) on delete set null;
alter table image_tag_labels add column verified_at     timestamptz;
alter table image_tag_labels add column excluded_reason text;
alter table image_tag_labels add column model           text;

-- Backfill from the free-text created_by migration 442 wrote. created_by ALONE
-- is not enough to tell fiction from fact: migration 442 stamped BOTH arms of
-- its backfill with the same 'backfill:image_training_examples' string (442:82
-- and 442:88), and only the second arm is manufactured. The first arm
-- TRANSCRIBED image_training_examples -- the operator's own hand-labels from the
-- /phash-audit "Train" CTA (migration 309) -- into one positive per image. Those
-- ~1,440 positives are the only ground truth this system has. Calling them
-- backfill_442 would hand the deletion PR's `WHERE source = 'backfill_442'` the
-- entire positive set, report zero human work on every tag, and leave the
-- ambiguity rate with an empty denominator everywhere.
--
-- So the fiction is named by what it actually IS: a NEGATIVE the one-hot
-- assumption manufactured, that nobody has touched since. 442 wrote created_at
-- and updated_at off the same transaction clock, so `updated_at = created_at`
-- means exactly "never re-decided" -- and a cell the operator HAS since
-- re-decided is a decision, never deletable, whichever way it went.
--
-- Verified against live data 2026-08-27, and the split matters: 72,000 rows
-- classify as backfill_442, NOT the 72,058 the raw negative total suggests. The
-- other 58 are operator-made negatives, and 1,440 backfill-stamped POSITIVES are
-- the transcribed ground truth. Both stay 'human'. 72,000 + 1,440 + 58 + 1
-- excluded = 73,499, the whole table.
update image_tag_labels
set source = 'backfill_442'
where source is null
  and starts_with(created_by, 'backfill:')
  and state = 'negative'
  and updated_at = created_at;

-- Everything else is a decision a person made, and gets verified_at stamped from
-- updated_at -- a human did decide it, at that moment -- so "human-verified"
-- counts are correct on day one. definition_id stays NULL for every existing
-- row: they all predate migration 445, which is the honest answer, not a gap.
update image_tag_labels
set source = 'human',
    verified_at = updated_at
where source is null;

alter table image_tag_labels alter column source set not null;

alter table image_tag_labels
  add constraint image_tag_labels_source_check
  check (source in ('human', 'human_confirmed', 'machine', 'backfill_442'));

-- excluded_reason is meaningful ONLY on an excluded cell. A CHECK, not a
-- convention: "ambiguous" and "pruned" have opposite diagnostic meanings and a
-- reason attached to a positive or negative row would silently poison the
-- ambiguity rate. Nullable even when excluded -- the one excluded row that
-- exists today predates this column and its reason is genuinely unknown; a
-- guessed value would be worse than none.
alter table image_tag_labels
  add constraint image_tag_labels_excluded_reason_check
  check (
    excluded_reason is null
    or (state = 'excluded' and excluded_reason in ('ambiguous', 'pruned'))
  );

-- A model name only means something when a machine had a hand in the decision.
alter table image_tag_labels
  add constraint image_tag_labels_model_check
  check (model is null or source in ('machine', 'human_confirmed'));

comment on column image_tag_labels.source is
  'Who or what decided this cell, controlled vocabulary: human (a person '
  'originated it, including correcting a machine); human_confirmed (a machine '
  'proposed it and a person affirmed it -- stronger evidence than human alone, '
  'and the substrate for measuring per-tag machine autonomy); machine (a machine '
  'decided it, NOBODY has checked -- not ground truth); backfill_442 (manufactured '
  'by migration 442''s one-hot assumption, not a decision by anyone, slated for '
  'deletion in a separate gated PR). A human OVERRIDING a machine is source=human; '
  'the disagreement is preserved in image_tag_label_events, not in a fifth value.';

comment on column image_tag_labels.definition_id is
  'The tag_definitions version (migration 445) this decision was made under. NULL '
  'for every row predating migration 445. Resolved at write time from the tag''s '
  'active definition -- never passed in by a caller -- so it cannot cite another '
  'tag''s definition or a stale version. ON DELETE SET NULL, never RESTRICT: '
  'tag_definitions cascades off tag_taxonomy, and a RESTRICT here would make '
  'remove_tag fail.';

comment on column image_tag_labels.verified_at is
  'When a human last put eyes on this cell. Derived, never supplied: now() when '
  'source is human or human_confirmed, otherwise NULL, and preserved (never '
  'erased) when a machine later touches the cell.';

comment on column image_tag_labels.excluded_reason is
  'Only on state=excluded. ambiguous = the operator or machine genuinely could '
  'not decide; pruned = deliberately removed from the training set. Identical '
  'effect on training, opposite meaning for diagnostics -- only ambiguous counts '
  'toward a tag''s ambiguity rate, and pruned is excluded from that rate''s '
  'DENOMINATOR too, so pruning cannot dilute the signal.';

comment on column image_tag_labels.model is
  'Which model proposed the state, when a machine had a hand in it. NULL for a '
  'cold human decision and for a human correction (the machine did not propose '
  'the state that landed).';

------------------------------------------------------------------
-- 2. append-only history
------------------------------------------------------------------

-- NO FOREIGN KEYS, deliberately. remove_tag deletes a tag's annotations and then
-- the tag; images cascade into image_tag_labels. An audit log with ON DELETE
-- CASCADE destroys the record it exists to keep -- including the deletion event
-- that just fired -- and ON DELETE RESTRICT would make remove_tag fail forever
-- once any event existed. Bare bigints, plus a denormalized tag_label snapshot
-- so the log stays readable after the tag is gone. Same lenient-on-read trade
-- tag_definitions.example_image_ids already makes.
create table image_tag_label_events (
  id              bigserial primary key,
  image_id        bigint not null,
  tag_id          bigint not null,
  tag_label       text,
  -- NULL state = the cell was cleared back to untouched. Absence is not a
  -- negative, in the history exactly as in the matrix.
  state           text check (state in ('positive', 'negative', 'excluded')),
  prior_state     text check (prior_state in ('positive', 'negative', 'excluded')),
  source          text not null
    check (source in ('human', 'human_confirmed', 'machine', 'backfill_442')),
  definition_id   bigint,
  model           text,
  excluded_reason text check (excluded_reason in ('ambiguous', 'pruned')),
  -- Who last WROTE the cell (image_tag_labels.created_by). On a cleared event
  -- this is who had created the row, not who cleared it: the trigger sees only
  -- OLD. Single-operator platform, so not currently a distinction.
  actor           text not null,
  verified_at     timestamptz,
  -- event_at = when the decision happened; created_at = when the log row landed.
  -- They are equal for captured events and differ for the one-time seed below --
  -- migration 392's split, same reason.
  event_at        timestamptz not null,
  created_at      timestamptz not null default now(),

  constraint image_tag_label_events_something_happened
    check (state is not null or prior_state is not null)
);

-- The only read this shape has: one cell's full history, oldest to newest. No
-- speculative (tag_id, event_at) index -- nothing reads that yet, and this table
-- is small enough to index later without ceremony.
create index image_tag_label_events_cell_idx
  on image_tag_label_events (image_id, tag_id, id);

alter table image_tag_label_events enable row level security;
revoke all on image_tag_label_events from anon, authenticated;

comment on table image_tag_label_events is
  'Append-only history of every state an (image, tag) cell has ever been in, with '
  'the provenance that produced it. image_tag_labels is latest-wins current state, '
  'this is the immutable record -- the listings / listing_snapshots idiom (CLAUDE.md '
  'rule 8). Written ONLY by the log_image_tag_label_event trigger, never by '
  'application code, so it cannot develop holes as new writers appear. state IS '
  'NULL means the cell was cleared back to untouched, which is itself a recorded '
  'decision. Carries NO foreign keys on purpose: a cascade would destroy the record '
  'this table exists to keep. Backend-only, RLS on, no _public view.';

-- One-time seed: a genesis event for every annotation that exists today, so no
-- cell's history starts mid-story. event_at is the row's updated_at (when its
-- CURRENT state was written); created_at is now(), which is what distinguishes a
-- seeded row from a captured one. Written directly, not through the trigger --
-- which does not exist yet at this point in the file, so the backfill UPDATE
-- above produced no events either. That is intended: a schema backfill is not a
-- decision, and 73,499 phantom "events" would pollute the very log this creates.
insert into image_tag_label_events (
  image_id, tag_id, tag_label, state, prior_state, source,
  definition_id, model, excluded_reason, actor, verified_at, event_at
)
select itl.image_id, itl.tag_id, t.label, itl.state, null, itl.source,
       itl.definition_id, itl.model, itl.excluded_reason, itl.created_by,
       itl.verified_at, itl.updated_at
from image_tag_labels itl
left join tag_taxonomy t on t.id = itl.tag_id;

------------------------------------------------------------------
-- 3. the trigger that keeps it complete
------------------------------------------------------------------

-- ci-allow-ungated: log_image_tag_label_event  an AFTER trigger on a backend-only
-- table, not SECURITY DEFINER (so it runs as whoever writes image_tag_labels, who
-- by definition already reads it) and with EXECUTE revoked from public, so it is
-- not reachable as an RPC. The is_platform_admin() gate the rule asks for is for
-- READ SURFACES; this function returns no rows to anyone.
create function log_image_tag_label_event() returns trigger
language plpgsql
as $$
declare
  v_row   record;
  v_state text;
  v_prior text;
begin
  if TG_OP = 'DELETE' then
    v_row   := OLD;
    v_state := null;
    v_prior := OLD.state;
  else
    v_row   := NEW;
    v_state := NEW.state;
    v_prior := case when TG_OP = 'UPDATE' then OLD.state else null end;
    -- Nothing that matters changed: a bare updated_at touch is not a decision.
    -- Note verified_at IS in this list, so a human re-affirming a cell that was
    -- already in that state DOES record an event. That is deliberate: a second
    -- independent human pass agreeing is evidence, and "every training label is
    -- a recorded decision" includes the sweep that re-confirmed 200 of them.
    if TG_OP = 'UPDATE'
       and OLD.state           is not distinct from NEW.state
       and OLD.source          is not distinct from NEW.source
       and OLD.definition_id   is not distinct from NEW.definition_id
       and OLD.model           is not distinct from NEW.model
       and OLD.excluded_reason is not distinct from NEW.excluded_reason
       and OLD.verified_at     is not distinct from NEW.verified_at then
      return NEW;
    end if;
  end if;

  insert into image_tag_label_events (
    image_id, tag_id, tag_label, state, prior_state, source,
    definition_id, model, excluded_reason, actor, verified_at, event_at
  )
  values (
    v_row.image_id, v_row.tag_id,
    -- NULL only if the tag row is already gone (a raw cascade off tag_taxonomy);
    -- remove_tag deletes annotations first, so the label is normally still there.
    (select label from tag_taxonomy where id = v_row.tag_id),
    v_state, v_prior, v_row.source,
    v_row.definition_id, v_row.model, v_row.excluded_reason,
    v_row.created_by, v_row.verified_at, now()
  );

  if TG_OP = 'DELETE' then
    return OLD;
  end if;
  return NEW;
end;
$$;

-- Not SECURITY DEFINER and not callable as an ordinary RPC, but revoked
-- explicitly to match this project's "revoke on every new function" posture
-- (migration 392).
revoke execute on function log_image_tag_label_event() from public;

-- `update of <cols>` scopes the trigger to statements that actually touch a
-- meaningful column (migration 392's `OF is_active`, migration 140's `OF geom`);
-- the IS DISTINCT FROM guard inside still gates on a REAL change.
create trigger image_tag_labels_log_event
  after insert or delete or update of
    state, source, definition_id, model, excluded_reason, verified_at
  on image_tag_labels
  for each row
  execute function log_image_tag_label_event();

commit;
