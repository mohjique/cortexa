"""Rule 1 for model-written findings text, enforced in code.

A findings summary (MedGemma) is shown to the doctor as a draft. It may
describe what is visible; it may not diagnose, name a tumor type without
hedging ("suspected", "consistent with"), recommend treatment, or suggest a
referral (the users are the specialists). Used by the schema, so no
unchecked summary can reach the frontend, and by classifier/medgemma.py.
"""

from __future__ import annotations

import re

_FORBIDDEN = re.compile(
    r"\b(diagnos(?:is|ed|e|tic of)|confirm(?:s|ed)?|definitive(?:ly)?|certainly|"
    r"recommend\w*|treat(?:ment|ed)?|surgery|resect\w*|chemo\w*|radiation therapy|"
    r"refer(?:ral|red)?|specialist|consult\w*|prognosis)\b",
    re.IGNORECASE,
)
_TUMOR = re.compile(r"\b(glioma|glioblastoma|astrocytoma|oligodendroglioma|meningioma|"
                    r"pituitary (?:adenoma|tumou?r)|adenoma|metasta\w*|lymphoma)\b", re.IGNORECASE)
_HEDGE = re.compile(r"\b(suspect\w*|suspicious|possible|possibly|may represent|could represent|"
                    r"consistent with|concerning for|differential)\b", re.IGNORECASE)


def check_summary(text: str) -> str | None:
    """Why the summary breaks rule 1, or None if it is fine to show as a draft."""
    t = " ".join(text.split())
    if not t:
        return "empty"
    if m := _FORBIDDEN.search(t):
        return f"forbidden wording: {m.group(0)!r}"
    for sentence in re.split(r"(?<=[.!?])\s+", t):
        if _TUMOR.search(sentence) and not _HEDGE.search(sentence):
            return f"unhedged tumor type: {sentence!r}"
    return None
