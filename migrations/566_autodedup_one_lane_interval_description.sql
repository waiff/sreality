-- 566_autodedup_one_lane_interval_description.sql — W5 (AUTODEDUP PROGRAM.md E911, E914).
--
-- The always-on worker's `autodedup` lane is now THE one path of the dedup engine: one pass
-- decides and groups, then RECONCILES production — the groups it re-clustered become merges
-- through toolkit.property_identity.merge_property_set, inside the area
-- app_settings.autodedup_apply_scope names. The row migration 557 seeded still tells /settings
-- that the lane "never merges anything in Browse", which is now false, and /settings is where
-- the operator turns the lane on. This rewrites that DESCRIPTION only: the value (the interval
-- an operator may have set) is not touched, no schema changes, and a re-run writes the same
-- text again. Migration 565 is the sibling feature/mf-read-time branch's.

set lock_timeout = '5s';

update app_settings
   set description =
       'Seconds between two passes of the duplicate engine inside the always-on worker; 0 stops '
       'it. A pass decides new and changed adverts against their neighbours, groups them, and '
       'MERGES each group it re-grouped into one property through the normal merge function - '
       'only inside the area autodedup_apply_scope names, never splitting anything. Keep it at 0 '
       'until the engine has been re-seeded for this version (autodedup lane, mode=rt_seed '
       'fresh=true) and its gates G1-G3 have passed; only then raise it (60: a pass only looks '
       'at adverts at least five minutes old). To stop it: 0 here; to undo what it merged: the '
       'autodedup lane, mode=unapply (which refuses while this is above 0).'
 where key = 'realtime_autodedup_interval_seconds';

reset lock_timeout;
