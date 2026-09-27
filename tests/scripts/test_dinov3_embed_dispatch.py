"""scripts/dinov3_embed_dispatch.py — the pod plan: credentials reach the pod through
the REST body's `env` (never argv), the wait window covers the payload's own budget,
the GPU is an ordered ladder over both clouds, and the git ref cannot smuggle shell.

Hermetic: a fake RunPodClient and a fake requests.Session. No RunPod call, no pod, no
network, no spend.
"""

from __future__ import annotations

import dataclasses
import sys

import pytest

from scripts import dinov3_embed_dispatch as dispatch
from scripts.runpod_client import GpuOption, RunPodClient, RunPodError

IDENTITY = {
    "model": "facebook/dinov3-vitb16-pretrain-lvd1689m",
    "revision": "a" * 40,
    "library": "transformers",
    "pooling": "cls",
    "resolution": 224,
    "preprocessing": "letterbox_pad",
    "dtype": "bf16",
}


# --- start command -------------------------------------------------------------


def test_start_cmd_runs_the_payload_at_the_pinned_ref():
    cmd = dispatch.build_start_cmd(ref="abc123", backfill_args=["--limit=10"])
    assert cmd[0] == "bash" and cmd[1] == "-c"
    assert "git fetch --depth 1 origin abc123" in cmd[2]
    assert "python -m scripts.dinov3_embed_backfill --limit=10" in cmd[2]


def test_start_cmd_carries_no_secrets():
    # argv is visible in the pod record; credentials go through `env` instead. The
    # embedded step reporter READS SUPABASE_DB_URL by name — a name is not a secret, an
    # assignment would be.
    cmd = dispatch.build_start_cmd(ref="main", backfill_args=["--limit=10"])
    for key in dispatch.POD_ENV_KEYS:
        assert f"{key}=" not in cmd[2]


@pytest.mark.parametrize(
    "ref",
    ["main; rm -rf /", "a b", "$(id)", "`id`", "'x'", 'a"b', "-flag", "", "x" * 300],
)
def test_start_cmd_refuses_an_unsafe_ref(ref):
    with pytest.raises(ValueError):
        dispatch.build_start_cmd(ref=ref, backfill_args=[])


def test_start_cmd_refuses_an_unsafe_payload_arg():
    with pytest.raises(ValueError):
        dispatch.build_start_cmd(ref="main", backfill_args=["--limit=1; curl evil"])


# --- pod environment -----------------------------------------------------------


def test_pod_env_forwards_only_the_keys_that_are_set(monkeypatch):
    for key in dispatch.POD_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://x")
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    assert dispatch.pod_env() == {"SUPABASE_DB_URL": "postgres://x", "HF_TOKEN": "hf_secret"}


def test_pod_env_includes_the_gated_weights_token_and_the_r2_credentials(monkeypatch):
    for key in dispatch.POD_ENV_KEYS:
        monkeypatch.setenv(key, f"value-of-{key}")
    assert set(dispatch.pod_env()) == set(dispatch.POD_ENV_KEYS)
    assert "HF_TOKEN" in dispatch.POD_ENV_KEYS  # facebook/dinov3-* is gated: manual


# --- GPU selection: an ordered ladder over both clouds (B-k, 2026-09-27) ---------
# GitHub run 36316992243 found no COMMUNITY capacity for any card of G1's three-card
# allowlist; this lane allowed two of them, community only, so it would have rented nothing.


class _FakeClient:
    def __init__(self, gpus: list[GpuOption], stop_reason: str | None = None) -> None:
        self._gpus = gpus
        self.jobs: list[dict] = []
        self.stop_reason = stop_reason

    def eligible_gpus(self, *, max_price_per_hr=None, cloud_type="COMMUNITY"):
        return [dataclasses.replace(g, cloud_type=cloud_type) for g in self._gpus]

    def run_job_with_fallback(self, **kwargs):
        self.jobs.append(kwargs)
        return type("R", (), {"pod_id": "pod1", "gpu_type_id": "g", "final_status": "RUNNING",
                              "timed_out": True, "elapsed_s": 1.0, "cost_per_hr": 0.22,
                              "stop_reason": self.stop_reason})()


CATALOG = [
    GpuOption("NVIDIA GeForce RTX 4090", "RTX 4090", 24, 0.20),     # fastest, FEWEST vCPUs (6)
    GpuOption("NVIDIA RTX A5000", "RTX A5000", 24, 0.16),
    GpuOption("NVIDIA GeForce RTX 3090", "RTX 3090", 24, 0.22),
]

