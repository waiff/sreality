-- Separate non-image media (video tours) from the images pipeline.
--
-- iDNES (and potentially other portals) embed a video-tour clip as the FIRST
-- gallery item. It was being ingested into `images`, downloaded to R2 as a
-- `<id>/0000.jpg`, served as the listing "cover" (an <img> can't decode MP4, so
-- the card rendered blank), AND fed as the first frame to every LLM-vision /
-- pHash consumer. Videos now land here instead, keeping `images` strictly
-- photographic.
--
-- We KEEP the video URLs (and the bytes we already downloaded — the backfill
-- relocates them here, it does not delete from R2) for later use, but do NOT
-- process them today. The download-state columns mirror `images` so a future,
-- isolated video drain needs no further migration.

create table listing_videos (
  id                       bigserial primary key,
  sreality_id              bigint references listings(sreality_id) on delete cascade,
  source_url               text not null,
  sequence                 integer,
  storage_path             text,
  download_attempts        integer not null default 0,
  last_download_attempt_at timestamptz,
  unavailable_reason       text,
  last_error               text,
  created_at               timestamptz not null default now(),
  unique (sreality_id, sequence)
);

create index listing_videos_sreality_id_idx on listing_videos (sreality_id);

-- Backend-only table (no frontend/anon exposure, no *_public view): enable RLS
-- with no policy so the service role keeps full access while anon is blocked,
-- mirroring the `images` posture.
alter table listing_videos enable row level security;
