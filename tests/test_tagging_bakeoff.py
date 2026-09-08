"""The tagging bake-off lane, offline: no database, no network, no pod, no dollar.

Four things are worth a test here, and they are the four that are expensive to get wrong
and invisible at review time:

  * the manifest composes ONLY sanctioned readers — a fake connection proves the image
    set is built from `machine_labeling.training_rows` plus a `tag_exam_members` read,
    never a hand-rolled `image_tag_labels` query;
  * arm-preset expansion and patch snapping — an arm recorded at a resolution it did not
    run at poisons every comparison downstream;
  * the skip path — a gated arm with no resolvable revision must be SKIPPED, never
    loaded unpinned;
  * stage routing — the embed stage must be the only one that can reach RunPod.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import pytest

from scripts import tagging_bakeoff_arms as arms_mod
from scripts import tagging_bakeoff_dispatch as dispatch
from scripts import tagging_bakeoff_embed as embed
from scripts import tagging_bakeoff_manifest as manifest


# --------------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(sql.split()), params))
        self._rows = list(self._conn.answer(sql, params))

    def executemany(self, sql, params):
        self._conn.executed.append((" ".join(sql.split()), list(params)))
        self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Answers by matching a substring of the statement. Anything unmatched returns no
    rows, so a query nobody taught it about fails loudly rather than quietly."""

    def __init__(self, answers: dict[str, list]):
        self._answers = answers
        self.executed: list[tuple[str, object]] = []

    def cursor(self):
        return _FakeCursor(self)

    def answer(self, sql: str, params):
        flat = " ".join(sql.split())
        for needle, rows in self._answers.items():
            if needle in flat:
                return rows(params) if callable(rows) else rows
        return []


# --------------------------------------------------------------------------------
# Arms
# --------------------------------------------------------------------------------

def test_default_preset_is_the_run_the_operator_asked_for():
    arms = arms_mod.default_arms()
    names = [a.name for a in arms]
    for resolution in (512, 768, 1024):
        for dtype in ("fp32", "bf16"):
            assert f"dinov3-b16@{resolution}/{dtype}" in names
    assert "dinov3-l16@512/bf16" in names
    assert "dinov2-l14-reg@504/bf16" in names
    assert any(n.startswith("siglip2-b16@") for n in names)
    assert "clip-b32-laion@224/fp32" in names
    assert arms_mod.STORED_CLIP_ARM in names
    # One arm per name; a duplicate would mean two rows fighting over one unique key.
    assert len(names) == len(set(names))


def test_every_arm_carries_all_seven_identity_facts():
    for arm in arms_mod.default_arms():
        identity = arm.identity(revision="deadbeef")
        for field in ("model", "revision", "library", "pooling", "resolution",
                      "preprocessing", "dtype"):
            assert identity[field] not in (None, ""), f"{arm.name} has no {field}"


def test_dino_arms_speak_the_production_config_vocabulary():
    # The winner's seven facts are copied into data/dinov3_config.json verbatim, so the
    # pooling label has to be the one scraper/dinov3_tagger.py already implements.
    from scraper import dinov3_tagger

    for arm in arms_mod.default_arms():
        if not arm.model.startswith("facebook/dino"):
            continue
        assert arm.pooling in dinov3_tagger.POOLING_MODES
        assert arm.preprocessing in dinov3_tagger.PREPROCESSING_MODES
        assert arm.dtype in dinov3_tagger.DTYPES


def test_resolution_snaps_to_the_patch_grid():
    assert arms_mod.snap_to_patch(512, 16) == 512
    assert arms_mod.snap_to_patch(512, 14) == 504   # DINOv2's patch 14 does not divide 512
    assert arms_mod.snap_to_patch(1024, 16) == 1024
    assert arms_mod.snap_to_patch(10, 16) == 16     # never below one patch


def test_default_arms_need_no_snapping_but_record_the_effective_value():
    for arm in arms_mod.default_arms():
        assert arm.effective_resolution == arm.resolution, arm.name
        assert arm.identity(revision="x")["resolution"] == arm.effective_resolution