_LIVE = [
    # id, displayName, GB, community $/h, secure $/h, in community, in secure
    ("NVIDIA GeForce RTX 3090", "RTX 3090", 24, 0.22, 0.43, True, True),
    ("NVIDIA GeForce RTX 3090 Ti", "RTX 3090 Ti", 24, 0.27, 0.0, True, False),
    ("NVIDIA RTX A5000", "RTX A5000", 24, 0.16, 0.27, True, True),
    ("NVIDIA GeForce RTX 4090", "RTX 4090", 24, 0.34, 0.59, True, True),
    ("NVIDIA L4", "L4", 24, 0.0, 0.43, False, True),
    ("NVIDIA L40S", "L40S", 48, 0.79, 1.09, True, True),
    ("NVIDIA GeForce RTX 3070", "RTX 3070", 8, 0.13, 0.0, True, False),
    ("NVIDIA A100 80GB PCIe", "A100 PCIe", 80, 1.19, 1.64, True, True),
    ("Tesla V100-PCIE-16GB", "Tesla V100", 16, 0.19, 0.0, True, False),
    ("NVIDIA GeForce RTX 5090", "RTX 5090", 32, 0.69, 0.99, True, True),
]


class _Resp:
    def __init__(self, status: int = 200, body=None, text: str = "") -> None:
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def iter_lines(self, decode_unicode: bool = True):
        return iter(())

    def close(self) -> None:
        pass


class _Session:
    """RunPod's two APIs, faked: the GraphQL catalogue and REST pod CRUD. `no_capacity`
    names the (card, cloud) rungs that answer RunPod's real no-instances 500."""

    def __init__(self, no_capacity=(), catalog=_LIVE) -> None:
        self.headers: dict[str, str] = {}
        self.no_capacity = set(no_capacity)
        self.catalog = catalog
        self.launches: list[dict] = []
        self.deleted: list[str] = []

    def post(self, url, json=None, timeout=30):
        if url.endswith("/pods"):
            self.launches.append(json)
            if (json["gpuTypeIds"][0], json["cloudType"]) in self.no_capacity:
                return _Resp(500, text='{"error":"create pod: There are no instances '
                                       'currently available","status":500}')
            return _Resp(201, {"id": f"pod{len(self.launches)}", "costPerHr": 0.3})
        return _Resp(200, {"data": {"gpuTypes": [
            {"id": i, "displayName": d, "memoryInGb": m, "communityPrice": c, "securePrice": s,
             "communityCloud": ic, "secureCloud": isc} for i, d, m, c, s, ic, isc in self.catalog]}})

    def get(self, url, timeout=30, stream=False):
        return _Resp(200, {"desiredStatus": "EXITED"})

    def delete(self, url, timeout=30):
        self.deleted.append(url.rsplit("/", 1)[-1])
        return _Resp(204)


def _rungs(ladder):
    return [(g.id.replace("NVIDIA ", "").replace("GeForce ", ""), g.cloud_type[0]) for g in ladder]


def test_the_ladder_walks_the_allowlist_in_order_each_card_community_then_secure():
    client = RunPodClient("k", session=_Session())
    allow = ("NVIDIA GeForce RTX 3090", "NVIDIA RTX A5000", "NVIDIA GeForce RTX 3090 Ti", "NVIDIA L4")
    assert _rungs(dispatch.gpu_ladder(client, allow, dispatch.CLOUD_TYPES["ANY"])) == [
        ("RTX 3090", "C"), ("RTX 3090", "S"), ("RTX A5000", "C"), ("RTX A5000", "S"),
        ("RTX 3090 Ti", "C"), ("L4", "S")]
    assert _rungs(dispatch.gpu_ladder(client, allow, dispatch.CLOUD_TYPES["COMMUNITY"])) == [
        ("RTX 3090", "C"), ("RTX A5000", "C"), ("RTX 3090 Ti", "C")]
    assert _rungs(dispatch.gpu_ladder(client, allow, dispatch.CLOUD_TYPES["SECURE"])) == [
        ("RTX 3090", "S"), ("RTX A5000", "S"), ("L4", "S")]


