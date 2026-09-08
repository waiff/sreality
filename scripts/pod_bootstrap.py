"""The shell a rented GPU pod runs before the payload: report, fetch this repo at a sha,
stand up a Python 3.12 interpreter, install torch, install us — reporting after every
step. One module because both RunPod lanes (`dinov3_embed_dispatch`,
`tagging_bakeoff_dispatch`) need the identical thing and every 2026-09-08 failure was
in this script, not in either lane's logic.

SIX BUGS THIS EXISTS TO NOT REPEAT, all paid for on 2026-09-08:

  * `git clone --depth 1 --branch <sha>` CANNOT WORK (pod u1yvcktjn6dbrt, 8,115 s,
    ~$0.50). `--branch` takes a branch or a tag, never a commit sha, and the dispatchers
    pass `GITHUB_SHA`. The pod died in seconds ("fatal: Remote branch ... not found in
    upstream origin", exit 128) under `set -euo pipefail` and then idled, billing. The
    fetch-by-sha form below is the one that works against GitHub: `git init` +
    `git remote add` + `git fetch --depth 1 origin <sha>` + `checkout FETCH_HEAD`. Never
    reintroduce `--branch` — a branch name would work and a sha would not, so it fails
    only in production.
  * THE IMAGE'S PYTHON IS 3.10 AND `pyproject.toml` REQUIRES >=3.12, so even a fixed
    clone would have died on `pip install -e '.[clip]'`. RunPod's newer image tags do
    not name a Python version, so the interpreter is BOOTSTRAPPED rather than inherited:
    `uv venv --python 3.12` (uv downloads a managed CPython when the box has none).
  * NOTHING SAID WHICH STEP FAILED (pod bsg9k5ee9y6jcm, 1,245 s, ~$0.08). The watchdog
    killed it correctly and the verdict could only be "the pod never reported Python
    running", because the first heartbeat was the PAYLOAD's — written after every step
    below had already succeeded. RunPod's Pod logs endpoint answers 400, so a slow 2 GB
    torch download and a dead clone were the same observation. Hence: a reporter
    (`scripts/pod_report.py`) is written to the box FIRST, on the image's own 3.10, and
    called after every step; a background beat repeats the current step every
    `PODBOOT_BEAT_S` so a long silent install still proves life; and an EXIT trap ships
    `exit=<code> step=<the step that failed>` plus the tail of the bootstrap log into
    the same row. The next failure names itself.
  * RUNPOD RE-RUNS THE DOCKER START COMMAND WHENEVER IT EXITS (pod lg5oy1ivlgoyh7,
    ~33 min, ~$0.12). Attempt 3's heartbeats showed `step=venv ok` at 63 s and then, 98 s
    later, a SECOND pass of this whole script failing at `step=fetch` with
    `error: remote origin already exists` — repeating every ~60 s until the watchdog's
    stall rail ended the run. So: EVERY STEP IS IDEMPOTENT (the checkout and the venv are
    removed before they are rebuilt, and `$PODBOOT_ROOT` itself — which holds the
    reporter, the log, the step file, the heartbeat history and the pass counter — is
    not), and after a clean payload the script SLEEPS instead of exiting, because the
    container restarts if it does not. The dispatcher's watchdog is what ends the pod.
    Every report carries `pass=N` (a counter on disk, so it survives a restart), so a
    restart loop is legible in the note instead of looking like ordinary progress.
  * `/workspace` IS THE POD VOLUME AND THE VOLUME IS 1 GB. `RunPodClient.launch_pod`
    defaulted `volume_gb=1` and RunPod mounts the volume at `/workspace`, while this
    script put the venv (torch: ~2.5 GB downloaded, ~5 GB installed) under it. That is
    the likeliest reason attempt 3's first pass died at `step=torch` in under 100 s — the
    report we needed was overwritten by the restart before anyone read it. The work dir
    is therefore on the CONTAINER disk (`/opt/podboot`), the lanes size
    `container_disk_gb` for the job and ask for no volume at all, and a `df -h` plus a
    `step=disk <free>GB` heartbeat before the torch step make the next disk problem
    visible instead of inferred.
  * `torch` ALONE IS NOT ENOUGH: `transformers` NEEDS `torchvision` (pod 4rgi66lggbty11,
    1,547 s, ~$0.09). Attempt 4 booted, installed and embedded — the DINOv2, SigLIP2 and
    LAION-CLIP arms each wrote their 9,514 vectors — and then all SEVEN DINOv3 arms died
    identically at model load: "`DINOv3ViTImageProcessorFast` requires `torchvision` to
    be installed". The fast image processors transformers now selects by default do their
    resize/crop in torchvision, and only the DINOv3 checkpoints resolve to one, so a torch
    install that had been fine for three arms was fatal for seven. `torchvision` is
    therefore installed in the SAME step from the SAME index as torch, because a
    torchvision built against a different CUDA than the torch beside it fails at import
    rather than at install.

TORCH AND TORCHVISION COME FROM THE cu118 INDEX, in one command. The image is CUDA
11.8-era and cu118 is the flavour with the widest cp312 coverage on the PyTorch index
(cp312 through torch 2.7.1 / torchvision 0.22.1; cu121 stops at 2.5.1). One command
because the resolver then picks the pair the index publishes together — attempt 4
resolved torch 2.7.1+cu118, whose partner is torchvision 0.22.1+cu118. The versions are
deliberately NOT pinned here — the payload records the RESOLVED python/torch/transformers
versions into its own progress rows, which is a more honest record than a pin nobody
re-reads.

THE GENERATED SCRIPT IS EXECUTABLE OFFLINE. With `PODBOOT_DRY=1` every real step becomes
a no-op stub and `PODBOOT_DRY_FAIL=<step>` forces one to fail, so `preflight()` proves —
for free, before a pod is rented — that the script parses, that the trap reports the
failing step, and that a second pass over the same root is harmless. That is the one
behaviour we cannot afford to get wrong twice.

Carries no secrets: this is argv, visible in the pod record. Credentials (and the
lane's `HEARTBEAT_SQL`) travel in the REST body's `env` (see `RunPodClient.launch_pod`).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

REPO_URL = "https://github.com/waiff/sreality"
# THE CONTAINER DISK, NOT `/workspace`. `/workspace` is RunPod's default mount point for
# the pod VOLUME, which the lanes no longer rent (and which defaulted to 1 GB — far less
# than torch needs). Every path below is derived from `$PODBOOT_ROOT`, which defaults to
# this and is overridden only by the offline self-check (a test runner cannot mkdir
# /opt/podboot).
CONTAINER_ROOT = "/opt/podboot"
# What a lane must NOT set `PODBOOT_ROOT` to, and what the tests assert against.
VOLUME_MOUNT_PATH = "/workspace"
PYTHON_VERSION = "3.12"
TORCH_INDEX_URL = "https://download.pytorch.org/whl/cu118"
# Long enough not to spam the note through a 10-minute install, short enough that three
# missed beats (the watchdog's 900 s stall deadline) means dead rather than busy.
BEAT_INTERVAL_S = 300
# The steps, in order. The names are the vocabulary of every heartbeat and of the exit
# report, so they are short and stable.
STEPS = ("deps", "fetch", "uv", "venv", "disk", "torch", "repo", "payload", "idle")

_REPORTER_SOURCE = (Path(__file__).with_name("pod_report.py")).read_text(encoding="utf-8")
_HEREDOC = "PODBOOT_REPORT_EOF"

# The ref is interpolated into a shell command, so it is constrained to what a git ref
# can legally contain — no spaces, quotes, semicolons or backticks.
_REF_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]{0,199}$")
# What may be interpolated as a payload argument. Arm names carry `@` and `/`
# (`dinov3-b16@768/bf16`), so those are in; a space, quote, semicolon or backtick is not.
_ARG_RE = re.compile(r"[A-Za-z0-9._=/@,:-]+")


def build_bootstrap_script(*, ref: str, module: str, payload_args: Sequence[str],
                           extra: str = "clip") -> str:
    """The pod's whole shell script, as one `bash -c` string."""
    if not _REF_RE.match(ref):
        raise ValueError(
            f"refusing to interpolate an unsafe git ref into a shell command: {ref!r}")
    for arg in payload_args:
        if not _ARG_RE.fullmatch(arg):
            raise ValueError(f"refusing to interpolate an unsafe payload arg: {arg!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.]+", module):
        raise ValueError(f"refusing to interpolate an unsafe module name: {module!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", extra):
        raise ValueError(f"refusing to interpolate an unsafe extra: {extra!r}")
    if _HEREDOC in _REPORTER_SOURCE:
        raise ValueError("the reporter source would terminate its own heredoc")
    payload = " ".join(payload_args)
    return "\n".join([
        "set -euo pipefail",
        f'PODBOOT_ROOT="${{PODBOOT_ROOT:-{CONTAINER_ROOT}}}"',
        f'PODBOOT_BEAT_S="${{PODBOOT_BEAT_S:-{BEAT_INTERVAL_S}}}"',
        # 0 = sleep forever after a clean payload (the production setting). Any other
        # value is a bounded sleep, which is how the offline self-check finishes.
        'PODBOOT_SLEEP_S="${PODBOOT_SLEEP_S:-0}"',
        'mkdir -p "$PODBOOT_ROOT"',
        'PODBOOT_LOG="$PODBOOT_ROOT/bootstrap.log"',
        ': > "$PODBOOT_LOG"',
        # Everything the bootstrap says goes to the log, so the EXIT trap can ship the
        # actual error text — the Pod logs endpoint answers 400 and never will.
        'exec > >(tee -a "$PODBOOT_LOG") 2>&1',
        # RunPod restarts the container when this command exits, so this script may be
        # running for the second (or twentieth) time over the same disk. The counter is
        # a file for exactly that reason, and every report carries it.
        'PODBOOT_PASS="$(( $(cat "$PODBOOT_ROOT/pass" 2>/dev/null || echo 0) + 1 ))"',
        'echo "$PODBOOT_PASS" > "$PODBOOT_ROOT/pass"',
        'echo "PODBOOT pass=$PODBOOT_PASS root=$PODBOOT_ROOT"',
        # The disk this whole bootstrap lives on, named before anything can fill it.
        'df -h || true',
        # The reporter runs on the IMAGE's python. The 3.12 venv built below is a
        # different interpreter without psycopg, and may not exist when this is needed.
        'PODBOOT_PY="$(command -v python3)"',
        # Heartbeat history: the reporter keeps the last few records here so a crash
        # loop cannot overwrite the report that explains it (2026-09-08 (h)).
        'export PODBOOT_HISTORY="$PODBOOT_ROOT/steps.json"',
        f'cat > "$PODBOOT_ROOT/report.py" <<\'{_HEREDOC}\'',
        _REPORTER_SOURCE.rstrip("\n"),
        _HEREDOC,
        'report() { "$PODBOOT_PY" "$PODBOOT_ROOT/report.py" "pass=$PODBOOT_PASS" "$@" '
        "|| true; }",
        # The current step lives in a FILE, not just a variable: the background beat is
        # a forked subshell and would otherwise report the step it was forked at.
        'step() { PODBOOT_STEP="$1"; echo "$1" > "$PODBOOT_ROOT/step"; }',
        # PODBOOT_DRY swaps every real command for a stub so the whole script can be
        # executed offline; PODBOOT_DRY_FAIL forces one step to fail (see preflight()).
        "x() {",
        '  if [ -n "${PODBOOT_DRY:-}" ]; then',
        # A stub that can take time is how the offline run reproduces the thing this
        # whole file exists for: a step that is slow and silent.
        '    if [ -n "${PODBOOT_DRY_SLEEP:-}" ]; then sleep "$PODBOOT_DRY_SLEEP"; fi',
        '    if [ "${PODBOOT_DRY_FAIL:-}" = "$PODBOOT_STEP" ]; then',
        '      echo "PODBOOT_DRY: failing step=$PODBOOT_STEP: $*"; return 1',
        "    fi",
        '    echo "PODBOOT_DRY: skipping $*"; return 0',
        "  fi",
        '  "$@"',
        "}",
        "step start",
        "on_exit() {",
        "  PODBOOT_CODE=$?",
        # tee is asynchronous; without this the tail can miss the very lines that say
        # what went wrong.
        "  sleep 1 || true",
        '  kill "${PODBOOT_BEAT:-0}" 2>/dev/null || true',
        '  report "exit=$PODBOOT_CODE step=$PODBOOT_STEP" --tail-file "$PODBOOT_LOG"',
        "}",
        "trap on_exit EXIT",
        'report "step=start"',
        "step deps",
        # psycopg into the image's python, for the reporter only. A few seconds, and a
        # failure here costs the heartbeats, never the run.
        "x pip install --quiet 'psycopg[binary]' || echo 'step=deps FAILED — heartbeats will only print'",
        'report "step=deps ok"',
        # The beat writes straight to the log file rather than through the tee pipe: an
        # orphaned `sleep` holding that pipe open would hang whoever reads our output.
        '( while true; do sleep "$PODBOOT_BEAT_S"; '
        'report "step=$(cat "$PODBOOT_ROOT/step" 2>/dev/null || echo unknown) running"; '
        'done ) >> "$PODBOOT_LOG" 2>&1 &',
        "PODBOOT_BEAT=$!",
        "step fetch",
        # IDEMPOTENT BY DEMOLITION. `git remote add origin` on a second pass fails with
        # "remote origin already exists" (exit 3) — the whole of attempt 3's restart
        # loop. A fresh directory cannot be in a half-built state either.
        'rm -rf "$PODBOOT_ROOT/sreality"',
        'mkdir -p "$PODBOOT_ROOT/sreality"',
        'cd "$PODBOOT_ROOT/sreality"',
        # Fetch BY SHA. `--branch` resolves a branch or tag only and would fail here.
        "x git init -q",
        f"x git remote add origin {REPO_URL}",
        f"x git fetch --depth 1 origin {ref}",
        "x git checkout -q FETCH_HEAD",
        'report "step=fetch ok"',
        "step uv",
        # The image's own pip (3.10) installs uv; uv then provides the 3.12 the project
        # requires, downloading a managed CPython if the box has none.
        "x pip install --quiet uv",
        'report "step=uv ok"',
        "step venv",
        # Same reason as the checkout: a venv left half-built by a killed pass is worse
        # than no venv, and `uv venv` is not a repair tool.
        'rm -rf "$PODBOOT_ROOT/venv"',
        f'x uv venv --python {PYTHON_VERSION} "$PODBOOT_ROOT/venv"',
        # Not `source .../activate`: that script is not written for `set -u`, and these
        # two exports are all of it that matters (uv honours VIRTUAL_ENV).
        'export VIRTUAL_ENV="$PODBOOT_ROOT/venv"',
        'export PATH="$PODBOOT_ROOT/venv/bin:$PATH"',
        'report "step=venv ok"',
        "step disk",
        # The step before the 5 GB one says how much room it has. A disk failure was
        # invisible on 2026-09-08 (h) and cost a whole run to guess at.
        'PODBOOT_FREE_GB="$(df -BG --output=avail "$PODBOOT_ROOT" 2>/dev/null '
        "| tail -1 | tr -dc '0-9' || true)\"",
        'report "step=disk ${PODBOOT_FREE_GB:-unknown}GB free on $PODBOOT_ROOT"',
        "step torch",
        # TORCHVISION IS NOT OPTIONAL, see the header. Same command and same index as
        # torch so the two CUDA builds are the matched pair the index publishes.
        f"x uv pip install torch torchvision --index-url {TORCH_INDEX_URL}",
        'report "step=torch ok"',
        "step repo",
        f"x uv pip install -e '.[{extra}]'",
        'report "step=repo ok"',
        "step payload",
        'report "step=payload starting"',
        # The payload owns the heartbeat from here (it writes its own boot stamp and
        # per-arm progress), so the beat stops rather than racing its read-modify-write.
        'kill "$PODBOOT_BEAT" 2>/dev/null || true',
        f"x python -m {module} {payload}".rstrip(),
        'report "step=payload ok"',
        "step idle",
        # DO NOT EXIT. RunPod re-runs this start command whenever it ends, so returning
        # here means running the whole bootstrap again, forever, on the pod's dime. The
        # dispatcher's watchdog terminates the pod once every arm is terminal.
        'report "step=idle"',
        'if [ "$PODBOOT_SLEEP_S" = "0" ]; then sleep infinity; '
        'else sleep "$PODBOOT_SLEEP_S"; fi',
    ])


