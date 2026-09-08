"""scripts/dinov3_embed_dispatch.py — the pod plan: credentials reach the pod through
the REST body's `env` (never argv), the wait window covers the payload's own budget,
the GPU is not picked on price alone, and the git ref cannot smuggle shell.

Hermetic: a fake RunPodClient and a fake requests.Session. No RunPod call, no pod, no
network, no spend.
"""

from __future__ import annotations

import sys

import pytest

from scripts import dinov3_embed_dispatch as dispatch
from scripts.runpod_client import GpuOption

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
    # argv is visible in the pod record; credentials go through `env` instead.
    cmd = dispatch.build_start_cmd(ref="main", backfill_args=["--limit=10"])
    for key in dispatch.POD_ENV_KEYS:
        assert key not in cmd[2]


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


# --- GPU selection -------------------------------------------------------------


class _FakeClient:
    def __init__(self, gpus: list[GpuOption], stop_reason: str | None = None) -> None:
        self._gpus = gpus
        self.jobs: list[dict] = []
        self.stop_reason = stop_reason

    def eligible_gpus(self, *, max_price_per_hr=None):
        return list(self._gpus)

    def run_job_with_fallback(self, **kwargs):
        self.jobs.append(kwargs)
        return type("R", (), {"pod_id": "pod1", "gpu_type_id": "g", "final_status": "RUNNING",
                              "timed_out": True, "elapsed_s": 1.0, "cost_per_hr": 0.22,
                              "stop_reason": self.stop_reason})()


CATALOG = [
    GpuOption("rtx4090", "RTX 4090", 24, 0.20),     # fastest card, FEWEST vCPUs (6)
    GpuOption("rtxa5000", "RTX A5000", 24, 0.16),
    GpuOption("rtx3090", "RTX 3090", 24, 0.22),
]


def test_gpu_selection_prefers_the_cpu_adequate_boxes_over_the_cheapest():
    # JPEG decode is CPU-side; eligible_gpus ranks on price and knows nothing about
    # vCPU, so the 4090 would otherwise win and starve the GPU (§5.3).
    chosen = dispatch.select_gpus(_FakeClient(CATALOG), dispatch.DEFAULT_GPU_ALLOWLIST)
    assert [g.id for g in chosen] == ["rtxa5000", "rtx3090"]


def test_gpu_selection_falls_back_to_the_catalog_when_no_preferred_type_is_listed(caplog):
    with caplog.at_level("WARNING"):
        chosen = dispatch.select_gpus(_FakeClient([CATALOG[0]]), dispatch.DEFAULT_GPU_ALLOWLIST)
    assert [g.id for g in chosen] == ["rtx4090"]
    assert "vCPU" in caplog.text


def test_an_empty_allowlist_means_the_price_ranked_catalog():
    assert dispatch.select_gpus(_FakeClient(CATALOG), ()) == CATALOG


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
    monkeypatch.setattr(sys, "argv",
                        ["dinov3_embed_dispatch", "--max-write-mb-per-hour", "500"])
    monkeypatch.setattr(
        dispatch, "RunPodClient",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not rent a pod")),
    )
    with pytest.raises(RuntimeError) as exc:
        dispatch.main()
    assert "ENCODER-DECISION" in str(exc.value)