def test_the_ladder_prices_each_rung_in_its_own_cloud_under_the_cap():
    client = RunPodClient("k", session=_Session())
    ladder = dispatch.gpu_ladder(client, ("NVIDIA RTX A5000", "NVIDIA L40S"), ("COMMUNITY", "SECURE"))
    # The L40S is $1.09 in the secure cloud: over the cap there, so one rung, not two.
    assert [(g.id, g.cloud_type, g.price_per_hr()) for g in ladder] == [
        ("NVIDIA RTX A5000", "COMMUNITY", 0.16), ("NVIDIA RTX A5000", "SECURE", 0.27),
        ("NVIDIA L40S", "COMMUNITY", 0.79)]
    assert all(g.price_per_hr() <= dispatch.MAX_PRICE_PER_HR for g in ladder)


def test_the_ladder_matches_whole_words_and_exact_ids():
    client = RunPodClient("k", session=_Session())
    # Shorthand keeps working ("3090" is both 3090s, cheaper first); a full id is that card
    # only; "NVIDIA L4" is never the L40S.
    assert _rungs(dispatch.gpu_ladder(client, ("3090", "NVIDIA L4"), ("COMMUNITY", "SECURE"))) == [
        ("RTX 3090", "C"), ("RTX 3090", "S"), ("RTX 3090 Ti", "C"), ("L4", "S")]
    assert _rungs(dispatch.gpu_ladder(client, ("NVIDIA GeForce RTX 3090",), ("COMMUNITY",))) == [
        ("RTX 3090", "C")]


def test_the_ladder_drops_small_and_over_cap_cards_and_refuses_an_empty_ladder():
    client = RunPodClient("k", session=_Session())
    assert _rungs(dispatch.gpu_ladder(client, ("3070", "a100", "a5000"), ("COMMUNITY", "SECURE"))) == [
        ("RTX A5000", "C"), ("RTX A5000", "S")]
    with pytest.raises(RunPodError):
        dispatch.gpu_ladder(client, ("3070", "a100"), ("COMMUNITY", "SECURE"))


def test_the_ladder_survives_one_cloud_whose_catalogue_read_fails():
    class _Half(RunPodClient):
        def eligible_gpus(self, *, max_price_per_hr=None, cloud_type="COMMUNITY"):
            if cloud_type == "COMMUNITY":
                raise RunPodError("no COMMUNITY GPU type available under the given price cap")
            return super().eligible_gpus(max_price_per_hr=max_price_per_hr, cloud_type=cloud_type)

    ladder = dispatch.gpu_ladder(_Half("k", session=_Session()), ("a5000",), ("COMMUNITY", "SECURE"))
    assert _rungs(ladder) == [("RTX A5000", "S")]


def test_the_default_ladder_is_g1s_and_leaves_the_tagging_lane_alone():
    from scripts import tagging_bakeoff_dispatch as tb

    assert tb.DEFAULT_GPU_ALLOWLIST == ("3090", "a5000")
    assert dispatch.DEFAULT_GPU_ALLOWLIST[:3] == ("NVIDIA GeForce RTX 3090", "NVIDIA RTX A5000",
                                                  "NVIDIA GeForce RTX 3090 Ti")
    assert len(set(dispatch.DEFAULT_GPU_ALLOWLIST)) == len(dispatch.DEFAULT_GPU_ALLOWLIST) == 19
    assert dispatch.MIN_GPU_MEMORY_GB == 16 and dispatch.MAX_PRICE_PER_HR == 1.00
    # bf16 (no Volta/Turing) on a cu118 torch (no Blackwell).
    assert not any(b in name for name in dispatch.DEFAULT_GPU_ALLOWLIST
                   for b in ("V100", "Tesla T4", "RTX 5080", "RTX 5090", "RTX PRO", "Blackwell"))


def test_the_default_ladder_equals_g1s_wherever_both_exist():
    g1d = pytest.importorskip("scripts.g1_image_stack_dispatch")
    assert dispatch.DEFAULT_GPU_ALLOWLIST == g1d.G1_GPU_ALLOWLIST
    assert dispatch.MIN_GPU_MEMORY_GB == g1d.MIN_GPU_MEMORY_GB
    assert dispatch.CLOUD_TYPES == g1d.CLOUD_TYPES