def build_start_cmd(*, ref: str, module: str, payload_args: Sequence[str],
                    extra: str = "clip") -> list[str]:
    """The pod's argv."""
    return ["bash", "-c", build_bootstrap_script(ref=ref, module=module,
                                                 payload_args=payload_args, extra=extra)]


def run_dry(script: str, *, fail_step: str | None = None, beat_s: float = 0,
            sleep_s: float = 0, root: str | None = None, idle_s: float = 0.2,
            timeout_s: float = 120) -> subprocess.CompletedProcess[str]:
    """Execute the generated script locally with every real step stubbed out.

    Real bash, real trap, real reporter — only the commands that would cost money or
    need a network are stubs. This is the only way to prove the trap fires, and it is
    free. `idle_s` stands in for the production `sleep infinity`; it must never be 0
    here, or the offline run would hang exactly the way the pod is supposed to."""
    with tempfile.TemporaryDirectory(prefix="podboot-") as tmp:
        root = root or tmp
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "PODBOOT_DRY": "1",
            "PODBOOT_ROOT": root,
            "PODBOOT_BEAT_S": str(beat_s or BEAT_INTERVAL_S),
            "PODBOOT_SLEEP_S": str(idle_s or 0.2),
        }
        if fail_step:
            env["PODBOOT_DRY_FAIL"] = fail_step
        if sleep_s:
            env["PODBOOT_DRY_SLEEP"] = str(sleep_s)
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                              text=True, timeout=timeout_s, check=False)


