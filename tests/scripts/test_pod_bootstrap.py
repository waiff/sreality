"""scripts/pod_bootstrap.py — the script a rented pod runs before the payload.

Every assertion here is a line item on a 2026-09-08 invoice: a 3090 held for 8,115 s
(~$0.50) that produced zero vectors because the pod's first command could not succeed
and its second could not have either, and its retry (~$0.08) that was killed correctly
and still could not be diagnosed, because nothing reported until the whole bootstrap had
already worked.

The last two tests are the ones that matter most: they EXECUTE the generated bash, with
every real step stubbed, and prove the EXIT trap names the step that failed. Offline: no
pod, no network, no dollar.
"""

from __future__ import annotations

import json

import pytest

from scripts import (dinov3_embed_dispatch, pod_bootstrap, pod_report,
                     tagging_bakeoff_dispatch)


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
    assert f'uv venv --python {pod_bootstrap.PYTHON_VERSION} "$PODBOOT_ROOT/venv"' in script
    assert pod_bootstrap.PYTHON_VERSION == "3.12"
    # The install and the payload must run INSIDE that venv, so the venv has to be on
    # PATH before either of them.
    assert (script.index('export PATH="$PODBOOT_ROOT/venv/bin:$PATH"')
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
    assert 'export VIRTUAL_ENV="$PODBOOT_ROOT/venv"' in script


def test_every_step_reports_before_the_next_one_starts():
    # The 2026-09-08 retry died somewhere in here and the run could only say "the pod
    # never reported Python running". Each step now names itself as it completes.
    script = _script()
    positions = [script.index(f'report "step={name} ok"')
                 for name in ("deps", "fetch", "uv", "venv", "torch", "repo")]
    assert positions == sorted(positions)
    assert script.index('report "step=payload starting"') > positions[-1]
    # Free space is reported BEFORE the ~5GB install, not after it fails.
    assert script.index('report "step=disk') > script.index('report "step=venv ok"')
    assert script.index('report "step=disk') < script.index("uv pip install torch")
    # The reporter is installed and callable BEFORE the first real step, or the first
    # step's failure is again invisible.
    assert script.index("report.py") < script.index("git init")


def test_the_reporter_runs_on_the_images_python_not_the_venv():
    # The 3.12 venv the bootstrap builds has no psycopg, and does not exist yet when the
    # first steps report. Resolving python3 BEFORE the PATH export is what guarantees it.
    script = _script()
    assert 'PODBOOT_PY="$(command -v python3)"' in script
    assert (script.index('PODBOOT_PY="$(command -v python3)"')
            < script.index('export PATH="$PODBOOT_ROOT/venv/bin:$PATH"'))
    # Every report carries the pass number, so a restart loop reads as one in the note.
    assert ('report() { "$PODBOOT_PY" "$PODBOOT_ROOT/report.py" "pass=$PODBOOT_PASS" '
            '"$@" || true; }') in script


def test_a_reporter_failure_can_never_fail_the_bootstrap():
    # A heartbeat that kills the job it reports on would be worse than blindness.
    script = _script()
    assert script.count("|| true") >= 1
    # Every report carries the pass number, so a restart loop reads as one in the note.
    assert ('report() { "$PODBOOT_PY" "$PODBOOT_ROOT/report.py" "pass=$PODBOOT_PASS" '
            '"$@" || true; }') in script
    assert "x pip install --quiet 'psycopg[binary]' ||" in script


def test_the_exit_trap_ships_the_failing_step_and_the_log_tail():
    script = _script()
    assert "trap on_exit EXIT" in script
    assert 'report "exit=$PODBOOT_CODE step=$PODBOOT_STEP" --tail-file "$PODBOOT_LOG"' in script
    # `$?` must be read first or the trap reports the exit code of its own first command.
    body = script[script.index("on_exit() {"):script.index("trap on_exit EXIT")]
    assert body.splitlines()[1].strip() == "PODBOOT_CODE=$?"
    assert 'exec > >(tee -a "$PODBOOT_LOG") 2>&1' in script


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
        assert 'report "step=torch ok"' in cmd[2]


def test_the_start_command_still_carries_no_credential():
    # argv is visible in the pod record; secrets travel in the REST body's `env`. The
    # reporter READS `SUPABASE_DB_URL` by name, which is why this checks for an
    # assignment rather than for the name — a name in a log has never been the risk.
    script = _script()
    for key in dinov3_embed_dispatch.POD_ENV_KEYS:
        assert f"{key}=" not in script
    assert 'os.environ.get("SUPABASE_DB_URL")' in script


# --- the generated bash, actually executed ------------------------------------------


def test_the_generated_script_runs_clean_end_to_end_offline():
    proc = pod_bootstrap.run_dry(_script())
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-2000:]
    for name in ("deps", "fetch", "uv", "venv", "torch", "repo"):
        assert f"step={name} ok" in out
    # It ends idling, not exiting: RunPod re-runs the start command when it exits.
    assert "pass=1 step=payload ok" in out
    assert "pass=1 exit=0 step=idle" in out


def _records(out: str) -> list[dict]:
    """Every heartbeat the run printed, parsed."""
    return [json.loads(ln.split("heartbeat: ", 1)[1])
            for ln in out.splitlines()
            if ln.startswith("heartbeat: {")]


