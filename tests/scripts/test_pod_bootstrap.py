"""scripts/pod_bootstrap.py — the script a rented pod runs before the payload.

Every assertion here is a line item on the 2026-09-08 invoice: a 3090 held for 8,115 s
(~$0.50) that produced zero vectors because the pod's first command could not succeed
and its second could not have either. Offline: no pod, no network, no dollar.
"""

from __future__ import annotations

import pytest

from scripts import dinov3_embed_dispatch, pod_bootstrap, tagging_bakeoff_dispatch


def _script(ref: str = "2d00061db1089dad5d42cf51124494c2cdbd1032") -> str:
    return pod_bootstrap.build_bootstrap_script(
        ref=ref, module="scripts.tagging_bakeoff_embed", payload_args=["--run-id=1"])


def test_the_repo_is_fetched_by_sha_never_cloned_by_branch():
    # `git clone --branch <sha>` fails with "Remote branch ... not found in upstream
    # origin" (exit 128) — and under `set -euo pipefail` that is the whole pod.
    script = _script()
    assert "--branch" not in script
    assert "git clone" not in script
    assert "git fetch --depth 1 origin 2d00061db1089dad5d42cf51124494c2cdbd1032" in script
    assert "git checkout -q FETCH_HEAD" in script
    # Order matters: a remote must exist before the fetch, and the fetch before checkout.
    assert (script.index("git remote add origin")
            < script.index("git fetch --depth 1")
            < script.index("git checkout -q FETCH_HEAD"))


def test_the_interpreter_is_bootstrapped_not_inherited():
    # The image ships Python 3.10; pyproject requires >=3.12, so `pip install -e .`
    # under the image's own python cannot succeed however healthy the checkout is.
    script = _script()
    assert f"uv venv --python {pod_bootstrap.PYTHON_VERSION} {pod_bootstrap.VENV_DIR}" in script
    assert pod_bootstrap.PYTHON_VERSION == "3.12"
    # The install and the payload must run INSIDE that venv, so the venv has to be on
    # PATH before either of them.
    assert (script.index(f"export PATH={pod_bootstrap.VENV_DIR}/bin:$PATH")
            < script.index("uv pip install -e")
            < script.index("python -m scripts.tagging_bakeoff_embed"))


def test_torch_comes_from_the_cuda_flavour_the_image_can_run():
    # cu118: the image is CUDA 11.8-era, and cu118 is the flavour with cp312 wheels
    # furthest up the torch series (through 2.6.0; cu121 stops at 2.5.1).
    script = _script()
    assert "uv pip install torch --index-url https://download.pytorch.org/whl/cu118" in script
    assert script.index("uv pip install torch") < script.index("uv pip install -e")


def test_the_venv_is_entered_without_sourcing_activate():
    # `set -u` is on for the whole script and the activate script is not written for it.
    script = _script()
    assert "activate" not in script
    assert f"export VIRTUAL_ENV={pod_bootstrap.VENV_DIR}" in script


@pytest.mark.parametrize(
    "ref", ["main; rm -rf /", "a b", "$(id)", "`id`", "'x'", 'a"b', "-flag", "", "x" * 300],
)
def test_an_unsafe_ref_is_never_interpolated(ref):
    with pytest.raises(ValueError, match="unsafe git ref"):
        pod_bootstrap.build_bootstrap_script(ref=ref, module="m", payload_args=[])


def test_an_unsafe_payload_arg_or_module_is_never_interpolated():
    with pytest.raises(ValueError, match="unsafe payload arg"):
        pod_bootstrap.build_bootstrap_script(ref="main", module="m",
                                             payload_args=["--limit=1; curl evil"])
    with pytest.raises(ValueError, match="unsafe module"):
        pod_bootstrap.build_bootstrap_script(ref="main", module="m; id", payload_args=[])
    with pytest.raises(ValueError, match="unsafe extra"):
        pod_bootstrap.build_bootstrap_script(ref="main", module="m", payload_args=[],
                                             extra="clip'; id; '")


def test_both_lanes_run_the_same_bootstrap():
    # They were broken identically because the script was copied; one module now, so a
    # fix cannot land in one lane and miss the other.
    a = dinov3_embed_dispatch.build_start_cmd(ref="abc123", backfill_args=["--limit=10"])
    b = tagging_bakeoff_dispatch.build_start_cmd(
        ref="abc123", module="scripts.tagging_bakeoff_embed", payload_args=["--run-id=1"])
    for cmd in (a, b):
        assert cmd[0] == "bash" and cmd[1] == "-c"
        assert "git fetch --depth 1 origin abc123" in cmd[2]
        assert "uv venv --python 3.12" in cmd[2]
        assert "--branch" not in cmd[2]


def test_the_start_command_still_carries_no_credential():
    # argv is visible in the pod record; secrets travel in the REST body's `env`.
    script = _script()
    for key in dinov3_embed_dispatch.POD_ENV_KEYS:
        assert key not in script
