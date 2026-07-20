-- 310_image_border_cases.sql
-- Quarantine bucket for images where even a human isn't confident about the
-- room/plan classification — a separate concern from image_training_examples
-- (confident, curated ground truth): a border case can exist with NO training
-- label at all, or alongside one (the operator's best guess, flagged as
-- uncertain). Not itself a taxonomy label — the operator's own framing is
-- "resolve later, either by becoming its own label or by refining an existing
-- one once there are enough of these." Same mutable-upsert + _public view
-- shape as image_training_examples/image_tag_annotations (migrations 308/309).

create table image_border_cases (
  id            bigserial primary key,
  image_id      bigint not null references images(id) on delete cascade unique,
  created_by    text not null default 'operator',
  created_at    timestamptz not null default now()
);

alter table image_border_cases enable row level security;

create view image_border_cases_public as
select image_id, created_at
from image_border_cases;

grant select on image_border_cases_public to anon, authenticated;
