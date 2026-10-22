"""Collection sharing, view-only (migrations 610, 613), proven on the replayed schema.

Three tenants: A owns a collection holding one property and shares it with B; C is a
bystander. Every assertion about what a tenant can read or write runs through
`tests/_tenant_scope.scoped` (SET LOCAL ROLE authenticated + the caller's JWT claims, exactly
as api.tenant_pool.tenant_conn scopes a request), so the policies bind as in production.
Setup and read-backs run as the owner login, which RLS never binds.

Skip behaviour: skips without TEST_DATABASE_URL like its siblings; CI's migrations lane
sets DB_RAILS_REQUIRED=1, so a lane that loses its database goes RED instead of green.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any, NamedTuple

import pytest

from tests._tenant_scope import scoped

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_REQUIRED = os.environ.get("DB_RAILS_REQUIRED") == "1"

pytestmark = pytest.mark.skipif(
    not _DB_URL and not _REQUIRED,
    reason="TEST_DATABASE_URL not set — this rail runs in CI's migrations lane",
)


class _Tenant(NamedTuple):
    acc: uuid.UUID
    user: uuid.UUID
    email: str


class _World(NamedTuple):
    coll: int
    member: int
    outside: int


@pytest.fixture(scope="module")
def svc() -> Iterator[Any]:
    import psycopg

    conn = psycopg.connect(_DB_URL, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="module")
def tenants(svc: Any) -> Iterator[dict[str, _Tenant]]:
    """Three accounts, each with one confirmed auth user. The on-signup trigger is
    disabled around the auth.users insert (it would mint its own accounts and race for
    the legacy-backfill claim), as in tests/test_tenant_isolation_live.py."""
    made: dict[str, _Tenant] = {}
    with svc.cursor() as cur:
        for key in ("a", "b", "c"):
            user = uuid.uuid4()
            cur.execute(
                "INSERT INTO accounts (kind, name) VALUES ('personal', %s) RETURNING id",
                (f"share-{key}-{user.hex[:8]}",),
            )
            made[key] = _Tenant(cur.fetchone()[0], user, f"share-{key}-{user.hex[:8]}@test.local")
        cur.execute("ALTER TABLE auth.users DISABLE TRIGGER USER")
        try:
            for t in made.values():
                cur.execute(
                    "INSERT INTO auth.users (id, email, email_confirmed_at) VALUES (%s, %s, now())",
                    (t.user, t.email),
                )
        finally:
            cur.execute("ALTER TABLE auth.users ENABLE TRIGGER USER")
        for t in made.values():
            cur.execute(
                "INSERT INTO account_members (account_id, user_id, role) VALUES (%s, %s, 'owner')",
                (t.acc, t.user),
            )
    try:
        yield made
    finally:
        accs = [t.acc for t in made.values()]
        with svc.cursor() as cur:
            cur.execute("DELETE FROM accounts WHERE id = ANY(%s)", (accs,))
            cur.execute(
                "DELETE FROM auth.users WHERE id = ANY(%s)", ([t.user for t in made.values()],)
            )


@pytest.fixture
def world(svc: Any, tenants: dict[str, _Tenant]) -> Iterator[_World]:
    """A's collection holding `member` (not `outside`), shared with B. Fresh per test,
    so every test starts from the same facts whatever the one before it changed."""
    with svc.cursor() as cur:
        cur.execute("INSERT INTO properties DEFAULT VALUES RETURNING id")
        member = cur.fetchone()[0]
        cur.execute("INSERT INTO properties DEFAULT VALUES RETURNING id")
        outside = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO collections (account_id, name) VALUES (%s, %s) RETURNING id",
            (tenants["a"].acc, f"share-{uuid.uuid4().hex[:12]}"),
        )
        coll = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO collection_properties (collection_id, property_id) VALUES (%s, %s)",
            (coll, member),
        )
        cur.execute(
            "INSERT INTO collection_shares (collection_id, recipient_account_id) VALUES (%s, %s)",
            (coll, tenants["b"].acc),
        )
    try:
        yield _World(coll, member, outside)
    finally:
        with svc.cursor() as cur:
            cur.execute(
                "DELETE FROM property_notes WHERE property_id = ANY(%s)", ([member, outside],)
            )
            cur.execute(
                "DELETE FROM collections WHERE account_id = ANY(%s)",
                ([t.acc for t in tenants.values()],),
            )
            cur.execute("DELETE FROM properties WHERE id = ANY(%s)", ([member, outside],))


def _rows(t: _Tenant, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with scoped(t.user) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _touched(t: _Tenant, sql: str, params: tuple[Any, ...]) -> int:
    with scoped(t.user) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def _note(svc: Any, author: _Tenant, prop: int, *, shared: bool) -> int:
    with svc.cursor() as cur:
        cur.execute(
            "INSERT INTO property_notes (account_id, property_id, body, shared) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (author.acc, prop, f"note-{uuid.uuid4().hex[:8]}", shared),
        )
        return cur.fetchone()[0]


def _sees_note(t: _Tenant, note: int) -> bool:
    direct = _rows(t, "SELECT id FROM property_notes WHERE id = %s", (note,))
    via_view = _rows(t, "SELECT id FROM property_notes_public WHERE id = %s", (note,))
    assert bool(direct) == bool(via_view), "property_notes_public must inherit the base policy"
    return bool(direct)


def _add_share(svc: Any, coll: int, recipient: _Tenant) -> None:
    with svc.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_shares (collection_id, recipient_account_id) VALUES (%s, %s)",
            (coll, recipient.acc),
        )


def test_the_recipient_reads_the_shared_collection_its_members_and_their_count(
    tenants: dict[str, _Tenant], world: _World,
) -> None:
    for who in ("a", "b"):
        t = tenants[who]
        assert _rows(t, "SELECT id FROM collections WHERE id = %s", (world.coll,)) == [(world.coll,)]
        assert _rows(
            t, "SELECT property_id FROM collection_properties WHERE collection_id = %s", (world.coll,)
        ) == [(world.member,)]
        assert _rows(
            t, "SELECT count(*) FROM collection_properties_public WHERE collection_id = %s",
            (world.coll,),
        ) == [(1,)]


def test_a_bystander_reads_nothing_of_the_shared_collection(
    tenants: dict[str, _Tenant], world: _World,
) -> None:
    c = tenants["c"]
    assert _rows(c, "SELECT id FROM collections WHERE id = %s", (world.coll,)) == []
    assert _rows(c, "SELECT 1 FROM collection_properties WHERE collection_id = %s", (world.coll,)) == []
    assert _rows(
        c, "SELECT count(*) FROM collection_properties_public WHERE collection_id = %s", (world.coll,)
    ) == [(0,)]
    assert _rows(c, "SELECT 1 FROM collection_shares WHERE collection_id = %s", (world.coll,)) == []


def test_the_recipient_cannot_rename_or_delete_the_collection(
    svc: Any, tenants: dict[str, _Tenant], world: _World,
) -> None:
    b = tenants["b"]
    assert _touched(b, "UPDATE collections SET name = 'taken' WHERE id = %s", (world.coll,)) == 0
    assert _touched(b, "DELETE FROM collections WHERE id = %s", (world.coll,)) == 0
    assert _touched(
        b, "DELETE FROM collection_properties WHERE collection_id = %s", (world.coll,)
    ) == 0
    with svc.cursor() as cur:
        cur.execute(
            "SELECT c.name, (SELECT count(*) FROM collection_properties cp "
            "                WHERE cp.collection_id = c.id) FROM collections c WHERE c.id = %s",
            (world.coll,),
        )
        name, members = cur.fetchone()
    assert name != "taken" and members == 1


def test_the_recipient_cannot_add_a_member_because_the_trigger_stamps_the_owner(
    svc: Any, tenants: dict[str, _Tenant], world: _World,
) -> None:
    import psycopg

    b = tenants["b"]
    # The trigger's parent lookup runs as the invoker: for B it now finds the OWNER's row.
    assert _rows(b, "SELECT account_id FROM collections WHERE id = %s", (world.coll,)) == [
        (tenants["a"].acc,)
    ]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _touched(
            b, "INSERT INTO collection_properties (collection_id, property_id) VALUES (%s, %s)",
            (world.coll, world.outside),
        )
    with svc.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM collection_properties WHERE collection_id = %s AND property_id = %s",
            (world.coll, world.outside),
        )
        assert cur.fetchone()[0] == 0


def test_no_browser_role_can_create_a_share(tenants: dict[str, _Tenant], world: _World) -> None:
    import psycopg

    for who in ("a", "c"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _touched(
                tenants[who],
                "INSERT INTO collection_shares (collection_id, recipient_account_id) VALUES (%s, %s)",
                (world.coll, tenants["c"].acc if who == "a" else tenants["a"].acc),
            )


def test_a_tenant_sees_only_the_shares_it_is_party_to(
    svc: Any, tenants: dict[str, _Tenant], world: _World,
) -> None:
    a, b, c = tenants["a"], tenants["b"], tenants["c"]
    with svc.cursor() as cur:
        cur.execute(
            "INSERT INTO collections (account_id, name) VALUES (%s, %s) RETURNING id",
            (a.acc, f"share-{uuid.uuid4().hex[:12]}"),
        )
        other = cur.fetchone()[0]
    _add_share(svc, other, c)
    shares = "SELECT collection_id, recipient_account_id FROM collection_shares ORDER BY 1"
    assert _rows(b, shares) == [(world.coll, b.acc)]
    assert _rows(c, shares) == [(other, c.acc)]
    assert _rows(a, shares) == [(world.coll, b.acc), (other, c.acc)]
    assert _rows(b, "SELECT id FROM collections WHERE id = %s", (other,)) == []


def test_the_recipient_can_leave_and_a_bystander_cannot_end_the_share(
    tenants: dict[str, _Tenant], world: _World,
) -> None:
    b, c = tenants["b"], tenants["c"]
    leave = "DELETE FROM collection_shares WHERE collection_id = %s AND recipient_account_id = %s"
    assert _touched(c, leave, (world.coll, b.acc)) == 0
    assert _touched(b, leave, (world.coll, b.acc)) == 1
    assert _rows(b, "SELECT id FROM collections WHERE id = %s", (world.coll,)) == []
    assert _rows(b, "SELECT 1 FROM collection_properties WHERE collection_id = %s", (world.coll,)) == []


def test_the_owner_can_revoke_the_share(tenants: dict[str, _Tenant], world: _World) -> None:
    a, b = tenants["a"], tenants["b"]
    revoke = "DELETE FROM collection_shares WHERE collection_id = %s AND recipient_account_id = %s"
    assert _touched(a, revoke, (world.coll, b.acc)) == 1
    assert _rows(b, "SELECT id FROM collections WHERE id = %s", (world.coll,)) == []


def test_an_unmarked_note_stays_with_its_author_both_ways(
    svc: Any, tenants: dict[str, _Tenant], world: _World,
) -> None:
    a, b = tenants["a"], tenants["b"]
    by_b = _note(svc, b, world.member, shared=False)
    by_a = _note(svc, a, world.member, shared=False)
    assert _sees_note(b, by_b) and not _sees_note(a, by_b)
    assert _sees_note(a, by_a) and not _sees_note(b, by_a)


def test_a_marked_note_reaches_the_counterparty_and_never_the_bystander(
    svc: Any, tenants: dict[str, _Tenant], world: _World,
) -> None:
    a, b, c = tenants["a"], tenants["b"], tenants["c"]
    by_b = _note(svc, b, world.member, shared=True)
    by_a = _note(svc, a, world.member, shared=True)
    assert _sees_note(a, by_b) and not _sees_note(c, by_b)
    assert _sees_note(b, by_a) and not _sees_note(c, by_a)


def test_a_marked_note_on_a_property_outside_the_collection_stays_private(
    svc: Any, tenants: dict[str, _Tenant], world: _World,
) -> None:
    note = _note(svc, tenants["b"], world.outside, shared=True)
    assert not _sees_note(tenants["a"], note)


def test_co_recipients_never_read_each_others_marked_notes(
    svc: Any, tenants: dict[str, _Tenant], world: _World,
) -> None:
    a, b, c = tenants["a"], tenants["b"], tenants["c"]
    _add_share(svc, world.coll, c)
    by_c = _note(svc, c, world.member, shared=True)
    assert _sees_note(a, by_c)
    assert not _sees_note(b, by_c)
    # The labels keep the same boundary: each recipient learns the owner, never the other.
    labels = "SELECT account_id, email FROM share_party_emails()"
    assert set(_rows(b, labels)) == {(a.acc, a.email)}
    assert set(_rows(c, labels)) == {(a.acc, a.email)}
    assert set(_rows(a, labels)) == {(b.acc, b.email), (c.acc, c.email)}


def test_a_share_made_after_the_mark_reaches_the_new_party_both_ways(
    svc: Any, tenants: dict[str, _Tenant], world: _World,
) -> None:
    # Migration 613 (ruling 2026-10-17): a mark is not dated. B, the recipient of A's share,
    # marks a note on a property of a collection B owns; sharing that collection with A later
    # shows it to A. And a note B marked while A's share did not exist reaches A once it does.
    # C, party to neither share, reads neither.
    a, b, c = tenants["a"], tenants["b"], tenants["c"]
    with svc.cursor() as cur:
        cur.execute(
            "INSERT INTO collections (account_id, name) VALUES (%s, %s) RETURNING id",
            (b.acc, f"share-{uuid.uuid4().hex[:12]}"),
        )
        own = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO collection_properties (collection_id, property_id) VALUES (%s, %s)",
            (own, world.outside),
        )
    on_own = _note(svc, b, world.outside, shared=True)
    assert not _sees_note(a, on_own)
    _add_share(svc, own, a)
    assert _sees_note(a, on_own) and not _sees_note(c, on_own)

    with svc.cursor() as cur:
        cur.execute(
            "DELETE FROM collection_shares WHERE collection_id = %s AND recipient_account_id = %s",
            (world.coll, b.acc),
        )
    on_member = _note(svc, b, world.member, shared=True)
    assert not _sees_note(a, on_member)
    _add_share(svc, world.coll, b)
    assert _sees_note(a, on_member) and not _sees_note(c, on_member)


def test_a_share_received_after_the_mark_reaches_its_owner(
    svc: Any, tenants: dict[str, _Tenant], world: _World,
) -> None:
    # The bait-share edge, accepted by the 2026-10-17 ruling (spec §7): shares need no
    # acceptance, so C sharing a collection that holds the property with B reads what B marked
    # earlier for A. The mitigations live outside RLS (audience named, share notification,
    # unmark / Leave), so the policy is pinned to the ruled reach, not to the old anchor.
    a, b, c = tenants["a"], tenants["b"], tenants["c"]
    note = _note(svc, b, world.member, shared=True)
    assert _sees_note(a, note) and not _sees_note(c, note)
    with svc.cursor() as cur:
        cur.execute(
            "INSERT INTO collections (account_id, name) VALUES (%s, %s) RETURNING id",
            (c.acc, f"share-{uuid.uuid4().hex[:12]}"),
        )
        later = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO collection_properties (collection_id, property_id) VALUES (%s, %s)",
            (later, world.member),
        )
    _add_share(svc, later, b)
    assert _sees_note(c, note) and _sees_note(a, note)
    assert _touched(b, "UPDATE property_notes SET shared = false WHERE id = %s", (note,)) == 1
    assert not _sees_note(c, note) and not _sees_note(a, note)


def test_an_owners_note_marked_before_the_share_reaches_a_new_recipient(
    svc: Any, tenants: dict[str, _Tenant], world: _World,
) -> None:
    # A mark is not dated (migration 613): an earlier mark reaches a recipient added later.
    a, c = tenants["a"], tenants["c"]
    note = _note(svc, a, world.member, shared=True)
    assert not _sees_note(c, note)
    _add_share(svc, world.coll, c)
    assert _sees_note(c, note)


def test_unmarking_removing_the_property_or_ending_the_share_hides_the_note_again(
    svc: Any, tenants: dict[str, _Tenant], world: _World,
) -> None:
    a, b = tenants["a"], tenants["b"]
    note = _note(svc, b, world.member, shared=True)
    assert _sees_note(a, note)

    assert _touched(b, "UPDATE property_notes SET shared = false WHERE id = %s", (note,)) == 1
    assert not _sees_note(a, note)
    assert _touched(b, "UPDATE property_notes SET shared = true WHERE id = %s", (note,)) == 1
    assert _sees_note(a, note)

    remove = "DELETE FROM collection_properties WHERE collection_id = %s AND property_id = %s"
    assert _touched(a, remove, (world.coll, world.member)) == 1
    assert not _sees_note(a, note)
    with svc.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_properties (collection_id, property_id) VALUES (%s, %s)",
            (world.coll, world.member),
        )
    assert _sees_note(a, note)

    revoke = "DELETE FROM collection_shares WHERE collection_id = %s AND recipient_account_id = %s"
    assert _touched(a, revoke, (world.coll, b.acc)) == 1
    assert not _sees_note(a, note)
    assert _sees_note(b, note)


def test_a_marked_notes_attachment_follows_the_note(
    svc: Any, tenants: dict[str, _Tenant], world: _World,
) -> None:
    import psycopg

    a, b, c = tenants["a"], tenants["b"], tenants["c"]
    note = _note(svc, b, world.member, shared=True)
    with svc.cursor() as cur:
        cur.execute(
            "INSERT INTO property_note_attachments "
            "(note_id, storage_key, filename, mime_type, byte_size, sha256_hex) "
            "VALUES (%s, %s, 'plan.pdf', 'application/pdf', 1, %s) RETURNING id, account_id",
            (note, f"test/{uuid.uuid4().hex}", "0" * 64),
        )
        attachment, stamped = cur.fetchone()
    assert stamped == b.acc
    files = "SELECT id FROM property_note_attachments WHERE id = %s"
    assert _rows(a, files, (attachment,)) == [(attachment,)]
    assert _rows(c, files, (attachment,)) == []
    # Reading is all the counterparty gets: while the note is marked (so A SEES the row) A can
    # neither delete the file nor add one (the trigger stamps B, the author; WITH CHECK refuses).
    assert _touched(a, "DELETE FROM property_note_attachments WHERE id = %s", (attachment,)) == 0
    planted = f"test/{uuid.uuid4().hex}"
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _touched(
            a,
            "INSERT INTO property_note_attachments "
            "(note_id, storage_key, filename, mime_type, byte_size, sha256_hex) "
            "VALUES (%s, %s, 'x.pdf', 'application/pdf', 1, %s)",
            (note, planted, "1" * 64),
        )
    with svc.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM property_note_attachments WHERE id = %s OR storage_key = %s",
            (attachment, planted),
        )
        assert cur.fetchone()[0] == 1
    assert _touched(b, "UPDATE property_notes SET shared = false WHERE id = %s", (note,)) == 1
    assert _rows(a, files, (attachment,)) == []


def test_the_owner_cannot_edit_or_delete_the_recipients_shared_note(
    svc: Any, tenants: dict[str, _Tenant], world: _World,
) -> None:
    a = tenants["a"]
    note = _note(svc, tenants["b"], world.member, shared=True)
    assert _sees_note(a, note)
    assert _touched(a, "UPDATE property_notes SET body = 'edited' WHERE id = %s", (note,)) == 0
    assert _touched(a, "UPDATE property_notes SET shared = false WHERE id = %s", (note,)) == 0
    assert _touched(a, "DELETE FROM property_notes WHERE id = %s", (note,)) == 0
    with svc.cursor() as cur:
        cur.execute("SELECT body, shared FROM property_notes WHERE id = %s", (note,))
        body, shared = cur.fetchone()
    assert body != "edited" and shared is True


def test_share_party_emails_names_exactly_each_callers_counterparties(
    tenants: dict[str, _Tenant], world: _World,
) -> None:
    a, b, c = tenants["a"], tenants["b"], tenants["c"]
    expected = {"a": {(b.acc, b.email)}, "b": {(a.acc, a.email)}, "c": set()}
    for who, t in tenants.items():
        labels = set(_rows(t, "SELECT account_id, email FROM share_party_emails()"))
        assert labels == expected[who], who
        visible = _rows(t, "SELECT account_id, recipient_account_id FROM collection_shares")
        parties = {acc for row in visible for acc in row} - {t.acc}
        assert {acc for acc, _ in labels} == parties, f"{who}: labels drifted from the party rule"


def test_the_label_functions_are_closed_to_browser_roles(
    svc: Any, tenants: dict[str, _Tenant],
) -> None:
    import psycopg

    with svc.cursor() as cur:
        cur.execute(
            "SELECT has_function_privilege('anon', 'public.share_party_emails()', 'EXECUTE'), "
            "       has_function_privilege('anon', 'public.account_email(uuid)', 'EXECUTE'), "
            "       has_function_privilege('authenticated', 'public.account_email(uuid)', 'EXECUTE'), "
            "       has_function_privilege('authenticated', 'public.share_party_emails()', 'EXECUTE')"
        )
        assert cur.fetchone() == (False, False, False, True)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _rows(tenants["b"], "SELECT account_email(%s)", (tenants["a"].acc,))


def test_every_shared_surface_reads_without_policy_recursion(
    tenants: dict[str, _Tenant], world: _World,
) -> None:
    for t in tenants.values():
        for rel in (
            "collections", "collection_properties", "collection_properties_public",
            "property_notes", "property_notes_public", "property_note_attachments",
            "collection_shares",
        ):
            _rows(t, f"SELECT count(*) FROM {rel}")  # noqa: S608 - fixed relation list


def test_a_share_notification_is_stamped_to_the_recipient_and_read_by_them_only(
    svc: Any, tenants: dict[str, _Tenant], world: _World,
) -> None:
    import psycopg

    a, b, c = tenants["a"], tenants["b"], tenants["c"]
    with svc.cursor() as cur:
        cur.execute(
            "INSERT INTO notification_dispatches "
            "(source_kind, collection_id, change_kind, account_id, actor_account_id) "
            "VALUES ('share', %s, 'shared_collection', %s, %s) "
            "RETURNING id, account_id",
            (world.coll, b.acc, a.acc),
        )
        did, stamped = cur.fetchone()
        assert stamped == b.acc
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "INSERT INTO notification_dispatches "
                "(source_kind, collection_id, change_kind, event_ref) "
                "VALUES ('share', %s, 'shared_collection', 'unstamped')",
                (world.coll,),
            )
    read = "SELECT account_id FROM notification_dispatches WHERE id = %s"
    assert _rows(b, read, (did,)) == [(b.acc,)]
    assert _rows(a, read, (did,)) == []
    assert _rows(c, read, (did,)) == []
    # The bell's sentence comes from the actor and the live name (migration 656 dropped `message`),
    # both read as the recipient: the sharer's email through share_party_emails(), the collection's
    # name through the share's read policy.
    from api.notifications import _fetch_dispatch

    with svc.cursor() as cur:
        cur.execute("SELECT name FROM collections WHERE id = %s", (world.coll,))
        name = cur.fetchone()[0]
    with scoped(b.user) as conn:
        row = _fetch_dispatch(conn, str(did))
    assert row is not None
    assert (row["actor_email"], row["share_name"], row["share_kind"], row["share_mode"]) == (
        a.email, name, "collection", None)


def test_a_revoke_drops_the_sharers_address_and_the_collections_name_from_the_bell(
    svc: Any, tenants: dict[str, _Tenant], world: _World,
) -> None:
    """R11 (migration 656: no text is stored): while the share stands the recipient's row names the sharer and
    the collection; once the owner revokes it, no share joins the two accounts and the recipient cannot read the
    collection, so both read NULL and the row itself stays the recipient's. (share_party_emails()' ledger arm
    would keep the address only if the sharer stood in the recipient's own delegated_edits; nothing here does.)"""
    from api import shares
    from api.notifications import _fetch_dispatch

    a, b = tenants["a"], tenants["b"]
    with svc.cursor() as cur:
        cur.execute(shares._INSERT_SHARE_DISPATCH_SQL,
                    shares._share_dispatch("shared_collection", world.coll, a.acc, b.acc, None))
        cur.execute("SELECT id FROM notification_dispatches WHERE collection_id = %s AND account_id = %s",
                    (world.coll, b.acc))
        did = cur.fetchone()[0]
        cur.execute("SELECT name FROM collections WHERE id = %s", (world.coll,))
        name = cur.fetchone()[0]

    def labels() -> tuple[Any, ...]:
        with scoped(b.user) as conn:
            row = _fetch_dispatch(conn, str(did))
        assert row is not None, "the recipient lost its own notification"
        return row["actor_email"], row["share_name"], row["share_kind"]

    assert labels() == (a.email, name, "collection")
    with scoped(a.user, method="DELETE") as conn:
        assert shares.revoke_collection(conn, world.coll, str(b.acc)) == {"deleted": True}
    assert labels() == (None, None, "collection")
