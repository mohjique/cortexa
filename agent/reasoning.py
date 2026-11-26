"""MedGemma's clinical reasoning, grounded in the ResNet call and the decision tree.

MedGemma was weak at classifying brain MRI alone (below 0.5 on the test split),
so it does not vote. The ResNet classifier picks the suspected bucket. The
pathway for that bucket comes from this file (docs/decision-tree.md), in code,
so the LLM cannot change which steps the case follows. MedGemma gets the image,
the ResNet call and this pathway, and explains them for the care team: which
visible features are consistent with the suspected bucket, and what each next
step answers. The reasoning supports the ResNet call; it never offers a second
opinion. Whether the call is right is the doctor's decision at confirm_bucket.

`check_reasoning` enforces this in code:
- Rule 1: the reasoning may name the pathway's steps (surgery, pathology, labs),
  because the tree already contains them and the doctor decides each one. It may
  not diagnose, recommend, tell anyone what they should do, refer, or name a
  tumor type without hedging.
- Alignment (given the bucket): it may not contradict the ResNet call, e.g. call
  the image normal when the call is a tumor, name a different tumor type, or
  describe a mass when the call is no tumor.
A text that fails either check is dropped entirely; it is never stored or shown.
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
        "consistent with the suspected call. Then explain what the next steps of this pathway will "
        "answer for the team. Do not add steps that are not listed, and do not name any other "
        f"tumor type. Whenever you name the tumor, call it 'the suspected {BUCKET_TEXT[bucket]}'."
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

# A property of a named tumor type ("the specific type of glioma", "the grade
# of the meningioma") takes the type as settled, so a reasoning hedge in the
# same sentence does not cover it. "Will determine whether this is a glioma"
# or "the grade of the suspected glioma" leave the type open and still pass.
_PRESUMED_TYPE = re.compile(
    r"\b(?:type|subtype|kind|grade|nature|characteristics|features|properties)s?\s+of\s+"
    r"(?:the\s+|this\s+|that\s+|its\s+)?" + _TUMOR.pattern.removeprefix(r"\b"),
    re.IGNORECASE,
)


_FAMILY = {
    "glioma": re.compile(r"\b(glioma|glioblastoma|astrocytoma|oligodendroglioma)s?\b", re.IGNORECASE),
    "meningioma": re.compile(r"\bmeningiomas?\b", re.IGNORECASE),
    "pituitary": re.compile(r"\b(pituitary (?:adenoma|tumou?r|mass|lesion)|adenomas?)\b", re.IGNORECASE),
}
# Saying there is nothing there, when the ResNet call is a tumor.
_NO_FINDING = re.compile(
    r"\b(no (?:obvious |visible |clear |apparent |definite )?(?:mass|masses|lesions?|tumou?rs?|abnormalit\w*)"
    r"|(?:appears?|looks?|is|are)(?: to be)?(?: an?)?(?: entirely| completely)? normal"
    r"|does not (?:show|suggest|appear|look)|not consistent|inconsistent|argues? against|unlikely)\b",
    re.IGNORECASE,
)
_MASS = re.compile(r"\b(mass|masses|lesions?|tumou?rs?)\b", re.IGNORECASE)
_NEGATION = re.compile(r"\b(no|not|without|any|absence)\b", re.IGNORECASE)


def check_alignment(text: str, bucket: str) -> str | None:
    """Why the reasoning contradicts the ResNet call `bucket`, or None if it goes along with it."""
    for sentence in re.split(r"(?<=[.!?])\s+", " ".join(text.split())):
        for other, pat in _FAMILY.items():
            if other != bucket and pat.search(sentence):
                return f"names another tumor type than the call {bucket}"
        if bucket == "no_tumor":
            if _MASS.search(sentence) and not _NEGATION.search(sentence):
                return "describes a mass when the call is no tumor"
        elif "surrounding" not in sentence.lower() and _NO_FINDING.search(sentence):
            # "the surrounding tissue appears normal" supports the call; "the image appears normal" does not
            return f"says nothing is there when the call is {bucket}"
    return None


# The reasons check_alignment returns. A text dropped for one of these
# disagreed with the ResNet call (as opposed to breaking rule 1's wording).
_DISAGREEMENT_PREFIXES = (
    "names another tumor type than the call",
    "describes a mass when the call is",
    "says nothing is there when the call is",
)


def is_disagreement(problem: str | None) -> bool:
    """True when a rejection reason means the reasoning contradicted the ResNet call.

    Used only to tell the doctor the draft was withheld. It never changes the
    suspected bucket, the uncertainty flag or the case's route: MedGemma does
    not vote.
    """
    return bool(problem) and problem.startswith(_DISAGREEMENT_PREFIXES)  # type: ignore[union-attr]


def check_reasoning(text: str, bucket: str | None = None) -> str | None:
    """Why the reasoning can't be shown, or None if it is fine as a draft.

    The reason never quotes the text, so a rejected text is not kept anywhere.

    Always checks rule 1. With `bucket` (the ResNet call), also checks that the
    reasoning goes along with that call.
    """
    t = " ".join(text.split())
    if not t:
        return "empty"
    if _FORBIDDEN.search(t):
        return "forbidden wording (diagnoses, recommends, directs or refers)"
    for sentence in re.split(r"(?<=[.!?])\s+", t):
        if not _TUMOR.search(sentence) or _HEDGE.search(sentence):
            continue
        if _PRESUMED_TYPE.search(sentence):
            return "takes the tumor type as settled"
        if not _REASONING_HEDGE.search(sentence):
            return "unhedged tumor type"
    return check_alignment(t, bucket) if bucket else None