def test_a_snapped_arm_is_renamed_to_what_it_actually_runs():
    arm = arms_mod.Arm(name="dinov2-l14@512/bf16", model="m", library="transformers",
                       pooling="cls", resolution=512, preprocessing="letterbox_pad",
                       dtype="bf16", patch=14)
    assert arms_mod.renamed_to_effective(arm).name == "dinov2-l14@504/bf16"
    assert arms_mod.renamed_to_effective(arm).effective_resolution == 504


def test_select_arms_narrows_and_rejects_typos():
    arms = arms_mod.default_arms()
    picked = arms_mod.select_arms(arms, ["clip-b32-stored", "dinov3-b16@512/bf16"])
    assert [a.name for a in picked] == ["dinov3-b16@512/bf16", "clip-b32-stored"]
    assert arms_mod.select_arms(arms, []) == arms
    with pytest.raises(ValueError, match="unknown arm"):
        arms_mod.select_arms(arms, ["dinov3-b16@513/bf16"])


class _Resp:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_hub_sha_reads_the_live_commit_and_never_invents_one():
    got = arms_mod.hub_sha("facebook/x", get=lambda *a, **k: _Resp(200, {"sha": "abc"}))
    assert got == "abc"
    with pytest.raises(PermissionError):
        arms_mod.hub_sha("facebook/x", get=lambda *a, **k: _Resp(401))
    with pytest.raises(LookupError):
        arms_mod.hub_sha("facebook/x", get=lambda *a, **k: _Resp(404))
    with pytest.raises(RuntimeError):
        arms_mod.hub_sha("facebook/x", get=lambda *a, **k: _Resp(200, {}))


def test_siglip_checkpoint_prefers_the_largest_that_exists():
    def _get(url, **kwargs):
        return _Resp(200 if url.endswith("-512") else 404, {"sha": "s"})

    assert arms_mod.resolve_siglip_checkpoint(get=_get) == (
        "google/siglip2-base-patch16-512", 512)

    # Every probe failing degrades to the defensible fallback, never to no control arm.
    assert arms_mod.resolve_siglip_checkpoint(get=lambda *a, **k: _Resp(404)) == (
        arms_mod.SIGLIP2_CANDIDATES[-1])


# --------------------------------------------------------------------------------
# Manifest selection
# --------------------------------------------------------------------------------

def test_head_selection_is_the_operator_ready_flag(monkeypatch):
    # Only tag 1 is ready. Tag 2 is active and far better labelled, and stays out
    # anyway: the operator's flag is the decision, the counts are not.
    conn = _FakeConn({"WHERE active AND review_state = 'ready'": [(1, "kuchyne")]})
    rows = {
        1: [(10, "positive")] * 3 + [(20, "negative")],
        2: [(30, "positive")] * 500,
    }
    seen: list[int] = []

    def _training_rows(_conn, *, tag_id, **kwargs):
        seen.append(tag_id)
        return rows[tag_id]

    monkeypatch.setattr(manifest.machine_labeling, "training_rows", _training_rows)
    heads = manifest.select_heads(conn)

    assert seen == [1]                          # the ONE door, asked per selected head
    assert [h["tag_id"] for h in heads] == [1]
    assert heads[0]["positives"] == 3 and heads[0]["negatives"] == 1
    assert heads[0]["image_ids"] == [10, 20]    # distinct, both states
    # The selector reads the vocabulary and its flag; no label table.
    assert all("image_tag_labels" not in sql for sql, _ in conn.executed)


def test_a_ready_head_with_no_training_rows_is_still_selected(monkeypatch):
    conn = _FakeConn({"WHERE active AND review_state = 'ready'": [(7, "sklep")]})
    monkeypatch.setattr(manifest.machine_labeling, "training_rows", lambda *a, **k: [])
    heads = manifest.select_heads(conn)
    # It enters the run and fails at training time with a recorded reason; a count
    # must never quietly drop a head the operator marked ready.
    assert [h["tag_id"] for h in heads] == [7]
    assert heads[0]["positives"] == 0 and heads[0]["negatives"] == 0


