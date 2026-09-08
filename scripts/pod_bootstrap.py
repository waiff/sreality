"""The shell a rented GPU pod runs before the payload: fetch this repo at a sha, stand
up a Python 3.12 interpreter, install torch, install us. One module because both RunPod
lanes (`dinov3_embed_dispatch`, `tagging_bakeoff_dispatch`) need the identical thing and
the 2026-09-08 failure was in this script, not in either lane's logic.

TWO BUGS THIS EXISTS TO NOT REPEAT, both paid for on 2026-09-08 (pod u1yvcktjn6dbrt,
8,115 s on an RTX 3090, ~$0.50, zero vectors):

  * `git clone --depth 1 --branch <sha>` CANNOT WORK. `--branch` takes a branch or a
    tag, never a commit sha, and the dispatchers pass `GITHUB_SHA`. The pod died in
    seconds ("fatal: Remote branch ... not found in upstream origin", exit 128) under
    `set -euo pipefail` and then idled, billing, for the whole wait window. The
    fetch-by-sha form below is the one that works against GitHub:
    `git init` + `git remote add` + `git fetch --depth 1 origin <sha>` + `checkout
    FETCH_HEAD`. Never reintroduce `--branch` here — a branch name would work and a sha
    would not, so it fails only in production.
  * THE IMAGE'S PYTHON IS 3.10 AND `pyproject.toml` REQUIRES >=3.12, so even a fixed
    clone would have died on `pip install -e '.[clip]'`. RunPod's newer image tags do
    not name a Python version, so the interpreter is BOOTSTRAPPED rather than inherited:
    `uv venv --python 3.12` (uv downloads a managed CPython when the box has none).
    The image tag then only has to supply a CUDA driver, which is the one thing it
    genuinely owns.

TORCH COMES FROM THE cu118 INDEX. The image is CUDA 11.8-era and cu118 is the flavour
with the widest cp312 coverage on the PyTorch index (cp312 wheels through torch 2.6.0;
cu121 stops at 2.5.1). The version is deliberately NOT pinned here — the payload records
the RESOLVED python/torch/transformers versions into its own progress rows, which is a
more honest record than a pin nobody re-reads.

Carries no secrets: this is argv, visible in the pod record. Credentials travel in the
REST body's `env` (see `RunPodClient.launch_pod`).
"""

from __future__ import annotations

import re
from typing import Sequence

REPO_URL = "https://github.com/waiff/sreality"
CHECKOUT_DIR = "/workspace/sreality"
VENV_DIR = "/workspace/venv"
PYTHON_VERSION = "3.12"
TORCH_INDEX_URL = "https://download.pytorch.org/whl/cu118"

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
    return "; ".join([
        "set -euo pipefail",
        f"mkdir -p {CHECKOUT_DIR}",
        f"cd {CHECKOUT_DIR}",
        # Fetch BY SHA. `--branch` resolves a branch or tag only and would fail here.
        "git init -q",
        f"git remote add origin {REPO_URL}",
        f"git fetch --depth 1 origin {ref}",
        "git checkout -q FETCH_HEAD",
        # The image's own pip (3.10) installs uv; uv then provides the 3.12 the project
        # requires, downloading a managed CPython if the box has none.
        "pip install --quiet uv",
        f"uv venv --python {PYTHON_VERSION} {VENV_DIR}",
        # Not `source .../activate`: that script is not written for `set -u`, and these
        # two exports are all of it that matters (uv honours VIRTUAL_ENV).
        f"export VIRTUAL_ENV={VENV_DIR}",
        f"export PATH={VENV_DIR}/bin:$PATH",
        f"uv pip install torch --index-url {TORCH_INDEX_URL}",
        f"uv pip install -e '.[{extra}]'",
        f"python -m {module} " + " ".join(payload_args),
    ])


def build_start_cmd(*, ref: str, module: str, payload_args: Sequence[str],
                    extra: str = "clip") -> list[str]:
    """The pod's argv."""
    return ["bash", "-c", build_bootstrap_script(ref=ref, module=module,
                                                 payload_args=payload_args, extra=extra)]
