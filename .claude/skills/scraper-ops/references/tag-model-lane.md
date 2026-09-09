# The tag model lane

`tag_model.yml` — **manual `workflow_dispatch` only, no schedule, CPU only, no GPU, no R2,
no money.** Where the tagging bake-off (`references/tagging-bakeoff-lane.md`) *measured* which
encoder and training mode makes the best per-tag heads, this lane *keeps* one of those answers
and puts it to work.

Two words do a lot of work below:

- a **head** is one yes/no classifier for one photo tag ("is this a kitchen?");
- a **model** is one **frozen decision** — this encoder configuration, this training mode, this
  set of heads, these weights — under a **version** name (`v1`).

## The rule everything follows from

The operator's ruling of 2026-09-09 had five asks; this lane exists for the first of them, and
the whole list (with which PR carried out each) is in `docs/design/new-dedup/PROGRAM.md`'s
`2026-09-09 (b)` ledger entry. **Cite an ask by its number**, never by a letter — the letters in
a ledger heading are that day's *entries*, not the ruling's parts.

Ask 1: **an image's tag is the head that scored highest.** There are **no per-head yes/no
decisions in the product at all.** So:

- every head's probability is stored for every image, not just the winner's;
- **no threshold and no boolean is stored** — a head's own threshold lives inside its artifact
  as *evidence* (it is what the bake-off measured it at), and any consumer that wants "…and
  only if it is confident" applies **its own floor** to `winner_score`;
- ties break toward the **lower `tag_id`**, stated so the same photo cannot tag differently
  between two runs of the same model;
- **adding a head is a NEW VERSION, never an edit** — "highest score wins" (an *argmax*) is only
  meaningful over one head set at a time, so the set is frozen on the model row.

## The loop

```
a new bake-off run  ->  promote  ->  score  ->  activate
```

| stage | what it does | needs |
| --- | --- | --- |
| `status` | every version, its head count, how many images it has scored, which one is active | read-only |
| `promote` | trains one head per selected tag of a bake-off run on that run's stored vectors and writes them as a **`candidate`** version nobody reads | the `training` extra (scikit-learn); minutes of CPU |
| `score` | every head's probability per image plus the winner, into `image_tag_scores` | base install — inference is a dot product and a logistic in **pure Python** |
| `activate` | makes exactly one version the one consumers read, in one transaction | base install |

**Why activation is its own stage**: a version that is only half scored must never be readable.
`promote` writes something inert, `score` fills it at whatever pace and is resumable, and the
flip is a single transaction. Rolling forward is another `promote`; rolling back is `activate`
on the previous version.

```
tag_model.yml
  └── python -m scripts.tag_model  status | promote | score | activate
        └── toolkit/tag_models.py
              ├── promote  → toolkit/tag_heads.train_head over dedup_sim.tag_head_bakeoff_vectors
              ├── score    → toolkit/tag_heads.score_embedding (stdlib only)
              └── winners  → the read contract for later waves + /new-dedup/tags/*
```

## The store (migration 490, PUBLIC schema)

| table | one row per | holds |
| --- | --- | --- |
| `tag_head_models` | version | status (`candidate`/`active`/`retired`), mode, the seven encoder identity facts, the frozen head set, a dataset hash. A **partial unique index** forbids two active rows. |
| `tag_head_model_heads` | (model, tag) | the `toolkit/tag_heads.py` artifact + the bake-off's cv/exam numbers **copied** at promotion |
| `image_tag_scores` | (image, model) | `scores` jsonb (every head), `winner_tag_id`, `winner_score` |

**Public and not `dedup_sim`, with no foreign key into it**: the bake-off's tables are evidence
and are dropped wholesale at Wave 8; this is the model the product tags with, so it outlives
them, and dropping the evidence must not cascade into the product. `source_run_id` /
`source_arm` are provenance values, not references.

## Running it

Always start with `dry_run` (the default). `promote`'s dry run prints the run's arms and the
head list and trains nothing; `score`'s prints what it would consider and writes nothing;
`activate`'s says which version would be retired.

```
stage=status                                      # what exists
stage=promote run_id=1 arm=dinov2-l14-reg@504/bf16 mode=pos_neg version=v1 \
              label="run 1, dinov2-l14 @504" dry_run=false
stage=score   version=v1 source=bakeoff dry_run=false
stage=activate version=v1 dry_run=false
```

**`source`** picks where the vectors come from:

- `bakeoff` / `bakeoff:<run_id>` — the model's own bake-off arm. Run 1's arm holds the 9,264
  labelled training photos plus the 250 sealed-exam photos: **the cheap iteration loop**.
- `production` — `image_dinov3_embeddings` filtered to the model's seven identity facts. This
  is the future **corpus pass**. It works today and returns nothing, because nothing has
  populated that table for this configuration yet, and the corpus pass is not scheduled until
  the operator is satisfied with accuracy.

`score` is **resumable**: an image this version already scored is skipped, so a pass that dies
halfway is re-runnable for free. `force=true` re-scores them — the upsert overwrites, because
the **model version is the identity**: the same version scoring the same photo twice must mean
the same thing.

## Reading results

Progress and results are SQL questions:

```sql
select version, status, mode, cardinality(heads) as n_heads,
       (select count(*) from image_tag_scores s where s.model_id = m.id) as n_scored
from tag_head_models m order by id desc;

select winner_tag_id, count(*) from image_tag_scores
where model_id = (select id from tag_head_models where status = 'active')
group by 1 order by 2 desc;
```

…or through the admin-gated API: `GET /new-dedup/tags/models`,
`GET /new-dedup/tags/models/{version}/heads`, `GET /new-dedup/tags/images/{image_id}`.
There is no page yet.

## Rails you must not step over

- **Labels come only through `toolkit.machine_labeling.training_rows`**, the one
  holdout-excluded door. `toolkit/tag_models.py` contains no SQL naming `image_tag_labels`,
  deliberately — `tests/test_holdout_exclusion_census.py` therefore has nothing here to exempt,
  and it must stay that way.
- **One model is one training mode, and `pos_neg` is the live one.** `pos_only_free_neg` (the
  "borrowed no") and `pos_only_centroid` (the "closeness only") were **retired from the
  experiment on 2026-09-09** (ask 2, PR #1365); the lane still accepts them by name so a
  bake-off cell can be reproduced, and both help texts say so. The winner is an argmax, and a
  centroid head's cosine and a logistic head's probability are not the same quantity; the mode
  is stamped on the model row so the two can never be mixed. Read a centroid version's stored
  value as a score, not as a percentage.
- **Never edit a version in place.** A new training set, a new head, a new encoder, new
  parameters: all the same move, a new version.
- **Never store a per-head decision.** If a surface needs "only confident tags", it applies a
  floor to `winner_score` at read time.
- **`image_tag_scores` is DERIVED, not history.** Rule #3's "never delete" is about listings; a
  retired version's rows are recomputable from its stored heads and are **safe to prune** when
  the disk matters (`delete from image_tag_scores where model_id = …`, or drop the model row and
  let the cascade do it). The operator decides when; nothing prunes automatically. The `active`
  version's rows are never pruned — that is what consumers read.