def test_explicit_heads_override_the_ready_flag(monkeypatch):
    conn = _FakeConn({"WHERE id = ANY": [(2, "garaz")]})
    monkeypatch.setattr(manifest.machine_labeling, "training_rows",
                        lambda *a, **k: [(30, "positive")])
    heads = manifest.select_heads(conn, head_ids=[2])
    assert [h["tag_id"] for h in heads] == [2]
    assert all("review_state" not in sql for sql, _ in conn.executed)


def test_the_run_note_carries_the_counts_as_information():
    census = manifest.head_census([
        {"tag_id": 1, "label": "kuchyne", "positives": 231, "negatives": 1004},
        {"tag_id": 2, "label": "garaz", "positives": 12, "negatives": 900},
    ])
    assert census == "kuchyne(1) 231+/1004-; garaz(2) 12+/900-"


def test_the_manifest_module_writes_no_sql_naming_image_tag_labels():
    # The clean path out of tests/test_holdout_exclusion_census.py is to never write one.
    from pathlib import Path

    source = Path(manifest.__file__).read_text()
    body = source.split('"""', 2)[-1]  # the module docstring names it as prose
    assert "image_tag_labels" not in body


def test_exam_images_arrive_through_membership_not_labels():
    conn = _FakeConn({"FROM tag_exam_members": [(101,), (100,)]})
    assert manifest.holdout_image_ids(conn) == [100, 101]
    statement = conn.executed[0][0]
    assert "image_tag_labels" not in statement
    assert "purpose = 'holdout'" in statement


def test_only_images_whose_bytes_we_hold_reach_the_manifest():
    conn = _FakeConn({"FROM images i": [(1, "a/b.jpg"), (3, "c/d.jpg")]})
    assert manifest.stored_images(conn, [1, 2, 3]) == {1: "a/b.jpg", 3: "c/d.jpg"}
    assert "storage_path IS NOT NULL" in conn.executed[0][0]


def test_stored_clip_copy_is_scoped_to_the_incumbent_model_and_this_run():
    conn = _FakeConn({"count(*)": [(7,)]})
    assert manifest.copy_stored_clip(conn, arm_id=5, image_ids=[1, 2]) == 7
    insert, params = conn.executed[0]
    assert "INSERT INTO dedup_sim.tag_head_bakeoff_vectors" in insert
    assert "ON CONFLICT DO NOTHING" in insert
    assert params["model"] == arms_mod.STORED_CLIP_MODEL
    assert params["arm_id"] == 5


def test_manifest_document_is_self_contained_and_leaks_no_answers():
    arms = arms_mod.select_arms(arms_mod.default_arms(), ["clip-b32-stored"])
    heads = [{"tag_id": 1, "label": "kuchyne", "positives": 200, "negatives": 300,
              "image_ids": [1, 2]}]
    doc = manifest.manifest_document(
        run_id=9, label="grid", heads=heads, arms=arms,
        urls={1: {"key": "k", "url": "https://signed"}}, exam_ids=[42], expires_in=60)
    assert doc["run_id"] == 9 and doc["images"]["1"]["url"] == "https://signed"
    assert doc["exam_image_ids"] == [42]
    # Head sizes travel (the pod logs them); the image_ids behind them do not need to.
    assert "image_ids" not in doc["heads"][0]
    json.dumps(doc)  # must survive the round trip to R2


# --------------------------------------------------------------------------------
# Embedder
# --------------------------------------------------------------------------------

def _arm_row(**overrides):
    row = {"id": 1, "arm": "dinov3-b16@512/bf16",
           "model": "facebook/dinov3-vitb16-pretrain-lvd1689m", "revision": "",
           "library": "transformers", "pooling": "cls", "resolution": 512,
           "preprocessing": "letterbox_pad", "dtype": "bf16", "status": "pending"}
    row.update(overrides)
    return row