def test_the_default_ladder_on_a_live_shaped_catalogue():
    ladder = dispatch.gpu_ladder(RunPodClient("k", session=_Session()),
                                 dispatch.DEFAULT_GPU_ALLOWLIST, dispatch.CLOUD_TYPES["ANY"])
    assert [(g.id, g.cloud_type[0], g.price_per_hr()) for g in ladder] == [
        ("NVIDIA GeForce RTX 3090", "C", 0.22), ("NVIDIA GeForce RTX 3090", "S", 0.43),
        ("NVIDIA RTX A5000", "C", 0.16), ("NVIDIA RTX A5000", "S", 0.27),
        ("NVIDIA GeForce RTX 3090 Ti", "C", 0.27),       # not offered in secure
        ("NVIDIA GeForce RTX 4090", "C", 0.34), ("NVIDIA GeForce RTX 4090", "S", 0.59),
        ("NVIDIA L40S", "C", 0.79),                      # $1.09 secure is over the cap
        ("NVIDIA L4", "S", 0.43)]                        # secure only; V100/5090/3070 never


def _dispatch_live(monkeypatch, session, *args):
    _argv(monkeypatch, "--max-write-mb-per-hour", "200", "--job-max-seconds", "60", *args)
    monkeypatch.setenv("RUNPOD_API_KEY", "rp_key")
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setattr(dispatch, "RunPodClient", lambda key: RunPodClient(key, session=session))
    return dispatch.main()


def test_the_dispatch_falls_back_from_community_to_secure_for_the_same_card(monkeypatch):
    session = _Session(no_capacity={("NVIDIA RTX A5000", "COMMUNITY")})
    rc = _dispatch_live(monkeypatch, session, "--gpu-allowlist", "NVIDIA RTX A5000,3090",
                        "--cloud-type", "ANY")
    assert rc == 0
    assert [(b["gpuTypeIds"][0], b["cloudType"]) for b in session.launches] == [
        ("NVIDIA RTX A5000", "COMMUNITY"), ("NVIDIA RTX A5000", "SECURE")]
    assert session.deleted == ["pod2"]          # the rented pod was torn down


def test_the_dispatch_rents_nothing_and_fails_when_no_rung_has_capacity(monkeypatch):
    rungs = {(i, c) for i, *_ in _LIVE for c in ("COMMUNITY", "SECURE")}
    session = _Session(no_capacity=rungs)
    rc = _dispatch_live(monkeypatch, session, "--cloud-type", "COMMUNITY")
    assert rc == 1
    # The default ladder, community only: the cards this catalogue lists under the cap.
    assert [b["gpuTypeIds"][0] for b in session.launches] == [
        "NVIDIA GeForce RTX 3090", "NVIDIA RTX A5000", "NVIDIA GeForce RTX 3090 Ti",
        "NVIDIA GeForce RTX 4090", "NVIDIA L40S"]
    assert {b["cloudType"] for b in session.launches} == {"COMMUNITY"}
    assert session.deleted == []


def test_an_empty_allowlist_means_the_default_ladder_in_both_clouds(monkeypatch):
    session = _Session(no_capacity={("NVIDIA GeForce RTX 3090", "COMMUNITY")})
    rc = _dispatch_live(monkeypatch, session, "--gpu-allowlist", "")
    assert rc == 0
    assert [(b["gpuTypeIds"][0], b["cloudType"]) for b in session.launches] == [
        ("NVIDIA GeForce RTX 3090", "COMMUNITY"), ("NVIDIA GeForce RTX 3090", "SECURE")]


def test_the_workflow_hands_the_dispatcher_its_allowlist_and_cloud():
    import pathlib

    import yaml

    wf = yaml.safe_load((pathlib.Path(__file__).resolve().parents[2]
                         / ".github/workflows/dinov3_embed_backfill.yml").read_text())
    inputs = (wf.get("on") or wf[True])["workflow_dispatch"]["inputs"]
    assert inputs["gpu_allowlist"]["default"] == ""
    assert inputs["cloud_type"]["default"] == "ANY"
    assert inputs["cloud_type"]["options"] == ["ANY", "COMMUNITY", "SECURE"]
    step = next(s for s in wf["jobs"]["embed"]["steps"]
                if s.get("name") == "Dispatch DINOv3 embedding pass")
    assert step["env"]["GPU_ALLOWLIST"] == "${{ inputs.gpu_allowlist }}"
    assert step["env"]["CLOUD_TYPE"] == "${{ inputs.cloud_type }}"
    # An array, so a GPU id with spaces reaches argparse as ONE argument.
    assert 'ARGS+=(--gpu-allowlist "${GPU_ALLOWLIST}")' in step["run"]
    assert 'ARGS+=(--cloud-type "${CLOUD_TYPE:-ANY}")' in step["run"]
    assert 'python -m scripts.dinov3_embed_dispatch "${ARGS[@]}"' in step["run"]


