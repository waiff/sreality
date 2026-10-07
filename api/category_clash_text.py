"""The one Czech sentence for a category clash rule 15 refuses (E925), in the Browse filters' own
labels: the pair page prints it with `SAME_ENDING`, the property split's join with `LETTER_ENDING`,
the merge route with `MERGE_ENDING`."""
from __future__ import annotations

from toolkit.filter_registry import CATEGORY_MAIN_OPTIONS, CATEGORY_TYPE_OPTIONS

_CLASH: dict[str, str] = {
    "category_type": "Inzerát typu {a} a inzerát typu {b} systém nikdy nespojí do jedné "
                     "nemovitosti",
    "category_main": "Inzerát v kategorii {a} a inzerát v kategorii {b} systém nikdy nespojí "
                     "do jedné nemovitosti (výjimkou jsou jen dvojice dům – komerční objekt, "
                     "dům – pozemek, komerční objekt – pozemek a byt – komerční objekt)",
}
SAME_ENDING: str = ", proto je nelze označit jako stejné."
LETTER_ENDING: str = ", proto nemohou mít stejné písmeno. Dejte jim různá písmena."
MERGE_ENDING: str = ", proto je nelze sloučit."
LABELS: dict[str, str] = {
    option.value: option.label_cs
    for option in (*CATEGORY_TYPE_OPTIONS, *CATEGORY_MAIN_OPTIONS)
}


def clash_sentence(field: str, a: str | None, b: str | None, *, ending: str) -> str:
    return _CLASH[field].format(a=LABELS.get(a, a), b=LABELS.get(b, b)) + ending