def test_pending_arms_skips_the_zero_gpu_arm_and_finished_work():
    rows = [
        _arm_row(id=1, arm="a", status="pending"),
        _arm_row(id=2, arm="b", status="ok"),
        _arm_row(id=3, arm=arms_mod.STORED_CLIP_ARM, status="pending"),
        _arm_row(id=4, arm="d", status="failed"),
        _arm_row(id=5, arm="e", status="skipped"),
    ]
    assert [a["arm"] for a in embed.pending_arms(rows)] == ["a", "d"]
    assert [a["arm"] for a in embed.pending_arms(rows, only=["d"])] == ["d"]
    # Naming an arm overrides its status — the retry affordance for a skipped arm once
    # the token is fixed. The stored arm stays excluded whatever anyone asks for.
    assert [a["arm"] for a in embed.pending_arms(rows, only=["e", "b"])] == ["b", "e"]
    assert embed.pending_arms(rows, only=[arms_mod.STORED_CLIP_ARM]) == []


def test_an_unresolvable_revision_skips_the_arm_and_loads_nothing(monkeypatch):
    conn = _FakeConn({})
    monkeypatch.setattr(embed.arms_mod, "hub_sha",
                        lambda *a, **k: (_ for _ in ()).throw(PermissionError("401")))
    monkeypatch.setattr(embed, "load_encoder", lambda *a, **k: pytest.fail(
        "a gated arm with no revision must never reach the loader"))
    status, written, note, dim = embed.embed_arm(
        conn, _arm_row(), paths={1: "/tmp/x"}, device="cpu", batch_size=8, deadline=None)
    assert (status, written, dim) == ("skipped", 0, None)
    assert "revision unresolved" in note
    assert "SET status = 'skipped'" in conn.executed[0][0]


def test_resumability_skips_images_the_arm_already_holds(monkeypatch):
    conn = _FakeConn({"FROM dedup_sim.tag_head_bakeoff_vectors": [(1,), (2,)]})
    monkeypatch.setattr(embed.arms_mod, "hub_sha", lambda *a, **k: "sha1234567890")
    monkeypatch.setattr(embed, "load_encoder", lambda *a, **k: pytest.fail(
        "an arm with nothing left to do must not load a model"))
    status, written, note, _dim = embed.embed_arm(
        conn, _arm_row(), paths={1: "a", 2: "b"}, device="cpu", batch_size=8,
        deadline=None)
    assert (status, written) == ("ok", 0)
    assert "already complete" in note


class _FakeVectors:
    def __init__(self, rows):
        self._rows = rows
        self.shape = (len(rows), len(rows[0]))

    def __getitem__(self, i):
        return _FakeRow(self._rows[i])


class _FakeRow:
    def __init__(self, values):
        self._values = values

    def tolist(self):
        return list(self._values)


def test_vectors_are_written_in_pgvector_text_form_with_do_nothing(monkeypatch, tmp_path):
    from PIL import Image

    for image_id in (1, 2):
        Image.new("RGB", (8, 6)).save(tmp_path / f"{image_id}.png")
    conn = _FakeConn({})
    monkeypatch.setattr(embed.arms_mod, "hub_sha", lambda *a, **k: "sha1234567890")

    class _Encoder:
        def embed(self, images, batch_size):
            return _FakeVectors([[0.5, -0.5], [0.25, 0.75]][:len(images)])

    monkeypatch.setattr(embed, "load_encoder", lambda *a, **k: _Encoder())
    status, written, _note, dim = embed.embed_arm(
        conn, _arm_row(),
        paths={1: str(tmp_path / "1.png"), 2: str(tmp_path / "2.png")},
        device="cpu", batch_size=8, deadline=None)

    assert (status, written, dim) == ("ok", 2, 2)
    insert = [e for e in conn.executed if "INSERT INTO" in e[0]][0]
    assert "ON CONFLICT DO NOTHING" in insert[0] and "::halfvec" in insert[0]
    assert insert[1] == [(1, 1, "[0.500000,-0.500000]"), (1, 2, "[0.250000,0.750000]")]