# --- the dispatch itself --------------------------------------------------------


def _argv(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["dinov3_embed_dispatch", *args])
    monkeypatch.setattr(dispatch, "encoder_identity", lambda *a, **k: dict(IDENTITY))


def test_dry_run_launches_nothing(monkeypatch, caplog):
    _argv(monkeypatch, "--max-write-mb-per-hour", "500", "--dry-run")
    monkeypatch.setattr(
        dispatch, "RunPodClient",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry run must not call RunPod")),
    )
    with caplog.at_level("INFO"):
        assert dispatch.main() == 0
    assert "DRY RUN" in caplog.text


def test_dry_run_never_prints_a_secret_value(monkeypatch, caplog):
    _argv(monkeypatch, "--max-write-mb-per-hour", "500", "--dry-run")
    for key in dispatch.POD_ENV_KEYS:
        monkeypatch.setenv(key, f"SECRET-{key}")
    with caplog.at_level("INFO"):
        dispatch.main()
    assert "SECRET-" not in caplog.text
    assert "HF_TOKEN" in caplog.text  # the NAME is reported, so a missing one is visible


def test_dispatch_hands_the_pod_its_credentials_and_a_wait_window_that_outlives_the_job(
    monkeypatch,
):
    client = _FakeClient(CATALOG)
    _argv(monkeypatch, "--max-write-mb-per-hour", "500", "--job-max-seconds", "3600",
          "--limit", "5000")
    monkeypatch.setenv("RUNPOD_API_KEY", "rp_key")
    for key in dispatch.POD_ENV_KEYS:
        monkeypatch.setenv(key, f"value-of-{key}")
    monkeypatch.setattr(dispatch, "RunPodClient", lambda *a, **k: client)

    assert dispatch.main() == 0
    job = client.jobs[0]
    assert set(job["env"]) == set(dispatch.POD_ENV_KEYS)
    # run_job always times out for on-demand Pods and then terminates in its `finally`,
    # so the wait window IS the pod's lifetime — it must outlive the payload's budget
    # plus the clone+install startup, or teardown lands mid-batch.
    assert job["max_wait_s"] == 3600 + dispatch.STARTUP_GRACE_S
    assert "--max-seconds=3600.0" in job["start_cmd"][2]
    assert "--max-write-mb-per-hour=500.0" in job["start_cmd"][2]
    assert "--limit=5000" in job["start_cmd"][2]


def test_the_pod_is_told_to_use_the_gpu_it_is_renting(monkeypatch, caplog):
    # A pod always has a card, and a payload that quietly ran on CPU would look like
    # a slow run while billing for a GPU it never touched.
    _argv(monkeypatch, "--max-write-mb-per-hour", "500", "--dry-run")
    with caplog.at_level("INFO"):
        assert dispatch.main() == 0
    assert "--device=cuda" in caplog.text


def test_dispatch_refuses_without_an_api_key(monkeypatch):
    _argv(monkeypatch, "--max-write-mb-per-hour", "500")
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    assert dispatch.main() == 1


def test_the_write_ceiling_is_required_here_too(monkeypatch):
    _argv(monkeypatch, "--dry-run")
    with pytest.raises(SystemExit) as exc:
        dispatch.main()
    assert exc.value.code == 2


# --- the watchdog: the wait window is a ceiling, not a plan ---------------------
# 2026-09-08 (the sibling bake-off lane): a pod died in its first seconds and was
# billed for the full 8,115s window because nothing here asked whether it was working.


