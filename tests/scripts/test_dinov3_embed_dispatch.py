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
    assert "--branch abc123" in cmd[2]
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
    def __init__(self, gpus: list[GpuOption]) -> None:
        self._gpus = gpus
        self.jobs: list[dict] = []

    def eligible_gpus(self, *, max_price_per_hr=None):
        return list(self._gpus)

    def run_job_with_fallback(self, **kwargs):
        self.jobs.append(kwargs)
        return type("R", (), {"pod_id": "pod1", "gpu_type_id": "g", "final_status": "RUNNING",
                              "timed_out": True, "elapsed_s": 1.0, "cost_per_hr": 0.22})()


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


def test_dispatch_refuses_without_an_api_key(monkeypatch):
    _argv(monkeypatch, "--max-write-mb-per-hour", "500")
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    assert dispatch.main() == 1


def test_the_write_ceiling_is_required_here_too(monkeypatch):
    _argv(monkeypatch, "--dry-run")
    with pytest.raises(SystemExit) as exc:
        dispatch.main()
    assert exc.value.code == 2


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
