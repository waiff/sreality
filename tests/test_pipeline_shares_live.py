"""Pipeline sharing and acting as the owner (migration 641), proven on the replayed schema.

Four tenants, each born through the real signup trigger (account, default board, monitoring
collection, trial): A owns the board and some curation, B holds a READ share of it (the board only,
ruling 2), C an EDIT share (all eleven curation tables), D none. Every tenant statement runs through `tests/_tenant_scope.scoped` in cursor mode
(SET LOCAL ROLE authenticated + the claims, header and method GUCs PostgREST and the API set),
inside one transaction the `cur` fixture rolls back; seeds and read-backs run as the owner login,
which RLS never binds. Skips without TEST_DATABASE_URL; CI's migrations lane sets
DB_RAILS_REQUIRED=1, so a lane that loses its database goes red instead of green.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from typing import Any, NamedTuple

import pytest

from tests._live_property import REQUIRED_DB, _property, cur  # noqa: F401
from tests._tenant_scope import scoped

pytestmark = REQUIRED_DB

CURATION = (
    "collections", "tags", "property_notes", "collection_properties", "property_tags",
    "property_note_attachments", "property_artifact_links", "property_dismissals",
    "property_pipeline", "pipeline_stages", "property_pipeline_events",
)
BOARD = ("property_pipeline", "pipeline_stages", "property_pipeline_events")
BOARD_VIEWS = ("pipeline_stages_public", "property_pipeline_public", "pipeline_board_public")
WRITES = ("POST", "PATCH", "DELETE")


class _Tenant(NamedTuple):
    acc: uuid.UUID
    user: uuid.UUID
    email: str


class _World(NamedTuple):
    a: _Tenant
    b: _Tenant
    c: _Tenant
    d: _Tenant
    card: int        # property on A's board (entry stage), noted by A (the note carries a file)
    spare: int       # in A's collection, tagged by A
    loose: int       # touched by nobody yet
    entry: int       # A's entry stage
    second: int      # A's second stage
    coll: int        # A's collection (holds `spare`)
    tag: int         # A's tag (on `spare`)
    note: int        # A's note on `card`


def _signup(cur: Any, key: str) -> _Tenant:
    user = uuid.uuid4()
    email = f"pshare-{key}-{user.hex[:8]}@test.local"
    cur.execute(
        "INSERT INTO auth.users (id, email, email_confirmed_at) VALUES (%s, %s, now())",
        (user, email),
    )
    cur.execute("SELECT account_id FROM account_members WHERE user_id = %s", (user,))
    return _Tenant(cur.fetchone()[0], user, email)


def _one(cur: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
    cur.execute(sql, params)
    return cur.fetchone()[0]


@pytest.fixture
def world(cur: Any) -> _World:  # noqa: F811
    a, b, c, d = (_signup(cur, k) for k in "abcd")
    card, spare, loose, dismissed = (_property(cur) for _ in range(4))
    entry = _one(cur, "SELECT id FROM pipeline_stages WHERE account_id = %s AND is_entry", (a.acc,))
    second = _one(
        cur,
        "SELECT id FROM pipeline_stages WHERE account_id = %s AND NOT is_entry "
        "AND archived_at IS NULL ORDER BY position LIMIT 1",
        (a.acc,),
    )
    cur.execute(
        "INSERT INTO property_pipeline (account_id, property_id, stage_id) VALUES (%s, %s, %s)",
        (a.acc, card, entry),
    )
    cur.execute(
        "INSERT INTO property_pipeline_events (account_id, property_id, to_stage_id, reason) "
        "VALUES (%s, %s, %s, 'operator')",
        (a.acc, card, entry),
    )
    coll = _one(
        cur, "INSERT INTO collections (account_id, name) VALUES (%s, %s) RETURNING id",
        (a.acc, f"pshare-{uuid.uuid4().hex[:12]}"),
    )
    cur.execute(
        "INSERT INTO collection_properties (collection_id, property_id) VALUES (%s, %s)",
        (coll, spare),
    )
    tag = _one(
        cur, "INSERT INTO tags (account_id, name, color) VALUES (%s, %s, 'slate') RETURNING id",
        (a.acc, f"pshare-{uuid.uuid4().hex[:12]}"),
    )
    cur.execute("INSERT INTO property_tags (property_id, tag_id) VALUES (%s, %s)", (spare, tag))
    note = _one(
        cur,
        "INSERT INTO property_notes (account_id, property_id, body) VALUES (%s, %s, 'mine') "
        "RETURNING id",
        (a.acc, card),
    )
    cur.execute(
        "INSERT INTO property_note_attachments (note_id, account_id, storage_key, filename, "
        "mime_type, byte_size, sha256_hex) VALUES (%s, %s, 'k', 'a.pdf', 'application/pdf', 1, %s)",
        (note, a.acc, "0" * 64),
    )
    cur.execute(
        "INSERT INTO property_artifact_links (account_id, property_id, url) "
        "VALUES (%s, %s, 'https://example.test/a')",
        (a.acc, card),
    )
    cur.execute(
        "INSERT INTO property_dismissals (account_id, property_id) VALUES (%s, %s)",
        (a.acc, dismissed),
    )
    # B's own curation, so "nothing of B's own" is not vacuous.
    b_entry = _one(cur, "SELECT id FROM pipeline_stages WHERE account_id = %s AND is_entry", (b.acc,))
    cur.execute(
        "INSERT INTO property_pipeline (account_id, property_id, stage_id) VALUES (%s, %s, %s)",
        (b.acc, loose, b_entry),
    )
    cur.execute(
        "INSERT INTO property_notes (account_id, property_id, body) VALUES (%s, %s, 'b-own')",
        (b.acc, card),
    )
    cur.execute(
        "INSERT INTO pipeline_shares (account_id, recipient_account_id, mode) "
        "VALUES (%s, %s, 'read'), (%s, %s, 'edit')",
        (a.acc, b.acc, a.acc, c.acc),
    )
    return _World(a, b, c, d, card, spare, loose, entry, second, coll, tag, note)


def _rows(cur: Any, t: _Tenant, sql: str, params: tuple[Any, ...] = (), *,
          act_as: uuid.UUID | None = None, method: str = "GET") -> list[tuple[Any, ...]]:
    with scoped(t.user, act_as=act_as, method=method, cur=cur):
        cur.execute(sql, params)
        return cur.fetchall()


def _touched(cur: Any, t: _Tenant, sql: str, params: tuple[Any, ...] = (), *,
             act_as: uuid.UUID | None = None, method: str = "PATCH") -> int:
    with scoped(t.user, act_as=act_as, method=method, cur=cur):
        cur.execute(sql, params)
        return cur.rowcount


def _refused(cur: Any, t: _Tenant, sql: str, params: tuple[Any, ...] = (), *,
             act_as: uuid.UUID | None = None, method: str = "POST") -> None:
    """The statement raises 42501 inside a SAVEPOINT, so the refusal cannot poison the test."""
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with cur.connection.transaction():
            _touched(cur, t, sql, params, act_as=act_as, method=method)


def _owned(cur: Any, table: str, acc: uuid.UUID) -> int:
    return _one(cur, f"SELECT count(*) FROM {table} WHERE account_id = %s", (acc,))


def test_without_the_header_acting_is_identity(cur: Any, world: _World) -> None:  # noqa: F811
    both = (
        "SELECT (SELECT array_agg(a.account_id ORDER BY a.account_id) FROM acting_account_ids() a), "
        "       (SELECT array_agg(c.id ORDER BY c.id) FROM current_account_ids() c(id)), acting()"
    )
    # Never set (a fresh session), then empty (what a session reads after a local set ends), then
    # PostgREST's shape with no x-act-as. Scoped by hand on purpose: scoped() always sets
    # request.headers, so it cannot express "never set".
    cur.execute("SET LOCAL ROLE authenticated")
    cur.execute(
        "SELECT set_config('request.jwt.claims', %s, true)",
        (json.dumps({"sub": str(world.a.user), "role": "authenticated"}),),
    )
    cur.execute(both)
    assert cur.fetchone() == ([world.a.acc], [world.a.acc], False)
    cur.execute("SELECT set_config('request.headers', '', true)")
    cur.execute(both)
    assert cur.fetchone() == ([world.a.acc], [world.a.acc], False)
    cur.execute("RESET ROLE")
    for t in (world.a, world.b, world.c, world.d):
        for method in ("GET", *WRITES):
            assert _rows(cur, t, both, method=method) == [([t.acc], [t.acc], False)]


def test_a_read_recipient_reads_the_owners_board_only_and_nothing_of_its_own(
    cur: Any, world: _World,  # noqa: F811
) -> None:
    a, b = world.a, world.b
    for table in CURATION:
        seen = _rows(cur, b, f"SELECT DISTINCT account_id FROM {table}", act_as=a.acc)
        if table in BOARD:
            assert seen == [(a.acc,)], f"{table}: B acting as A saw {seen}"
            assert _rows(cur, b, f"SELECT count(*) FROM {table}", act_as=a.acc) == [
                (_owned(cur, table, a.acc),)
            ], table
        else:
            # Ruling 2: a read share is the board, never the owner's notes, files, tags, lists...
            assert _owned(cur, table, a.acc) >= 1, f"the seed for {table} is missing"
            assert seen == [], f"{table}: a READ share reached A's {table}"
    assert _rows(cur, b, "SELECT stage_id FROM property_pipeline WHERE property_id = %s",
                 (world.card,), act_as=a.acc) == [(world.entry,)]
    assert _rows(cur, b, "SELECT property_id FROM property_pipeline WHERE property_id = %s",
                 (world.loose,), act_as=a.acc) == []
    assert _rows(cur, b, "SELECT body FROM property_notes WHERE property_id = %s",
                 (world.card,), act_as=a.acc) == []
    # Without the header B is B again.
    assert _rows(cur, b, "SELECT body FROM property_notes WHERE property_id = %s",
                 (world.card,)) == [("b-own",)]
    # Co-recipients do not see each other (ruling 9): B reads its own grant only.
    assert _rows(cur, b, "SELECT account_id, recipient_account_id, mode FROM pipeline_shares") == [
        (a.acc, b.acc, "read")
    ]


def test_a_read_recipient_writes_nothing(cur: Any, world: _World) -> None:  # noqa: F811
    a, b = world.a, world.b
    for method in WRITES:
        assert _touched(cur, b, "UPDATE property_pipeline SET stage_id = %s WHERE property_id = %s",
                        (world.second, world.card), act_as=a.acc, method=method) == 0
        assert _touched(cur, b, "DELETE FROM property_notes WHERE id = %s", (world.note,),
                        act_as=a.acc, method=method) == 0
        assert _touched(cur, b, "UPDATE pipeline_stages SET label = 'taken' WHERE id = %s",
                        (world.entry,), act_as=a.acc, method=method) == 0
        assert _rows(cur, b, "SELECT count(*) FROM collections", act_as=a.acc, method=method) == [(0,)]
    _refused(cur, b, "INSERT INTO property_notes (account_id, property_id, body) "
             "VALUES (%s, %s, 'as A')", (a.acc, world.loose), act_as=a.acc)
    _refused(cur, b, "INSERT INTO collection_properties (collection_id, property_id) VALUES (%s, %s)",
             (world.coll, world.loose), act_as=a.acc)
    assert _one(cur, "SELECT stage_id FROM property_pipeline WHERE account_id = %s AND property_id = %s",
                (a.acc, world.card)) == world.entry
    assert _one(cur, "SELECT count(*) FROM delegated_edits WHERE owner_account_id = %s", (a.acc,)) == 0


def test_an_edit_recipient_writes_as_the_owner_and_every_write_is_stamped(
    cur: Any, world: _World,  # noqa: F811
) -> None:
    a, c = world.a, world.c
    acting = {"act_as": a.acc, "method": "POST"}
    own_before = {t: _owned(cur, t, c.acc) for t in CURATION}
    assert _touched(cur, c, "UPDATE property_pipeline SET stage_id = %s WHERE property_id = %s",
                    (world.second, world.card), **acting) == 1
    note = _rows(cur, c, "INSERT INTO property_notes (account_id, property_id, body) "
                 "VALUES (%s, %s, 'by C') RETURNING id", (a.acc, world.loose), **acting)[0][0]
    _touched(cur, c, "INSERT INTO property_tags (property_id, tag_id) VALUES (%s, %s)",
             (world.loose, world.tag), **acting)
    _touched(cur, c, "INSERT INTO collection_properties (collection_id, property_id) VALUES (%s, %s)",
             (world.coll, world.loose), **acting)
    dismissal = _rows(cur, c, "INSERT INTO property_dismissals (account_id, property_id) "
                      "VALUES (%s, %s) RETURNING id", (a.acc, world.loose), **acting)[0][0]
    stage = _rows(cur, c, "INSERT INTO pipeline_stages (account_id, key, label, position) "
                  "VALUES (%s, %s, 'by C', 90) RETURNING id",
                  (a.acc, f"pshare-{uuid.uuid4().hex[:12]}"), **acting)[0][0]
    assert _touched(cur, c, "UPDATE pipeline_stages SET label = 'renamed by C' WHERE id = %s",
                    (world.entry,), **acting) == 1
    assert _touched(cur, c, "UPDATE pipeline_stages SET position = position + 100 WHERE id IN (%s, %s)",
                    (world.entry, world.second), **acting) == 2
    assert _touched(cur, c, "UPDATE pipeline_stages SET archived_at = now() WHERE id = %s",
                    (stage,), **acting) == 1
    event = _rows(cur, c, "INSERT INTO property_pipeline_events (account_id, property_id, from_stage_id, "
                  "to_stage_id, reason) VALUES (%s, %s, %s, %s, 'operator') RETURNING id",
                  (a.acc, world.card, world.entry, world.second), **acting)[0][0]
    tag = _rows(cur, c, "INSERT INTO tags (account_id, name, color) VALUES (%s, %s, 'slate') RETURNING id",
                (a.acc, f"pshare-{uuid.uuid4().hex[:12]}"), **acting)[0][0]
    coll = _rows(cur, c, "INSERT INTO collections (account_id, name) VALUES (%s, %s) RETURNING id",
                 (a.acc, f"pshare-{uuid.uuid4().hex[:12]}"), **acting)[0][0]
    _touched(cur, c, "INSERT INTO property_artifact_links (account_id, property_id, url) "
             "VALUES (%s, %s, 'https://example.test/c')", (a.acc, world.loose), **acting)

    assert {t: _owned(cur, t, c.acc) for t in CURATION} == own_before, "C wrote as C, not as A"
    assert _one(cur, "SELECT account_id FROM property_notes WHERE id = %s", (note,)) == a.acc
    assert _one(cur, "SELECT account_id FROM property_tags WHERE property_id = %s AND tag_id = %s",
                (world.loose, world.tag)) == a.acc
    for table, row_id in (("property_pipeline_events", event), ("tags", tag), ("collections", coll)):
        assert _one(cur, f"SELECT account_id FROM {table} WHERE id = %s", (row_id,)) == a.acc, table

    expected = Counter({
        ("property_pipeline", f"{a.acc}:{world.card}", "update"),
        ("property_notes", str(note), "insert"),
        ("property_tags", f"{world.loose}:{world.tag}", "insert"),
        ("collection_properties", f"{world.coll}:{world.loose}", "insert"),
        ("property_dismissals", str(dismissal), "insert"),
        ("pipeline_stages", str(stage), "insert"),
        ("pipeline_stages", str(world.second), "update"),
        ("pipeline_stages", str(stage), "update"),
        ("property_pipeline_events", str(event), "insert"),
        ("tags", str(tag), "insert"),
        ("collections", str(coll), "insert"),
        ("property_artifact_links", f"{a.acc}:{world.loose}", "insert"),
    })
    expected[("pipeline_stages", str(world.entry), "update")] = 2  # the rename, then the reorder
    ledger = "SELECT relation, row_key, op, actor_user_id, actor_account_id, owner_account_id " \
             "FROM delegated_edits"
    as_a = _rows(cur, a, ledger)
    assert Counter(r[:3] for r in as_a) == expected, "each delegated write lands exactly ONE stamp"
    assert {r[3:] for r in as_a} == {(c.user, c.acc, a.acc)}
    for t in (world.b, c, world.d):
        assert _rows(cur, t, ledger) == [], f"{t.email} read A's ledger"
        assert _rows(cur, t, ledger, act_as=a.acc) == [], f"{t.email} read A's ledger acting"

    # The board views carry the latest stamp to the owner; the owner's own write stamps nothing.
    assert _rows(cur, a, "SELECT edited_by_account_id FROM property_pipeline_public "
                 "WHERE property_id = %s", (world.card,)) == [(c.acc,)]
    assert _rows(cur, a, "SELECT edited_by_account_id FROM pipeline_board_public "
                 "WHERE property_id = %s", (world.card,)) == [(c.acc,)]
    assert _rows(cur, a, "SELECT edited_by_account_id, edited_at IS NOT NULL FROM pipeline_stages_public "
                 "WHERE id = %s", (world.entry,)) == [(c.acc, True)]
    untouched = _one(cur, "SELECT id FROM pipeline_stages WHERE account_id = %s AND archived_at IS NULL "
                     "AND id NOT IN (%s, %s) ORDER BY position LIMIT 1", (a.acc, world.entry, world.second))
    assert _rows(cur, a, "SELECT edited_by_account_id FROM pipeline_stages_public WHERE id = %s",
                 (untouched,)) == [(None,)]
    before = _one(cur, "SELECT count(*) FROM delegated_edits")
    _touched(cur, a, "INSERT INTO property_notes (account_id, property_id, body) VALUES (%s, %s, 'own')",
             (a.acc, world.spare), method="POST")
    assert _one(cur, "SELECT count(*) FROM delegated_edits") == before

    # The API's card projection reads the same stamp, labelled through the party door.
    from api import pipeline

    with scoped(a.user, cur=cur):
        card = pipeline._fetch_card(cur.connection, world.card, a.acc)
    assert card is not None and card["edited_by_email"] == c.email and card["edited_at"] is not None

    # The owner's own LATER write clears the stamp (a later request: a later updated_at).
    assert _touched(cur, a, "UPDATE property_pipeline SET stage_id = %s, updated_at = clock_timestamp() "
                    "WHERE property_id = %s", (world.entry, world.card)) == 1
    assert _rows(cur, a, "SELECT edited_by_account_id, edited_at FROM property_pipeline_public "
                 "WHERE property_id = %s", (world.card,)) == [(None, None)]

    # A revoked delegate stays named to the owner (the ledger arm of the label door), and a
    # downgrade to read takes the writes away on the next statement.
    cur.execute("UPDATE pipeline_shares SET mode = 'read' WHERE account_id = %s AND recipient_account_id = %s",
                (a.acc, c.acc))
    assert _touched(cur, c, "UPDATE pipeline_stages SET label = 'again' WHERE id = %s",
                    (world.second,), **acting) == 0
    cur.execute("DELETE FROM pipeline_shares WHERE account_id = %s AND recipient_account_id = %s",
                (a.acc, c.acc))
    assert (c.acc, c.email) in set(_rows(cur, a, "SELECT account_id, email FROM share_party_emails()"))
    assert _rows(cur, c, "SELECT account_id FROM share_party_emails()") == []


def test_acting_never_reaches_the_owners_identity_scoped_rows(
    cur: Any, world: _World,  # noqa: F811
) -> None:
    a, c, d = world.a, world.c, world.d
    d_coll = _one(
        cur, "INSERT INTO collections (account_id, name) VALUES (%s, %s) RETURNING id",
        (d.acc, f"pshare-{uuid.uuid4().hex[:12]}"),
    )
    cur.execute("INSERT INTO collection_properties (collection_id, property_id) VALUES (%s, %s)",
                (d_coll, world.loose))
    cur.execute("INSERT INTO collection_shares (collection_id, recipient_account_id) VALUES (%s, %s)",
                (d_coll, a.acc))
    cur.execute("INSERT INTO filter_presets (account_id, name, filter_spec) VALUES (%s, 'p', '{}')",
                (a.acc,))
    cur.execute("INSERT INTO notification_subscriptions (account_id, name, filter_spec) "
                "VALUES (%s, 's', '{}')", (a.acc,))
    cur.execute(
        "INSERT INTO notification_dispatches (source_kind, collection_id, change_kind, account_id, "
        "actor_account_id, event_ref) VALUES ('share', %s, 'shared_collection', %s, %s, NULL), "
        "('share', NULL, 'shared_pipeline', %s, %s, 'edit')",
        (d_coll, a.acc, d.acc, a.acc, c.acc),
    )
    cur.execute(
        "INSERT INTO estimation_runs (account_id, source, mode, status, input_spec) "
        "VALUES (%s, 'api', 'deterministic', 'success', '{}'::jsonb)", (a.acc,),
    )
    for table in ("entitlements", "filter_presets", "notification_subscriptions",
                  "notification_dispatches", "estimation_runs"):
        assert _owned(cur, table, a.acc) >= 1, f"the seed for {table} is missing"
        for method in ("GET", "POST"):
            assert _rows(cur, c, f"SELECT count(*) FROM {table} WHERE account_id = %s", (a.acc,),
                         act_as=a.acc, method=method) == [(0,)], f"C acting read A's {table}"
    for table in ("collection_shares", "pipeline_shares"):
        assert _rows(cur, c, f"SELECT count(*) FROM {table}", act_as=a.acc) == [(0,)], table
        assert _rows(cur, a, f"SELECT count(*) FROM {table}", act_as=a.acc) == [(0,)], table
    # A, as itself, reads D's shared collection; C acting as A does not.
    assert _rows(cur, a, "SELECT id FROM collections WHERE id = %s", (d_coll,)) == [(d_coll,)]
    assert _rows(cur, c, "SELECT id FROM collections WHERE id = %s", (d_coll,), act_as=a.acc) == []
    assert _rows(cur, c, "SELECT 1 FROM collection_properties WHERE collection_id = %s", (d_coll,),
                 act_as=a.acc) == []
    # What a third party shared with the DELEGATE stays out of the acting view (spec §3.6): D shares
    # a collection with C, holding a property D wrote a shared note (with a file) on.
    dc_coll = _one(cur, "INSERT INTO collections (account_id, name) VALUES (%s, %s) RETURNING id",
                   (d.acc, f"pshare-{uuid.uuid4().hex[:12]}"))
    cur.execute("INSERT INTO collection_properties (collection_id, property_id) VALUES (%s, %s)",
                (dc_coll, world.spare))
    d_note = _one(cur, "INSERT INTO property_notes (account_id, property_id, body, shared) "
                  "VALUES (%s, %s, 'd-shared', true) RETURNING id", (d.acc, world.spare))
    cur.execute(
        "INSERT INTO property_note_attachments (note_id, account_id, storage_key, filename, "
        "mime_type, byte_size, sha256_hex) VALUES (%s, %s, 'k', 'd.pdf', 'application/pdf', 1, %s)",
        (d_note, d.acc, "1" * 64),
    )
    cur.execute("INSERT INTO collection_shares (collection_id, recipient_account_id) VALUES (%s, %s)",
                (dc_coll, c.acc))
    d_rows = (
        ("SELECT count(*) FROM collections WHERE id = %s", (dc_coll,)),
        ("SELECT count(*) FROM collection_properties WHERE collection_id = %s", (dc_coll,)),
        ("SELECT count(*) FROM property_notes WHERE id = %s", (d_note,)),
        ("SELECT count(*) FROM property_note_attachments WHERE note_id = %s", (d_note,)),
    )
    for sql, params in d_rows:
        assert _rows(cur, c, sql, params) == [(1,)], f"the D->C share seed is not visible: {sql}"
        assert _rows(cur, c, sql, params, act_as=a.acc) == [(0,)], f"leaked while acting: {sql}"
    assert _rows(cur, a, "SELECT count(*) FROM notification_dispatches WHERE source_kind = 'share'") == [
        (2,)
    ]


def test_a_header_without_a_share_reads_nothing_not_the_callers_own(
    cur: Any, world: _World,  # noqa: F811
) -> None:
    a, d = world.a, world.d
    assert _rows(cur, d, "SELECT count(*) FROM pipeline_stages")[0][0] > 0
    for act_as in (a.acc, uuid.uuid4()):
        for table in CURATION:
            assert _rows(cur, d, f"SELECT count(*) FROM {table}", act_as=act_as) == [(0,)], table
        for view in BOARD_VIEWS:
            assert _rows(cur, d, f"SELECT count(*) FROM {view}", act_as=act_as) == [(0,)], view
        assert _rows(cur, d, "SELECT count(*) FROM acting_account_ids()", act_as=act_as) == [(0,)]


def test_a_revoke_denies_on_the_next_statement(cur: Any, world: _World) -> None:  # noqa: F811
    a, c = world.a, world.c
    assert _rows(cur, c, "SELECT count(*) FROM pipeline_stages", act_as=a.acc) == [
        (_owned(cur, "pipeline_stages", a.acc),)
    ]
    cur.execute("DELETE FROM pipeline_shares WHERE account_id = %s AND recipient_account_id = %s",
                (a.acc, c.acc))
    for table in CURATION:
        assert _rows(cur, c, f"SELECT count(*) FROM {table}", act_as=a.acc) == [(0,)], table
    assert _touched(cur, c, "UPDATE property_pipeline SET stage_id = %s WHERE property_id = %s",
                    (world.second, world.card), act_as=a.acc, method="PATCH") == 0


def test_the_label_door_names_both_kinds_counterparties(cur: Any, world: _World) -> None:  # noqa: F811
    a, b, c, d = world.a, world.b, world.c, world.d
    labels = "SELECT account_id, email FROM share_party_emails()"
    assert set(_rows(cur, a, labels)) == {(b.acc, b.email), (c.acc, c.email)}
    assert set(_rows(cur, b, labels)) == {(a.acc, a.email)}
    assert set(_rows(cur, c, labels)) == {(a.acc, a.email)}
    assert _rows(cur, d, labels) == []
    # A collection share joins the same door: B (already a party) stays one row, D appears.
    for recipient in (b, d):
        cur.execute(
            "INSERT INTO collection_shares (collection_id, recipient_account_id) VALUES (%s, %s)",
            (world.coll, recipient.acc),
        )
    assert sorted(_rows(cur, a, labels)) == sorted([(b.acc, b.email), (c.acc, c.email),
                                                    (d.acc, d.email)])
    assert set(_rows(cur, d, labels)) == {(a.acc, a.email)}


def test_the_board_views_read_under_every_tenant_with_and_without_the_header(
    cur: Any, world: _World,  # noqa: F811
) -> None:
    for t in (world.a, world.b, world.c, world.d):
        for act_as in (None, world.a.acc):
            for view in BOARD_VIEWS:
                _rows(cur, t, f"SELECT count(*), max(edited_at) FROM {view}", act_as=act_as)
    assert _rows(cur, world.b, "SELECT property_id FROM pipeline_board_public", act_as=world.a.acc) == [
        (world.card,)
    ]
    assert _rows(cur, world.b, "SELECT property_id FROM pipeline_board_public") == [(world.loose,)]


def test_only_the_api_creates_a_share_and_either_party_ends_it(
    cur: Any, world: _World,  # noqa: F811
) -> None:
    a, b, c, d = world.a, world.b, world.c, world.d
    for t, owner, recipient in ((a, a, d), (b, a, b), (c, c, a), (d, a, d)):
        _refused(cur, t, "INSERT INTO pipeline_shares (account_id, recipient_account_id, mode) "
                 "VALUES (%s, %s, 'edit')", (owner.acc, recipient.acc))
    end = "DELETE FROM pipeline_shares WHERE account_id = %s AND recipient_account_id = %s"
    assert _touched(cur, d, end, (a.acc, c.acc), method="DELETE") == 0
    assert _touched(cur, c, end, (a.acc, c.acc), act_as=a.acc, method="DELETE") == 0
    assert _touched(cur, c, end, (a.acc, c.acc), method="DELETE") == 1
    assert _touched(cur, a, end, (a.acc, b.acc), method="DELETE") == 1
    assert _one(cur, "SELECT count(*) FROM pipeline_shares WHERE account_id = %s", (a.acc,)) == 0


def test_the_api_tenant_transaction_sets_the_guc_shape_acting_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API path (api/tenant_pool.tenant_transaction) sets request.method and, only with an
    X-Act-As, request.headers — the shape PostgREST sets and acting() / acting_account_ids() read.
    No seed: a header naming an account that shared nothing must act on an EMPTY identity."""
    from api import tenant_pool
    from tests._live_property import db_url

    monkeypatch.setenv("TENANT_POOL_DB_URL", db_url())
    claims = {"sub": str(uuid.uuid4()), "role": "authenticated"}
    probe = (
        "SELECT current_setting('request.method', true), acting(), "
        "(SELECT count(*) FROM acting_account_ids())"
    )
    with tenant_pool.tenant_transaction(claims, act_as=None, method="PATCH") as conn:
        with conn.cursor() as c:
            c.execute(probe)
            assert c.fetchone() == ("PATCH", False, 0)
            c.execute("SHOW transaction_read_only")
            assert c.fetchone()[0] == "off"
    owner = uuid.uuid4()
    with tenant_pool.tenant_transaction(claims, act_as=owner, method="GET") as conn:
        with conn.cursor() as c:
            c.execute(probe)
            assert c.fetchone() == ("GET", True, 0)
            c.execute("SELECT current_setting('request.headers', true)::json ->> 'x-act-as'")
            assert c.fetchone()[0] == str(owner)
            # An acting GET is READ ONLY, as PostgREST runs it: a write there fails loudly.
            c.execute("SHOW transaction_read_only")
            assert c.fetchone()[0] == "on"