def test_the_arm_heartbeat_is_written_with_the_vectors_it_reports(monkeypatch, tmp_path):
    """A note that says "2 vectors" is a note whose 2 vectors are already committed —
    same connection, same batch boundary. It is what the dispatcher's watchdog reads to
    decide the pod is still earning its rent."""
    from PIL import Image

    for image_id in (1, 2):
        Image.new("RGB", (8, 6)).save(tmp_path / f"{image_id}.png")
    conn = _FakeConn({})
    monkeypatch.setattr(embed.arms_mod, "hub_sha", lambda *a, **k: "sha1234567890")

    class _Encoder:
        def embed(self, images, batch_size):
            return _FakeVectors([[0.5, -0.5], [0.25, 0.75]][:len(images)])

    monkeypatch.setattr(embed, "load_encoder", lambda *a, **k: _Encoder())
    beats: list[str] = []
    _status, _written, note, _dim = embed.embed_arm(
        conn, _arm_row(),
        paths={1: str(tmp_path / "1.png"), 2: str(tmp_path / "2.png")},
        device="cpu", batch_size=8, deadline=None,
        versions="py3.12.14 torch2.6.0+cu118 transformers4.57.6",
        on_heartbeat=beats.append)

    statements = [e for e in conn.executed if "tag_head_bakeoff_arms" in e[0]]
    heartbeat = [e for e in statements if "SET status = 'running', note" in e[0]][-1]
    assert "2/2 vectors" in heartbeat[1]["note"] and "dim=2" in heartbeat[1]["note"]
    # The insert is committed before the note that claims it.
    assert conn.executed.index(heartbeat) > [i for i, e in enumerate(conn.executed)
                                             if "INSERT INTO" in e[0]][-1]
    # The resolved runtime is recorded on the row the run is judged by — the pod
    # bootstraps its own interpreter and torch, so nothing else knows what ran.
    assert "torch2.6.0+cu118" in heartbeat[1]["note"] and "torch2.6.0+cu118" in note
    assert beats and "2/2 vectors" in beats[-1]


def test_a_running_arm_left_by_a_killed_pod_is_picked_up_again():
    """The 2026-09-08 pod was killed mid-run; a status of `running` must never be read
    as "someone else is on it". The vectors, not the status, are the record of work."""
    rows = [
        _arm_row(id=1, arm="stale-running", status="running"),
        _arm_row(id=2, arm="finished", status="ok"),
    ]
    assert [a["arm"] for a in embed.pending_arms(rows)] == ["stale-running"]


class _CtxConn(_FakeConn):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_payloads_first_write_is_the_boot_heartbeat(monkeypatch):
    """Before the manifest, before the image cache, before a byte of weights. Without
    it the dispatcher cannot tell "the clone or the install failed" from "the weights
    are still downloading" — which is exactly what 2026-09-08 could not tell."""
    import psycopg

    conn = _CtxConn({
        "SELECT id, label, status, manifest_key": [(1, "run one", "running", "key")],
        "SELECT note FROM dedup_sim.tag_head_bakeoff_runs": [("manifest: 10800 images",)],
        "SELECT id, arm, model": [],
    })
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://x")
    assert embed.main(["--run-id=1", "--device=cpu"]) == 0

    writes = [e for e in conn.executed if e[0].startswith("UPDATE")]
    assert writes, "the payload must announce itself before it does anything expensive"
    first = writes[0]
    assert "tag_head_bakeoff_runs SET note" in first[0]
    assert embed.BOOT_PREFIX in first[1]["note"]
    assert "py" in first[1]["note"]                    # the resolved interpreter
    assert "manifest: 10800 images" in first[1]["note"]  # the manifest's note survives


def test_a_second_boot_replaces_the_first_rather_than_stacking():
    """The dispatcher dates the boot stamp against its own launch: two stamps, or an
    old one left in place, would let a previous run vouch for this pod."""
    conn = _FakeConn({"SELECT note FROM dedup_sim.tag_head_bakeoff_runs":
                      [("kept line\npod booted 2026-09-01T00:00:00+00:00 old\n"
                        "pod alive 2026-09-01T00:05:00+00:00 arm x",)]})
    embed.stamp_run(conn, run_id=1, boot="py3.12.14", alive="starting")
    note = [e for e in conn.executed if e[0].startswith("UPDATE")][0][1]["note"]
    assert note.count(embed.BOOT_PREFIX) == 1 and note.count(embed.ALIVE_PREFIX) == 1
    assert "2026-09-01" not in note and "kept line" in note