class _CountingConn:
    """Answers the identity count and nothing else."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.timeouts: list[str] = []

    def transaction(self):
        return self

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if sql.startswith("SET LOCAL"):
            self.timeouts.append(sql)

    def fetchone(self):
        return (self.count,)


def test_progress_is_the_count_of_rows_this_identity_owns():
    conn = _CountingConn(1200)
    reading = dispatch.read_backfill_progress(conn, identity=IDENTITY, baseline=0)
    assert reading.booted is True and reading.marker == "1200"
    # Never terminal: `pending == 0` is an anti-join over ~10.4M images and far too
    # expensive to ask every minute. The stall deadline ends a finished pod instead.
    assert reading.terminal is False
    # And it asks with a short leash — this runs beside a live production database.
    assert "statement_timeout = 30000" in conn.timeouts[0]


def test_a_pod_that_has_written_nothing_yet_has_not_booted():
    conn = _CountingConn(500)
    assert dispatch.read_backfill_progress(conn, identity=IDENTITY,
                                           baseline=500).booted is False


def test_no_database_on_the_runner_means_no_watchdog_and_a_loud_warning(monkeypatch, caplog):
    import argparse

    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    args = argparse.Namespace(bootstrap_deadline_s=1200, stall_deadline_s=900)
    with caplog.at_level("WARNING"):
        assert dispatch.make_watchdog(args, dict(IDENTITY)) is None
    assert "no watchdog" in caplog.text


def test_the_dispatch_hands_the_pod_a_watchdog(monkeypatch):
    client = _FakeClient(CATALOG)
    _argv(monkeypatch, "--max-write-mb-per-hour", "500")
    monkeypatch.setenv("RUNPOD_API_KEY", "rp_key")
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setattr(dispatch, "RunPodClient", lambda *a, **k: client)
    monkeypatch.setattr(dispatch, "make_watchdog", lambda *a, **k: "the-watchdog")
    assert dispatch.main() == 0
    assert client.jobs[0]["progress"] == "the-watchdog"


def test_a_watchdog_teardown_fails_the_lane(monkeypatch):
    # A green run that embedded nothing is what 2026-09-08 looked like in Actions.
    client = _FakeClient(CATALOG, stop_reason="bootstrap-deadline: nothing ever booted")
    _argv(monkeypatch, "--max-write-mb-per-hour", "500")
    monkeypatch.setenv("RUNPOD_API_KEY", "rp_key")
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setattr(dispatch, "RunPodClient", lambda *a, **k: client)
    assert dispatch.main() == 1

    done = _FakeClient(CATALOG, stop_reason="all-terminal: the job finished")
    monkeypatch.setattr(dispatch, "RunPodClient", lambda *a, **k: done)
    assert dispatch.main() == 0


def test_an_under_specified_encoder_is_refused_before_a_pod_is_rented(monkeypatch):
    _provisional(monkeypatch)
    monkeypatch.setattr(sys, "argv",
                        ["dinov3_embed_dispatch", "--max-write-mb-per-hour", "500"])
    monkeypatch.setattr(
        dispatch, "RunPodClient",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not rent a pod")),
    )
    with pytest.raises(RuntimeError) as exc:
        dispatch.main()
    assert "ENCODER-DECISION" in str(exc.value)


def test_scope_and_head_scoring_reach_the_payload(monkeypatch, caplog):
    monkeypatch.setattr(sys, "argv", [
        "dinov3_embed_dispatch", "--max-write-mb-per-hour", "400", "--dry-run",
        "--scope", "ids", "--listing-ids-file", "data/g4/cohort_listing_ids.txt.gz",
        "--score-heads", "--blocks", "town:563510,quarter:490245"])
    with caplog.at_level("INFO"):
        assert dispatch.main() == 0
    text = caplog.text
    assert "--blocks=town:563510,quarter:490245" in text
    assert "--scope=ids" in text
    assert "--listing-ids-file=data/g4/cohort_listing_ids.txt.gz" in text
    assert "--score-heads" in text


def _provisional(monkeypatch):
    """An under-specified config file, standing in for the pre-G4 shipped one."""
    import json as _json
    import tempfile
    from pathlib import Path as _Path

    from scraper import dinov3_config

    raw = dinov3_config.load_dinov3_config()
    raw.update(resolution=None, preprocessing=None, dtype=None)
    path = _Path(tempfile.mkdtemp()) / "dinov3_config.json"
    path.write_text(_json.dumps(raw))
    monkeypatch.setattr(dinov3_config, "_CONFIG_PATH", path)


# --- sizing on the pod, the paged scan, and the R2 mode (2026-09-27) ---------------------


def _plan(monkeypatch, caplog, *args) -> str:
    monkeypatch.setattr(sys, "argv", ["dinov3_embed_dispatch", "--max-write-mb-per-hour", "100",
                                      "--dry-run", *args])
    monkeypatch.setattr(dispatch, "encoder_identity", lambda *a, **k: dict(IDENTITY))
    with caplog.at_level("INFO"):
        assert dispatch.main() == 0
    return caplog.text


def test_the_pod_sizes_itself_and_pays_the_pending_scan_once(monkeypatch, caplog):
    text = _plan(monkeypatch, caplog, "--limit", "12000")
    for arg in ("--chunk=0", "--batch-size=0", "--workers=0", "--select-page=12000",
                "--vectors-to=postgres"):
        assert arg in text
    assert "--r2-prefix" not in text


def test_the_scan_page_is_capped(monkeypatch, caplog):
    assert f"--select-page={dispatch.MAX_SELECT_PAGE}" in _plan(monkeypatch, caplog,
                                                               "--limit", "900000")


def test_r2_mode_and_its_prefix_reach_the_payload(monkeypatch, caplog):
    text = _plan(monkeypatch, caplog, "--vectors-to", "r2", "--r2-prefix", "runs/g4/",
                 "--score-heads", "--scope", "ids",
                 "--listing-ids-file", "data/g4/cohort_listing_ids.txt.gz")
    assert "--vectors-to=r2" in text and "--r2-prefix=runs/g4" in text


class _Store:
    def __init__(self, keys):
        self.keys = keys

    def list_keys(self, prefix):
        return sorted(k for k in self.keys if k.startswith(prefix))


def test_r2_progress_counts_only_this_shards_committed_parts():
    store = _Store(["p/manifest/s1of8/a.csv", "p/manifest/s1of8/b.csv",
                    "p/manifest/s2of8/c.csv", "p/parts/s1of8/a.npz", "p/identity.json"])
    reading = dispatch.read_r2_progress(store, prefix="p", shard=1, shards=8, baseline=0)
    assert (reading.booted, reading.marker, reading.terminal) == (True, "2", False)
    assert dispatch.read_r2_progress(store, prefix="p", shard=3, shards=8,
                                     baseline=0).booted is False


def test_an_r2_pass_is_watched_through_r2_not_postgres(monkeypatch):
    import argparse

    from scraper import image_storage
    from toolkit.vector_shards import default_prefix

    prefix = default_prefix(IDENTITY)
    store = _Store([f"{prefix}/manifest/s0of1/a.csv"])
    monkeypatch.setattr(image_storage, "is_configured", lambda: True)
    monkeypatch.setattr(image_storage.R2Client, "from_env", classmethod(lambda cls, **k: store))
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://must-not-be-read")
    monkeypatch.setattr(dispatch, "_connect",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("postgres read")))
    args = argparse.Namespace(vectors_to="r2", r2_prefix="", shard=0, shards=1,
                              bootstrap_deadline_s=1800, stall_deadline_s=900)
    watchdog = dispatch.make_watchdog(args, dict(IDENTITY))
    assert watchdog is not None
    store.keys.append(f"{prefix}/manifest/s0of1/b.csv")
    assert watchdog._poll().marker == "2"


def test_an_r2_pass_without_r2_on_the_runner_has_no_watchdog_loudly(monkeypatch, caplog):
    import argparse

    from scraper import image_storage

    monkeypatch.setattr(image_storage, "is_configured", lambda: False)
    args = argparse.Namespace(vectors_to="r2", r2_prefix="", shard=0, shards=1,
                              bootstrap_deadline_s=1800, stall_deadline_s=900)
    with caplog.at_level("WARNING"):
        assert dispatch.make_watchdog(args, dict(IDENTITY)) is None
    assert "no watchdog" in caplog.text


def test_the_workflow_offers_r2_mode_and_sizes_workers_on_the_pod():
    import pathlib

    import yaml

    wf = yaml.safe_load((pathlib.Path(__file__).resolve().parents[2]
                         / ".github/workflows/dinov3_embed_backfill.yml").read_text())
    inputs = (wf.get("on") or wf[True])["workflow_dispatch"]["inputs"]
    assert inputs["vectors_to"]["default"] == "postgres"
    assert inputs["vectors_to"]["options"] == ["postgres", "r2"]
    assert inputs["r2_prefix"]["default"] == ""
    assert inputs["workers"]["default"] == "0"
    assert len(inputs) <= 25                       # GitHub's workflow_dispatch input limit
    step = next(s for s in wf["jobs"]["embed"]["steps"]
                if s.get("name") == "Dispatch DINOv3 embedding pass")
    assert step["env"]["VECTORS_TO"] == "${{ inputs.vectors_to }}"
    assert 'ARGS+=(--vectors-to "${VECTORS_TO:-postgres}")' in step["run"]
    assert 'ARGS+=(--r2-prefix "${R2_PREFIX}")' in step["run"]
