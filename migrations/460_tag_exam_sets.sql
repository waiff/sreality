-- 460: exam SETS — iterations of the exam are named question lists over the SAME
-- 250 images.
--
-- WHY. The operator's ruling: the next labeling iteration runs on a completely new
-- set of tags (obývací pokoj, jídelna, nezařízená místnost), REPLACING the current
-- eight for that sitting, because the 1-8 keys UX is worth keeping. Tags will be
-- added to a set as needed.
--
-- The cohort cannot grow rows (an image belongs to at most ONE exam — the unique
-- index in 458 — and the sample's whole value is that it never changes), so an
-- iteration is the same 250 images asked a NEW question list: "the exam grows by
-- columns, never by rows." A set is that list, named, ORDERED (the array order IS
-- the 1..n key order on screen), and operator-owned.
--
-- Why sets are their own table and not routing_categories: the exam question list
-- and the dedup router are DIFFERENT facts that briefly coincided. Iteration 2's
-- trio are TRAINING heads — nothing in the north star compares obývací-to-obývací
-- to decide identity — so deriving the exam from routing flags would have forced a
-- false choice between breaking the exam and lying about the router.
--
-- CORRECTION to 457's header, recorded forward since history is append-only: 457
-- said the router map would "read the same column inverted." It will not.
-- routing_categories means exactly what its own comment says — which property
-- types a tag's IMAGES live in (candidate-draw scoping + the screener's tag list)
-- — and the router's comparison map will be its own explicit config when it is
-- built. The trio below gets the flag for DRAW CORRECTNESS (an obývací draw
-- spending 20% of its quota on pozemek listings would be the koupelna bug again),
-- not router membership.
--
-- Editing a set: add tags BEFORE its sitting starts. Adding mid-sitting re-serves
-- already-answered images asking ALL the set's columns again — deliberate (the new
-- column genuinely needs answers) but expensive, so the runbook says don't.

begin;

create table tag_exam_sets (
  id         bigserial primary key,
  name       text not null unique,
  -- Ordered: position in this array is the button position and digit key on the
  -- exam screen. Capped at 10 because the keyboard has digits 1-9 and 0; a
  -- larger sitting belongs in two sets.
  tag_ids    bigint[] not null
               check (array_length(tag_ids, 1) between 1 and 10),
  note       text,
  created_at timestamptz not null default now()
);

comment on table tag_exam_sets is
  'Named, ordered exam question lists. One sitting = one set over the sealed '
  'cohort; iteration N is a new set, never new images (458''s one-exam-per-image '
  'rule). Array order = on-screen key order. Operator-owned.';

alter table tag_exam_sets enable row level security;
revoke all on tag_exam_sets from anon, authenticated;
revoke all on sequence tag_exam_sets_id_seq from anon, authenticated;

-- set_1 = the sitting in progress, seeded in the EXACT order the screen already
-- shows (tag-id order), so nothing moves under the operator's fingers.
insert into tag_exam_sets (name, tag_ids, note) values
  ('set_1', '{3,17,22,25,42,43,46,48}',
   'Iteration 1 — the eight dedup routing tags.'),
  ('set_2', '{28,20,27}',
   'Iteration 2 — interior TRAINING heads (obývací pokoj, jídelna, nezařízená '
   'místnost), in the operator''s stated order. Replaces set_1 for its sitting; '
   'extend by updating tag_ids BEFORE the sitting starts.');

-- Draw scoping for the trio: interiors live in byt/dum/komercni listings. This is
-- the image property-type scope, NOT router membership — see header.
update tag_taxonomy set routing_categories = '{byt,dum,komercni}'
 where id in (20, 27, 28) and routing_categories is null;

commit;
