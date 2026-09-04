"""One definition, two renderings: the machine's instruction sheet and a human card.

A `tag_definitions` row is stored in the shape code needs — `counts`,
`does_not_count`, `confusable_with`, `leave_out_when` — because that is what a
prompt builder can assemble mechanically. It is NOT the shape a person labeling
images can hold in their head, and showing it to them is a real cost: the operator
reported being confused by exactly those four boxes, and confusion in the authoring
step becomes noise in every label written under it.

So the storage shape stays, and this module renders it two ways from that single
source. `render_prompt` is what the vision model reads. `render_card` is what the
operator reads while labeling — a headline question and three plain-language lists.
Neither is authored separately, so they cannot drift into disagreeing about what a
tag means, which is the failure a hand-written "labeling guide" document would
guarantee within a week.

Pure and DB-free: it takes the dict `tag_definitions.get_active_definition` already
returns (including its resolved `referenced_tags`), so it is testable without a
database and callable from the API, a batch job, or a script alike.
"""

from __future__ import annotations

from typing import Any, TypedDict


class HandbookCard(TypedDict):
    """The operator-facing rendering. Deliberately NOT keyed like the stored row:
    nothing here is named after a database column, because the whole point is that
    the person labeling never has to learn the storage vocabulary."""

    tag_label: str
    headline: str
    count_it: list[str]
    dont_count_it: list[str]
    cant_tell: list[str]


# The four storage field names. The card must never contain any of them — asserted
# in tests rather than merely intended, because a leak here is invisible to the
# person writing the renderer and obvious to the person using it.
STORAGE_FIELD_NAMES = ("counts", "does_not_count", "confusable_with", "leave_out_when")


def _label_map(definition: dict[str, Any]) -> dict[int, str]:
    return {
        int(t["tag_id"]): str(t["label"])
        for t in definition.get("referenced_tags") or []
    }


def _label_for(labels: dict[int, str], tag_id: Any) -> str | None:
    """A referenced tag that no longer exists resolves to None, never a crash and
    never an empty quoted string. `_referenced_tags` drops unknown ids by design —
    it resolves at render time against a denormalized snapshot — so a definition
    can legitimately point at a tag the operator has since deleted."""
    if tag_id is None:
        return None
    return labels.get(int(tag_id))


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _entries(definition: dict[str, Any], field: str) -> list[dict[str, Any]]:
    return [e for e in (definition.get(field) or []) if isinstance(e, dict)]


def _texts(definition: dict[str, Any], field: str) -> list[str]:
    return [t for t in (_clean(v) for v in (definition.get(field) or [])) if t]


def render_card(definition: dict[str, Any], *, tag_label: str) -> HandbookCard:
    """The operator's rendering: one question, three lists.

    `does_not_count` and `confusable_with` both collapse into "don't count it",
    because that distinction is a property of how the RULE works, not of what the
    labeler has to do — in both cases the answer is no. The redirect ("that is X
    instead") is kept, since knowing where a photo belongs is what makes the no
    feel decidable rather than arbitrary."""
    labels = _label_map(definition)
    means = _clean(definition.get("means"))

    headline = f"Is this photo of {tag_label}?" if not means else (
        f"Is this photo of {tag_label} — {means[0].lower() + means[1:]}"
        if means[0].isupper() else f"Is this photo of {tag_label} — {means}"
    )
    if not headline.endswith("?"):
        headline = headline.rstrip(".") + "?"

    count_it = _texts(definition, "counts")
    if not count_it and means:
        # A definition with a `means` and no `counts` is the common early shape;
        # an empty "count it" list would read as "nothing counts".
        count_it = [means]

    dont: list[str] = []
    for entry in _entries(definition, "does_not_count"):
        case = _clean(entry.get("case"))
        if not case:
            continue
        target = _label_for(labels, entry.get("goes_to_tag_id"))
        dont.append(f"{case} — that is “{target}” instead." if target else case)
    for entry in _entries(definition, "confusable_with"):
        tell = _clean(entry.get("tell"))
        target = _label_for(labels, entry.get("tag_id"))
        if not tell:
            continue
        dont.append(f"If it could be “{target}”: {tell}." if target else tell)

    cant_tell = [t for t in [_clean(definition.get("leave_out_when"))] if t]

    return HandbookCard(
        tag_label=tag_label, headline=headline, count_it=count_it,
        dont_count_it=dont, cant_tell=cant_tell,
    )


def render_prompt(definition: dict[str, Any], *, tag_label: str) -> str:
    """The vision model's rendering: the same content, kept structured.

    The model keeps the `confusable_with` / `does_not_count` distinction the card
    collapses, because it acts on them differently — a confusable names a rival tag
    to weigh against, a does-not-count is an unconditional exclusion."""
    labels = _label_map(definition)
    lines: list[str] = [f'TAG: "{tag_label}"']

    means = _clean(definition.get("means"))
    if means:
        lines.append(f"MEANS: {means}")

    counts = _texts(definition, "counts")
    if counts:
        lines.append("COUNTS AS THIS TAG:")
        lines += [f"  - {c}" for c in counts]

    excludes = []
    for entry in _entries(definition, "does_not_count"):
        case = _clean(entry.get("case"))
        if not case:
            continue
        target = _label_for(labels, entry.get("goes_to_tag_id"))
        excludes.append(f"{case}{f' (belongs to \"{target}\")' if target else ''}")
    if excludes:
        lines.append("DOES NOT COUNT:")
        lines += [f"  - {e}" for e in excludes]

    tells = []
    for entry in _entries(definition, "confusable_with"):
        tell = _clean(entry.get("tell"))
        if not tell:
            continue
        target = _label_for(labels, entry.get("tag_id"))
        tells.append(f'vs "{target}": {tell}' if target else tell)
    if tells:
        lines.append("EASILY CONFUSED WITH:")
        lines += [f"  - {t}" for t in tells]

    leave_out = _clean(definition.get("leave_out_when"))
    if leave_out:
        lines.append(f"GENUINELY UNDECIDABLE WHEN: {leave_out}")

    # Stated last so it is the final instruction the model reads, and it is the
    # BRIEF's three-state rule verbatim in spirit — the first version of this
    # paragraph "sharpened" the present-but-not-the-subject case into a yes, which
    # was a quiet guideline change hiding in a renderer. Restored: that case is a
    # leave-out, exactly as the operator labels it on the exam screen, so the
    # machine and the human answer the same question the same way.
    lines.append(
        "THE QUESTION IS WHAT THIS PHOTO IS AN IMAGE OF, never what is visible in "
        "it.\n"
        "ANSWER yes / no / skip.\n"
        "yes — this tag is what the photo is of. If two or three things are "
        "equally the subject and you cannot tell which is primary, answer yes for "
        "each of them.\n"
        "no — it does not apply. An incidental appearance in the background is a "
        "valuable no. A case listed under DOES NOT COUNT is ALWAYS a no, however "
        "similar it looks: those boundaries are the ones worth learning.\n"
        "skip — two cases only: the image is genuinely undecidable or unreadable, "
        "or this tag's subject is clearly and substantially present, yet the photo "
        "is plainly composed on something else. Never answer no in that second "
        "case: leave it out instead."
    )
    return "\n".join(lines)