def preflight(script: str, *, timeout_s: float = 120) -> tuple[bool, list[str]]:
    """Run the generated script offline — clean, again over the SAME root (a restart),
    then with `torch` forced to fail — and confirm the EXIT trap reported each outcome.

    A syntax error in this script, a trap that does not fire, or a step that cannot
    survive RunPod re-running the start command is otherwise only discovered by renting
    a GPU and waiting out a deadline. Returns (ok, log lines)."""
    lines: list[str] = []
    ok = True
    with tempfile.TemporaryDirectory(prefix="podboot-preflight-") as shared:
        cases = (
            ("clean", None, 0, "pass=1 exit=0 step=idle", shared),
            # The 2026-09-08 (h) restart loop: the SAME root, a second time.
            ("restart", None, 0, "pass=2 exit=0 step=idle", shared),
            ("torch", "torch", 1, "exit=1 step=torch", None),
        )
        for label, fail_step, want_code, want, root in cases:
            try:
                proc = run_dry(script, fail_step=fail_step, root=root,
                               timeout_s=timeout_s)
            except OSError as exc:  # no bash here: report, do not fail the dry run
                lines.append(f"preflight[{label}]: SKIPPED — cannot execute bash ({exc})")
                continue
            except subprocess.TimeoutExpired:
                lines.append(f"preflight[{label}]: FAILED — the script did not finish "
                             f"in {timeout_s:.0f}s")
                ok = False
                continue
            out = proc.stdout + proc.stderr
            good = proc.returncode == want_code and want in out
            ok = ok and good
            lines.append(f"preflight[{label}]: rc={proc.returncode} "
                         f"{'OK' if good else 'FAILED'} — expected rc={want_code} and "
                         f"{want!r} in the trap's report")
            if not good:
                lines.extend(out.splitlines()[-25:])
    return ok, lines
