"""The tagging bake-off's ARMS — one encoder configuration each, and the one place
that knows what the default run measures.

Shared by `scripts/tagging_bakeoff_manifest.py` (which writes one arm row per arm) and
`scripts/tagging_bakeoff_embed.py` (which loads each arm's encoder on the pod). Neither
duplicates the list, so "what did run 7 measure?" has exactly one answer in code and
exactly one answer in `dedup_sim.tag_head_bakeoff_arms`.

WHAT AN ARM IS. The same SEVEN FACTS that identify a production DINOv3 vector
(`scraper/dinov3_config.py`): model, revision, library, pooling, resolution,
preprocessing, dtype. Any of them changing is a NEW POPULATION, not a new value — which
is why the arms table carries all seven and the vectors table is keyed on `arm_id`. An
arm is therefore transplantable: the winner's seven facts are exactly what gets written
into `data/dinov3_config.json`, with no translation step in between.

WHAT THE DEFAULT RUN ASKS. The operator's questions, one arm each:
  * "at least 512, and pad with bars rather than crop" -> 512/768/1024 x letterbox_pad
  * "test both precisions"                             -> fp32 and bf16 at each
  * "what about even larger?"                          -> the 1024 pair. AS MEASURED
    (run 1, 2026-09), stored photos were 749x562 (sreality, the largest portal) to
    ~1800 px wide, so 768 and above mostly UPSAMPLED the biggest portal. Whether that
    buys anything is the measurement. That premise EXPIRED on 2026-09-11: sreality now
    downloads the whole frame at up to 1800 px, so the corpus is mixed-rendition
    (`images.rendition`) until the re-master lane finishes and a re-run of these arms
    measures a different population from run 1's.
  * "is a bigger model worth it?"                      -> DINOv3 ViT-L/16 @512
  * "what if the licence falls through?"               -> DINOv2-L/14-with-registers
    (Apache-2.0), at 504 because patch 14 does not divide 512
  * "is a language-supervised encoder better at TAGS?" -> SigLIP2 B/16, its own pooling

NOTHING BELOW 512 PX IS IN THE DEFAULT ANY MORE (operator ruling 2026-09-09). Run 1
measured the two 224 px arms — LAION CLIP B/32 and the zero-GPU `clip-b32-stored` copy
of the INCUMBENT's live vectors — 0.03 to 0.06 mean F1 below the 512+ field, which
answers "how much better than what we already have?" once. They are not deleted:
`retired_arms()` keeps them, `all_arms()` is the catalogue an explicit
`--arms clip-b32-stored` resolves against, and a named retired arm still runs. It is
`default_arms()` — what a run measures when nobody narrows it — that has stopped
spending GPU minutes on a settled question. DINOv2's 504 is 512 snapped to patch 14,
so it stays.

RESOLUTION SNAPS TO THE PATCH GRID and the snapped value is what gets recorded. DINOv2
is patch 14, so a requested 512 is really 504; recording the request and calling the
result 512 would be a lie the trainer could never detect.

REVISIONS ARE RESOLVED AT RUN TIME, never hardcoded here — nothing in this file could
verify a sha. `hub_sha` asks the public model-metadata endpoint with `requests` (a base
dependency) rather than `huggingface_hub`, because the manifest stage runs on a plain
GitHub runner that installs neither torch nor the `clip` extra. An arm whose sha will
not resolve (gated weights, no token, licence not accepted) is SKIPPED with the reason
recorded — never loaded from an unpinned `main`.

No torch, no transformers, no DB — importable anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Sequence

# The public model-metadata endpoint. Same host `data/dinov3_config.json`'s pinned
# revision was read from; returns the repo's HEAD sha plus enough metadata to answer
# "does this checkpoint exist and may this token read it?".
HUB_API = "https://huggingface.co/api/models"
HUB_TIMEOUT_S = 30

# The incumbent, copied not computed. `image_clip_embeddings` is keyed (image_id,
# model); this is the model NAME the live corpus was written under.
STORED_CLIP_ARM = "clip-b32-stored"
STORED_CLIP_MODEL = "openai/clip-vit-base-patch32"
# vector(512) in migration 226 — a schema fact, not a guess, so the arm's `dim` can be
# stamped without a pgvector-specific dimension query the CI schema replay may not have.
STORED_CLIP_DIM = 512

# SigLIP2 B/16 at the largest fixed-resolution checkpoint that exists. Probed in this
# order at run time; the first that resolves wins. `-512` is the operator's "as large as
# it goes" question applied to the tag-side control. The `-256` fallback PR #1300
# defended was DROPPED on 2026-09-09: the ruling put a 512 floor under the default
# preset, so silently degrading this control to 256 would smuggle a retired resolution
# back into a run nobody narrowed. If 512 will not resolve the embed stage skips the
# arm and says so — a missing control is readable, a quietly smaller one is not.
SIGLIP2_CANDIDATES: tuple[tuple[str, int], ...] = (
    ("google/siglip2-base-patch16-512", 512),
)


@dataclass(frozen=True)
class Arm:
    """One encoder configuration. `name` is the arm's identity to the operator; the
    seven fields below are its identity to the mathematics."""

    name: str
    model: str
    library: str
    pooling: str
    resolution: int          # as REQUESTED; `effective_resolution` is what runs
    preprocessing: str
    dtype: str
    patch: int
    model_class: str = "AutoModel"
    gated: bool = False
    note: str = ""

    @property
    def effective_resolution(self) -> int:
        return snap_to_patch(self.resolution, self.patch)

    def identity(self, revision: str | None) -> dict[str, Any]:
        """The seven facts, shaped for a `tag_head_bakeoff_arms` row."""
        return {
            "arm": self.name,
            "model": self.model,
            "revision": revision or "",
            "library": self.library,
            "pooling": self.pooling,
            "resolution": self.effective_resolution,
            "preprocessing": self.preprocessing,
            "dtype": self.dtype,
        }


def snap_to_patch(size: int, patch: int) -> int:
    """Largest multiple of `patch` at or below `size` (floor one patch).

    A ViT cannot take a resolution off its patch grid, so a requested 512 on a patch-14
    model IS 504 whatever anyone writes down. Snapping here makes the effective value a
    recorded fact rather than a surprise discovered in the results table.
    """
    if patch <= 0:
        return int(size)
    return max(int(patch), (int(size) // int(patch)) * int(patch))


# --------------------------------------------------------------------------------
# The default preset
# --------------------------------------------------------------------------------

DINOV3_B16 = "facebook/dinov3-vitb16-pretrain-lvd1689m"
DINOV3_L16 = "facebook/dinov3-vitl16-pretrain-lvd1689m"
DINOV2_L14_REG = "facebook/dinov2-with-registers-large"
LAION_CLIP_B32 = "laion/CLIP-ViT-B-32-laion2B-s34B-b79K"

# The DINO family's pooling label. 'cls' — NOT 'cls_post_ln' — because that is the
# vocabulary `data/dinov3_config.json` and `scraper/dinov3_tagger.py` already speak, and
# the winning arm's seven facts are meant to be copied into that file verbatim. It means
# the same thing either way: `outputs.pooler_output`, the POST-LayerNorm CLS token, not
# `last_hidden_state[:, 0]`.
DINO_POOLING = "cls"

# The operator's resolution x precision grid on the leading arm. A real cross product
# here (6 arms) rather than one-knob-at-a-time, because "is 1024 worth it in bf16?" is
# not answerable from a 512-fp32 baseline plus two separate knobs.
DINOV3_B16_RESOLUTIONS: tuple[int, ...] = (512, 768, 1024)
DINOV3_B16_DTYPES: tuple[str, ...] = ("fp32", "bf16")


def _dinov3_b16_arms() -> list[Arm]:
    arms: list[Arm] = []
    for resolution in DINOV3_B16_RESOLUTIONS:
        for dtype in DINOV3_B16_DTYPES:
            arms.append(Arm(
                name=f"dinov3-b16@{resolution}/{dtype}",
                model=DINOV3_B16, library="transformers", pooling=DINO_POOLING,
                resolution=resolution, preprocessing="letterbox_pad", dtype=dtype,
                patch=16, gated=True,
                note="the recommended encoder; the resolution x precision grid the "
                     "operator asked for (min 512, bars not crops, both precisions)",
            ))
    return arms


def default_arms(*, siglip_model: str = SIGLIP2_CANDIDATES[-1][0],
                 siglip_resolution: int = SIGLIP2_CANDIDATES[-1][1]) -> list[Arm]:
    """Every arm of the default run, in the order they are worth reading.

    `siglip_model`/`siglip_resolution` are arguments because which SigLIP2 checkpoint
    exists is a question for the Hub, answered at run time by
    `resolve_siglip_checkpoint`; the signature defaults to the guaranteed fallback so
    this function stays pure and offline-testable.
    """
    arms = _dinov3_b16_arms()
    arms.append(Arm(
        name="dinov3-l16@512/bf16", model=DINOV3_L16, library="transformers",
        pooling=DINO_POOLING, resolution=512, preprocessing="letterbox_pad",
        dtype="bf16", patch=16, gated=True,
        note="is the bigger DINOv3 worth its cost? bf16 only — an fp32 L/16 at 512 "
             "doubles the priciest arm to answer a question the B/16 pair already asks",
    ))
    arms.append(Arm(
        name="dinov2-l14-reg@504/bf16", model=DINOV2_L14_REG, library="transformers",
        pooling=DINO_POOLING, resolution=504, preprocessing="letterbox_pad",
        dtype="bf16", patch=14,
        note="Apache-2.0 — the licence fallback if DINOv3's terms ever bite. 504, not "
             "512: patch 14 does not divide 512",
    ))
    arms.append(Arm(
        name=f"siglip2-b16@{siglip_resolution}/bf16", model=siglip_model,
        library="transformers", pooling="attention_pool", resolution=siglip_resolution,
        preprocessing="letterbox_pad", dtype="bf16", patch=16,
        model_class="SiglipVisionModel",
        note="the language-supervised control. NO CLS token: its summary comes from a "
             "learned attention-pooling head, so `pooler_output` here is a different "
             "mechanism from the DINO arms' post-LN CLS that happens to share a name",
    ))
    return arms


def retired_arms() -> list[Arm]:
    """The arms the 2026-09-09 ruling took OFF the default: everything under 512 px.

    Kept rather than deleted, because run 1's rows name them and a settled question can
    still be re-asked. Nothing here runs unless `--arms` names it.
    """
    return [
        Arm(
            name="clip-b32-laion@224/fp32", model=LAION_CLIP_B32,
            library="transformers", pooling="image_embeds", resolution=224,
            preprocessing="square_squash", dtype="fp32", patch=32,
            model_class="CLIPVisionModelWithProjection",
            note="RETIRED 2026-09-09 (under 512 px). The LAION CLIP baseline (MIT, "
                 "ungated) at its native geometry — told 'the CLIP family is weak "
                 "here' apart from 'the 2021 checkpoint is weak'",
        ),
        Arm(
            name=STORED_CLIP_ARM, model=STORED_CLIP_MODEL,
            library="pgvector (stored)", pooling="image_embeds", resolution=224,
            preprocessing="square_squash", dtype="fp32", patch=32, model_class="",
            note="RETIRED 2026-09-09 (under 512 px). ZERO GPU: the incumbent's LIVE "
                 "vectors, copied out of image_clip_embeddings by SQL in the manifest "
                 "stage. Run 1 measured that baseline; naming this arm is how a later "
                 "run measures it again",
        ),
    ]


def all_arms(*, siglip_model: str = SIGLIP2_CANDIDATES[-1][0],
             siglip_resolution: int = SIGLIP2_CANDIDATES[-1][1]) -> list[Arm]:
    """Every arm a NAME can resolve to — the default preset plus the retired ones.

    The catalogue `--arms` is checked against, so asking for a retired arm is a
    request rather than a typo, while a run nobody narrowed still gets only
    `default_arms()`.
    """
    return default_arms(siglip_model=siglip_model,
                        siglip_resolution=siglip_resolution) + retired_arms()


def select_arms(arms: Sequence[Arm], names: Iterable[str] | None) -> list[Arm]:
    """Narrow the preset to `names`, preserving preset order. An unknown name raises —
    a typo that silently ran the whole grid would cost real GPU minutes."""
    if not names:
        return list(arms)
    wanted = [n.strip() for n in names if n and n.strip()]
    if not wanted:
        return list(arms)
    known = {a.name for a in arms}
    unknown = [n for n in wanted if n not in known]
    if unknown:
        raise ValueError(
            f"unknown arm(s): {', '.join(sorted(unknown))}. Known: "
            + ", ".join(sorted(known))
        )
    keep = set(wanted)
    return [a for a in arms if a.name in keep]


def renamed_to_effective(arm: Arm) -> Arm:
    """An arm whose NAME carries its effective resolution, so a snapped arm cannot be
    called by a resolution it did not run at."""
    if arm.effective_resolution == arm.resolution or "@" not in arm.name:
        return arm
    head, _, tail = arm.name.partition("@")
    _, _, suffix = tail.partition("/")
    new = f"{head}@{arm.effective_resolution}" + (f"/{suffix}" if suffix else "")
    return replace(arm, name=new)


# --------------------------------------------------------------------------------
# Hub metadata — resolved at run time, never hardcoded
# --------------------------------------------------------------------------------

def hub_sha(repo_id: str, *, token: str | None = None, get: Any = None) -> str:
    """The HF commit sha for `repo_id`, from the public model-metadata endpoint.

    `requests`, not `huggingface_hub`: the manifest stage runs on a plain runner with
    only the base dependencies installed, and the same helper has to work there and on
    the pod. `get` is injectable so the offline tests never touch the network.
    """
    if get is None:
        import requests

        get = requests.get
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = get(f"{HUB_API}/{repo_id}", headers=headers, timeout=HUB_TIMEOUT_S)
    status = int(getattr(resp, "status_code", 0))
    if status == 401 or status == 403:
        raise PermissionError(
            f"{repo_id}: hub returned {status} — gated weights with no usable token "
            "(HF_TOKEN missing, or the licence has not been accepted for this account)"
        )
    if status == 404:
        raise LookupError(f"{repo_id}: hub returned 404 — no such checkpoint")
    if status >= 400:
        raise RuntimeError(f"{repo_id}: hub returned {status}")
    sha = (resp.json() or {}).get("sha")
    if not sha:
        raise RuntimeError(f"{repo_id}: hub returned no commit sha")
    return str(sha)


def resolve_siglip_checkpoint(*, token: str | None = None,
                              get: Any = None) -> tuple[str, int]:
    """(repo_id, resolution) for the largest SigLIP2 B/16 checkpoint that exists.

    Probes `SIGLIP2_CANDIDATES` largest-first and returns the first that resolves. The
    last candidate is returned unprobed if every probe fails — since the 2026-09-09
    ruling dropped the 256 fallback that is the 512 checkpoint itself, so a Hub outage
    ends in the embed stage resolving the sha again and SKIPPING the arm, never in a
    quietly smaller control.
    """
    for repo_id, resolution in SIGLIP2_CANDIDATES:
        try:
            hub_sha(repo_id, token=token, get=get)
        except Exception:  # noqa: BLE001 - a candidate that will not resolve is not it
            continue
        return repo_id, resolution
    return SIGLIP2_CANDIDATES[-1]