def test_the_cache_phase_heartbeats_because_it_writes_no_vector(tmp_path):
    class _R:
        content = b"bytes"

        def raise_for_status(self):
            return None

    doc = {"images": {str(i): {"url": f"u{i}"} for i in range(1, 601)}}
    beats: list[tuple[int, int]] = []
    embed.cache_images(doc, cache_dir=str(tmp_path), workers=4,
                       get=lambda url, **kw: _R(),
                       on_progress=lambda done, total: beats.append((done, total)))
    assert beats == [(250, 600), (500, 600)]


def test_the_loader_is_chosen_by_family_not_by_a_stored_class_name():
    assert embed._model_class_for("image_embeds") == "CLIPVisionModelWithProjection"
    assert embed._model_class_for("attention_pool") == "SiglipVisionModel"
    assert embed._model_class_for("cls") == "AutoModel"


def test_the_image_cache_is_filled_once_and_reused(tmp_path):
    calls: list[str] = []

    class _R:
        content = b"bytes"

        def raise_for_status(self):
            return None

    def _get(url, **kwargs):
        calls.append(url)
        return _R()

    doc = {"images": {"1": {"url": "u1"}, "2": {"url": "u2"}}}
    first = embed.cache_images(doc, cache_dir=str(tmp_path), workers=2, get=_get)
    second = embed.cache_images(doc, cache_dir=str(tmp_path), workers=2, get=_get)
    assert first == second and len(first) == 2
    assert len(calls) == 2, "a second pass must read the cache, not re-download"


# --------------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------------

