-- 616: the real-time lane's calibration payload as compressed bytes, not jsonb (PROGRAM.md E963).
--
-- WHY. `autodedup.rt_calibration.payload` is one jsonb value, and a jsonb container stops at
-- 268,435,455 bytes of its BINARY form (`JENTRY_OFFLENMASK`, src/include/utils/jsonb.h; raised as
-- "total size of jsonb object elements exceeds the maximum" by convertJsonbObject in
-- src/backend/utils/adt/jsonb_util.c). The binary is ~1.57 times the JSON text (the 10-16 seed's
-- row: 14,712,023 bytes binary, 9,367,806 of text, for 60,594 adverts), and a calibration over
-- small blocks is ~678 bytes of text an advert (E962) — ~1,065 binary: the ceiling binds at
-- ~252k such adverts, and a nationwide scope (~872k) is ~591 MB of text. A bytea is one varlena,
-- up to 1 GB (read back as hex, so ~537 MB of it), and the payload goes in zlib-compressed (3.6x
-- on the trial and cohort 18): ~164 MB for the nation.
--
-- WHAT. `payload_zlib`: zlib over the calibration's canonical JSON (sorted keys, UTF-8;
-- `autodedup.calibration_cut.encode_payload`), written by every cut and read by one reader,
-- `incremental_lane.read_calibration`. `digest` is unchanged: it is `Calibration.digest()`, a
-- hash of the same calibration, never of the stored bytes. TOAST stores the value out of line as
-- it does any large value; pglz abandons an incompressible value within its first kilobyte.
--
-- `payload` (jsonb) and `artifact_url` are RETIRED: from E963 on no code reads or writes either
-- (`artifact_url` only ever held NULL). They are dropped by a later DESTRUCTIVE migration, with
-- the operator's OK and a backup, right after the sitting is verified (an old reader then fails
-- loudly); until then the 10-16 seed's jsonb stays where it is, read by nothing.
--
-- AFTER THE FIRST E963 SEED COMMITS, NEVER A BARE REVERT: BRAKE, REVERT, RE-SEED. The seed's
-- upsert leaves `payload` holding the 10-16 calibration (60,594 adverts, digest 8a84f60920e84b59)
-- beside the new digest and blob: a reverted reader would decide the re-seeded store under it.
--
-- Additive and idempotent; schema `autodedup` only (ruling D8). A nullable column with no default
-- rewrites nothing. The table holds one row a generation.
--
-- APPLY UNDER THE LANE'S BRAKE, BEFORE THE CODE MERGES, THEN RE-SEED. The brake is
-- `realtime_autodedup_interval_seconds` 0 with `autodedup.rt_lease` clear (the ALTER takes ACCESS
-- EXCLUSIVE). Until it is applied the code that reads the column skips every pass
-- (`store_absent`). A row cut before it has no `payload_zlib`, and the reader refuses that row
-- rather than decide under an empty calibration: the generation is re-seeded
-- (`rt_seed fresh=true`, the planned re-seed) before the brake comes off.

set lock_timeout = '5s';

alter table autodedup.rt_calibration add column if not exists payload_zlib bytea;

comment on column autodedup.rt_calibration.payload_zlib is
  'E963: zlib over the calibration''s canonical JSON (sorted keys, UTF-8), written by every cut '
  'and read by incremental_lane.read_calibration alone. digest is Calibration.digest(), not a '
  'hash of these bytes.';

comment on column autodedup.rt_calibration.payload is
  'RETIRED by E963 (migration 616): from E963 on read and written by nothing; payload_zlib '
  'holds the calibration. To be dropped by a later migration with the operator''s OK.';

comment on column autodedup.rt_calibration.artifact_url is
  'RETIRED by E963 (migration 616): only ever NULL, from E963 on written by nothing. To be '
  'dropped by a later migration with the operator''s OK.';

reset lock_timeout;