def test_a_failing_step_is_named_by_the_trap_with_the_log_tail():
    # THE behaviour we cannot get wrong twice: when torch dies, the run note must say
    # torch — not "the pod never reported Python running".
    proc = pod_bootstrap.run_dry(_script(), fail_step="torch")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1
    assert "step=venv ok" in out and "step=torch ok" not in out
    record = _records(out)[-1]
    assert record["msg"] == "pass=1 exit=1 step=torch"
    # The tail is what turns "torch failed" into "torch failed BECAUSE …".
    assert "failing step=torch" in record["tail"]
    assert record["ts"] and record["pod"] and record["python"]


# --- the restart loop (2026-09-08 (h)) ----------------------------------------------


def test_the_work_dir_is_on_the_container_disk_not_the_pod_volume():
    # `/workspace` is the VOLUME mount point, and the volume defaulted to 1 GB while a
    # ~5 GB torch install was aimed at it. Nothing this script writes may land there.
    script = _script()
    assert pod_bootstrap.CONTAINER_ROOT == "/opt/podboot"
    assert pod_bootstrap.VOLUME_MOUNT_PATH not in script
    assert 'PODBOOT_ROOT="${PODBOOT_ROOT:-/opt/podboot}"' in script


def test_a_second_pass_over_the_same_disk_is_harmless_and_says_which_pass_it_is(tmp_path):
    # RunPod re-runs the docker start command whenever it exits, so pass 2 met
    # `git remote add origin` -> "remote origin already exists" (exit 3) and looped.
    root = tmp_path / "podboot"
    root.mkdir()
    script = _script()
    first = pod_bootstrap.run_dry(script, root=str(root))
    second = pod_bootstrap.run_dry(script, root=str(root))
    assert first.returncode == 0 and second.returncode == 0, second.stderr[-2000:]
    assert "already exists" not in (second.stdout + second.stderr)
    assert "pass=1 step=fetch ok" in first.stdout + first.stderr
    assert "pass=2 step=fetch ok" in second.stdout + second.stderr
    # The counter is a file, which is the only reason it survives the restart.
    assert (root / "pass").read_text().strip() == "2"


def test_a_clean_payload_ends_in_a_sleep_rather_than_an_exit():
    # Exiting IS the restart loop. In production the sleep is unbounded; the offline run
    # only finishes because PODBOOT_SLEEP_S bounds it.
    script = _script()
    assert 'if [ "$PODBOOT_SLEEP_S" = "0" ]; then sleep infinity;' in script
    assert script.index("python -m scripts.tagging_bakeoff_embed") < script.index(
        "sleep infinity")
    proc = pod_bootstrap.run_dry(script, idle_s=1.5, timeout_s=60)
    assert proc.returncode == 0
    steps = [r["msg"] for r in _records(proc.stdout + proc.stderr)]
    assert steps[-2] == "pass=1 step=idle"          # said before the sleep
    assert steps[-1] == "pass=1 exit=0 step=idle"   # the trap, after it


def test_the_free_space_before_the_torch_install_is_reported(tmp_path):
    root = tmp_path / "podboot"
    root.mkdir()
    proc = pod_bootstrap.run_dry(_script(), root=str(root))
    disk = [r["msg"] for r in _records(proc.stdout + proc.stderr)
            if "step=disk" in r["msg"]]
    assert len(disk) == 1 and "GB free on" in disk[0]
    assert "unknown" not in disk[0]


def test_the_heartbeat_history_survives_the_restart_that_it_describes(tmp_path):
    # The record that named the cause was overwritten by the next pass's first
    # heartbeat, 98 s later, before the runner's 60 s poll could read it.
    root = tmp_path / "podboot"
    root.mkdir()
    script = _script()
    pod_bootstrap.run_dry(script, fail_step="torch", root=str(root))
    pod_bootstrap.run_dry(script, fail_step="fetch", root=str(root))
    history = json.loads((root / "steps.json").read_text())
    messages = [r["msg"] for r in history]
    assert "pass=1 exit=1 step=torch" in messages    # the cause, still there
    assert "pass=2 exit=1 step=fetch" in messages    # and the restart that buried it
    assert len(history) <= pod_report.HISTORY_LIMIT


def test_the_beat_keeps_reporting_through_a_step_that_says_nothing(tmp_path):
    # A 2 GB torch download is silent for minutes. Without the beat, silence during a
    # step and a dead pod are again the same observation.
    root = tmp_path / "ws"
    root.mkdir()
    proc = pod_bootstrap.run_dry(_script(), beat_s=0.2, sleep_s=0.4, root=str(root),
                                 timeout_s=60)
    assert proc.returncode == 0
    log = (root / "bootstrap.log").read_text()
    assert "running" in log
    # The beat names the step the script is ON, not the step it was forked at.
    assert any(f'"step={name} running"' in log or f"step={name} running" in log
               for name in ("uv", "venv", "torch", "repo"))


def test_preflight_passes_for_the_script_we_ship_and_fails_for_a_broken_one():
    ok, lines = pod_bootstrap.preflight(_script())
    assert ok, lines
    assert any("preflight[clean]" in ln and "OK" in ln for ln in lines)
    # The restart case runs the script a SECOND time over the same root — the shape of
    # the 2026-09-08 (h) failure, proven offline before a pod is rented.
    assert any("preflight[restart]" in ln and "OK" in ln for ln in lines)
    assert any("preflight[torch]" in ln and "OK" in ln for ln in lines)

    broken_ok, broken_lines = pod_bootstrap.preflight("set -euo pipefail\nif then fi\n")
    assert not broken_ok and broken_lines