def _args(**overrides) -> argparse.Namespace:
    base = dict(stage="manifest", run_id=0, label="", note="",
                heads="", arms="", batch_size=32, workers=16, job_max_seconds=7200,
                ref="main", image="img", gpu_allowlist="3090", dry_run=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def test_only_the_embed_stage_reaches_a_pod():
    assert dispatch.plan_stage(_args(stage="manifest")).where == "runner"
    assert dispatch.plan_stage(_args(stage="embed", run_id=7)).where == "pod"
    assert dispatch.plan_stage(_args(stage="train", run_id=7)).where == "runner"


def test_embed_and_train_refuse_to_run_without_a_run_id():
    for stage in ("embed", "train"):
        with pytest.raises(ValueError, match="--run-id is required"):
            dispatch.plan_stage(_args(stage=stage, run_id=0))


def test_the_wait_window_is_the_payload_budget_plus_the_startup_grace():
    plan = dispatch.plan_stage(_args(stage="embed", run_id=7, job_max_seconds=3600))
    assert plan.max_wait_s == 3600 + dispatch.STARTUP_GRACE_S
    assert "--max-seconds=3600" in plan.payload_args


def test_a_dry_run_never_launches_and_never_runs_a_foreign_cli():
    assert dispatch.plan_stage(_args(stage="embed", run_id=7, dry_run=True)).execute is False
    assert dispatch.plan_stage(_args(stage="train", run_id=7, dry_run=True)).execute is False
    # Ours does run: its own --dry-run is a reporting mode that writes nothing, and the
    # counts are the entire reason to dispatch this stage dry.
    manifest_plan = dispatch.plan_stage(_args(stage="manifest", dry_run=True))
    assert manifest_plan.execute is True and "--dry-run" in manifest_plan.argv


def test_the_pod_command_carries_no_secret_and_refuses_unsafe_interpolation():
    plan = dispatch.plan_stage(_args(stage="embed", run_id=7,
                                     arms="dinov3-b16@768/bf16,clip-b32-stored"))
    script = plan.start_cmd[-1]
    assert "scripts.tagging_bakeoff_embed" in script and "--run-id=7" in script
    assert "dinov3-b16@768/bf16" in script          # arm names carry @ and / legitimately
    # A VALUE is the leak; the embedded reporter reads SUPABASE_DB_URL by NAME, which is
    # exactly how it avoids carrying one (the value arrives in the REST body's env).
    for leaked in ("SUPABASE_DB_URL=", "HF_TOKEN", "R2_SECRET"):
        assert leaked not in script
    with pytest.raises(ValueError, match="unsafe git ref"):
        dispatch.build_start_cmd(ref="main; rm -rf /", module="m", payload_args=[])
    with pytest.raises(ValueError, match="unsafe payload arg"):
        dispatch.build_start_cmd(ref="main", module="m",
                                 payload_args=["--arms=$(whoami)"])


def test_pod_env_forwards_by_name_only(monkeypatch):
    for key in dispatch.POD_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://secret")
    env = dispatch.pod_env()
    assert env == {"SUPABASE_DB_URL": "postgres://secret"}


def test_the_pod_is_told_which_row_to_report_its_bootstrap_steps_into(monkeypatch):
    # Without this the bootstrap is mute until the payload's first write, which is what
    # made the 2026-09-08 retry undiagnosable (~$0.08 and no cause).
    for key in dispatch.POD_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://secret")
    env = dispatch.pod_env(run_id=7)
    assert env["HEARTBEAT_RUN_ID"] == "7"
    assert "%(note)s" in env["HEARTBEAT_SQL"] and "%(run_id)s" in env["HEARTBEAT_SQL"]
    # It must UPDATE this lane's run row and nothing else, and it must not append
    # without bound: one `pod step` line at a time.
    assert "dedup_sim.tag_head_bakeoff_runs" in env["HEARTBEAT_SQL"]
    assert "regexp_replace" in env["HEARTBEAT_SQL"] and "left(" in env["HEARTBEAT_SQL"]
    # No run id (the manifest/train stages) means no heartbeat wiring at all.
    assert "HEARTBEAT_SQL" not in dispatch.pod_env()


# --- what the watchdog reads ---------------------------------------------------

LAUNCHED = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _progress_conn(*, arms, run_note):
    return _FakeConn({
        "FROM dedup_sim.tag_head_bakeoff_arms a": arms,
        "SELECT note FROM dedup_sim.tag_head_bakeoff_runs": [(run_note,)],
    })


def test_a_stale_boot_stamp_does_not_vouch_for_this_pod():
    # The failure this guards: a re-dispatch reading the PREVIOUS run's boot line, never
    # firing the bootstrap deadline, and paying out the whole window again.
    conn = _progress_conn(arms=[("a", "pending", None, 0)],
                          run_note="pod booted 2026-09-07T09:00:00+00:00 py3.12.14")
    reading = dispatch.read_bakeoff_progress(conn, run_id=1, only=[],
                                             launched_at=LAUNCHED, baseline_vectors=0)
    assert reading.booted is False


def test_a_boot_stamp_from_this_dispatch_counts_as_alive():
    conn = _progress_conn(arms=[("a", "running", None, 0)],
                          run_note="pod booted 2026-09-08T12:00:30+00:00 py3.12.14")
    assert dispatch.read_bakeoff_progress(conn, run_id=1, only=[], launched_at=LAUNCHED,
                                          baseline_vectors=0).booted is True


def test_vectors_above_the_baseline_are_proof_of_life_on_their_own():
    conn = _progress_conn(arms=[("a", "running", None, 900)], run_note=None)
    assert dispatch.read_bakeoff_progress(conn, run_id=1, only=[], launched_at=LAUNCHED,
                                          baseline_vectors=0).booted is True


def test_the_marker_moves_with_a_heartbeat_even_when_no_vector_is_written():
    # Fetching the manifest and filling the ~10.8k image cache writes no vector; without
    # this the stall deadline would tear down a pod that is working.
    early = _progress_conn(arms=[("a", "running", None, 0)],
                           run_note="pod alive 2026-09-08T12:01:00+00:00 caching 250/10800")
    later = _progress_conn(arms=[("a", "running", None, 0)],
                           run_note="pod alive 2026-09-08T12:06:00+00:00 caching 5000/10800")
    assert (dispatch.read_bakeoff_progress(early, run_id=1, only=[], launched_at=LAUNCHED,
                                           baseline_vectors=0).marker
            != dispatch.read_bakeoff_progress(later, run_id=1, only=[],
                                              launched_at=LAUNCHED,
                                              baseline_vectors=0).marker)


def test_terminal_means_every_arm_this_dispatch_asked_for():
    arms = [("a", "ok", None, 5400), ("b", "failed", None, 0),
            (arms_mod.STORED_CLIP_ARM, "pending", None, 0)]
    conn = _progress_conn(arms=arms, run_note=None)
    # The zero-GPU arm is the manifest stage's business and never blocks the teardown.
    assert dispatch.read_bakeoff_progress(conn, run_id=1, only=[], launched_at=LAUNCHED,
                                          baseline_vectors=0).terminal is True

    pending = _progress_conn(arms=[("a", "ok", None, 5400), ("b", "running", None, 10)],
                             run_note=None)
    assert dispatch.read_bakeoff_progress(pending, run_id=1, only=[],
                                          launched_at=LAUNCHED,
                                          baseline_vectors=0).terminal is False
    # Narrowed to one arm, the arms nobody asked for cannot keep the pod alive.
    narrowed = _progress_conn(arms=[("a", "ok", None, 5400), ("b", "pending", None, 0)],
                              run_note=None)
    assert dispatch.read_bakeoff_progress(narrowed, run_id=1, only=["a"],
                                          launched_at=LAUNCHED,
                                          baseline_vectors=0).terminal is True


def test_a_bootstrap_step_is_progress_and_reaches_the_watchdog(monkeypatch):
    # The pod is still installing torch: no vectors, no payload boot stamp — and yet the
    # run is advancing. The step line's own timestamp is what says so.
    step = ('pod step {"ts": "2026-09-08T12:04:00+00:00", "msg": "step=torch ok", '
            '"pod": "bsg9k5ee9y6jcm", "python": "3.10.12"}')
    conn = _progress_conn(arms=[("a", "pending", None, 0)],
                          run_note=f"manifest: 15 heads\n{step}")
    reading = dispatch.read_bakeoff_progress(conn, run_id=1, only=[],
                                             launched_at=LAUNCHED, baseline_vectors=0)
    assert reading.booted is False           # the payload genuinely has not started
    assert "step=torch ok" in reading.step   # but the bootstrap is talking
    assert "2026-09-08T12:04:00" in reading.marker


def test_the_exit_trap_s_error_tail_is_what_the_dispatcher_reads_back():
    # The whole point: the next failure names its own step and ships its own error text.
    step = ('pod step {"ts": "2026-09-08T12:09:00+00:00", "msg": "exit=1 step=repo", '
            '"tail": "ERROR: No matching distribution found for torch"}')
    conn = _progress_conn(arms=[("a", "pending", None, 0)], run_note=step)
    reading = dispatch.read_bakeoff_progress(conn, run_id=1, only=[],
                                             launched_at=LAUNCHED, baseline_vectors=0)
    assert "exit=1 step=repo" in reading.step
    assert "No matching distribution" in reading.step


def test_a_dry_run_proves_the_generated_bootstrap_before_a_pod_is_rented(monkeypatch,
                                                                        caplog):
    # The self-check EXECUTES the script offline (every real step stubbed) and asserts
    # the EXIT trap names a forced failure. A syntax error must not cost a GPU-hour.
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    with caplog.at_level("INFO"):
        rc = dispatch.main(["--stage", "embed", "--run-id", "7", "--dry-run"])
    assert rc == 0
    assert "preflight[clean]: rc=0 OK" in caplog.text
    assert "preflight[torch]: rc=1 OK" in caplog.text
    assert "DRY RUN" in caplog.text


def test_no_database_on_the_runner_means_no_watchdog_and_a_loud_warning(monkeypatch, caplog):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    with caplog.at_level("WARNING"):
        assert dispatch.make_watchdog(_args(stage="embed", run_id=1), only=[]) is None
    assert "no watchdog" in caplog.text


def test_the_train_stage_shells_out_rather_than_importing_the_sibling():
    plan = dispatch.plan_stage(_args(stage="train", run_id=7))
    assert plan.argv[1:4] == ["-m", dispatch.TRAIN_MODULE, "--run-id"]
    from pathlib import Path

    source = Path(dispatch.__file__).read_text()
    assert "import scripts.tag_head_bakeoff" not in source
    assert "from scripts.tag_head_bakeoff" not in source
