-- 473: tag_label_notes — the operator's reason for changing a mark.
--
-- The training-set review page lets the operator flip a machine label (applies
-- / does not / left out). A flip alone says WHAT was wrong; a note says WHY,
-- and the why is what improves the definition the next machine pass reads.
-- Notes are the raw material for definition revisions, never labels
-- themselves, and never training data.
--
-- HOW NOTES BECOME DEFINITION TEXT is a rule, recorded here because a schema
-- comment outlives a chat: a note is NOT copied into the definition one
-- sentence per note. The definitions are read by a model AND by a person, and
-- either one absorbs a short, general rule and drowns in a list of specifics.
-- So the reviser reads every unabsorbed note for a head together, finds the
-- rule the notes point at, states that rule ONCE at the level of the existing
-- lines, and marks the batch absorbed by the version that carries it.
--
-- absorbed_definition_id records which version incorporated a note, so the
-- same note is never re-read into a later revision and the audit stays
-- possible ("what did v10 change and why").

begin;

create table tag_label_notes (
  id          bigserial primary key,
  image_id    bigint not null references images (id) on delete cascade,
  tag_id      bigint not null references tag_taxonomy (id) on delete cascade,
  -- The mark before and after. from_state is null when the cell was untouched.
  from_state  text check (from_state in ('positive', 'negative', 'excluded')),
  to_state    text not null check (to_state in ('positive', 'negative', 'excluded')),
  note        text not null check (length(btrim(note)) between 1 and 600),
  created_by  text not null default 'operator',
  created_at  timestamptz not null default now(),
  -- The definition version that incorporated this note; null = still open.
  absorbed_definition_id bigint references tag_definitions (id) on delete set null,
  absorbed_at timestamptz
);

create index tag_label_notes_open_by_tag
  on tag_label_notes (tag_id, created_at desc)
  where absorbed_definition_id is null;

comment on table tag_label_notes is
  'Why the operator changed a training-set mark. Raw material for definition '
  'revisions: read per head, distilled into ONE general rule, then marked '
  'absorbed by the version that carries it. Never a label, never training data.';

alter table tag_label_notes enable row level security;
revoke all on tag_label_notes from anon, authenticated;
revoke all on sequence tag_label_notes_id_seq from anon, authenticated;

commit;
