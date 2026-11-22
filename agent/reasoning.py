"""MedGemma's clinical reasoning, grounded in the ResNet call and the decision tree.

MedGemma was weak at classifying brain MRI alone (below 0.5 on the test split),
so it does not vote. The ResNet classifier picks the suspected bucket. The
pathway for that bucket comes from this file (docs/decision-tree.md), in code,
so the LLM cannot change which steps the case follows. MedGemma gets the image,
the ResNet call and this pathway, and explains them for the care team: which
visible features fit or argue against the suspected bucket, and what each next
step answers.

`check_reasoning` enforces rule 1 in code. The reasoning may name the pathway's
steps (surgery, pathology, labs), because the tree already contains them and the
doctor decides each one. It may not diagnose, recommend, tell anyone what they
should do, refer, or name a tumor type without hedging.
"""

from __future__ import annotations

import re

from agent.findings import _HEDGE, _TUMOR

# One entry per bucket: the decision-tree branch, as the steps the app follows.
PATHWAY: dict[str, list[str]] = {
    "no_tumor": [
        "Doctor confirms or corrects the suspected bucket.",
        "If confirmed as no tumor, the case stops: no trial search.",
    ],
    "meningioma": [
        "Doctor confirms or corrects the suspected bucket.",
        "Doctor decides watch or treat.",
        "Watch: rescan later, no trial search.",
        "Treat: surgery and pathology, then the doctor enters results (diagnosis, grade).",
        "Then trial matching, with each eligibility rule marked met, not met or unknown.",
    ],
    "glioma": [
        "Doctor confirms or corrects the suspected bucket.",
        "Pre-op screen: lists what the biopsy must answer (IDH status, 1p/19q codeletion, "
        "MGMT methylation, grade) and how many trials each answer decides.",
        "Flags any trial that requires enrollment before surgery; the window closes at surgery.",
        "Doctor reviews the pre-op screen, then surgery and pathology.",
        "Doctor enters results; recurrent gliomas with broad sequencing get the mutation check.",
        "Then trial matching, with each eligibility rule marked met, not met or unknown.",
    ],
    "pituitary": [
        "Doctor confirms or corrects the suspected bucket.",
        "Hormone labs first; the doctor marks them reviewed.",
        "Doctor decides surgery or medication only.",
        "Surgery: pathology, then the doctor enters results. Medication only: the doctor "
        "enters hormone and imaging results directly.",
        "Then trial matching, with each eligibility rule marked met, not met or unknown.",
    ],
}

BUCKET_TEXT = {"glioma": "glioma", "meningioma": "meningioma",
               "pituitary": "pituitary tumor", "no_tumor": "no tumor"}

REASONING_SYSTEM = (
    "You support a neuro-oncology care team. An image classifier has already made a "
    "suspected call; a doctor confirms or corrects every call. You explain the imaging and "
    "the care pathway. You never diagnose, never recommend or advise, never say what anyone "
    "should do, and never suggest a referral. Write 'suspected' before any tumor type."
)


def reasoning_prompt(bucket: str, confidence: float, flagged: bool) -> str:
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(PATHWAY[bucket], 1))
    flag = (" The case is flagged uncertain, so the doctor will look harder."
            if flagged else "")
    return (
        f"The classifier's call for this brain MRI slice is: suspected {BUCKET_TEXT[bucket]} "
        f"(confidence {confidence:.2f}).{flag}\n"
        f"The app's pathway for this suspected bucket:\n{steps}\n\n"
        "In 3 to 5 short sentences: first, describe the visible features in the image that are "
        "consistent with the suspected call, and any that argue against it (say so plainly if the "
        "image does not look like it). Then explain what the next steps of this pathway will "
        "answer for the team. Do not add steps that are not listed."
    )


_FORBIDDEN = re.compile(
    r"\b(diagnos(?:is of|ed (?:as|with)|e[ds]?|tic of)|confirm\w* (?:a|an|the)? ?"
    r"(?:glioma|meningioma|pituitary|tumou?r|diagnosis)|definitive(?:ly)?|certainly|"
    r"recommend\w*|advis\w*|should|must(?! answer)|refer(?:ral|red)?|specialist|consult\w*|prognosis)\b",
    re.IGNORECASE,
)

# Hedges beyond agent.findings._HEDGE that the reasoning uses: radiology hedging
# ("could be", "suggest") and sentences about what a later step will determine,
# which describe an open question rather than claim a tumor type. Only the
# future "will determine" counts: "determines the lesion is a glioblastoma" or
# "a glioma, whether or not ..." are flat claims and must stay rejected.
_REASONING_HEDGE = re.compile(
    r"\b(could be|may be|might be|likely|suggest\w*|will determine)\b",
    re.IGNORECASE,
)


def check_reasoning(text: str) -> str | None:
    """Why the reasoning breaks rule 1, or None if it is fine to show as a draft."""
    t = " ".join(text.split())
    if not t:
        return "empty"
    if m := _FORBIDDEN.search(t):
        return f"forbidden wording: {m.group(0)!r}"
    for sentence in re.split(r"(?<=[.!?])\s+", t):
        if _TUMOR.search(sentence) and not (_HEDGE.search(sentence) or _REASONING_HEDGE.search(sentence)):
            return f"unhedged tumor type: {sentence!r}"
    return None
