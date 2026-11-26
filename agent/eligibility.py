"""Eligibility checker (F8): one `met | not_met | unknown` row per criterion.

Deterministic code makes every call. The trial's criteria text is split into
inclusion and exclusion bullets. For each bullet, every rule that applies
runs, not just the first (`evaluate`):

- pattern rules (`RULES`) for phrases such as "prior bevacizumab";
- the tumor types the bullet names, checked against the diagnosis;
- the concepts `agent.concepts` finds (IDH, 1p/19q, MGMT, grade, age,
  pediatric, recurrent, newly diagnosed, sex), checked against Facts
  wherever the bullet states a readable requirement.

Each rule answers whether the criterion's condition holds: yes, no or
unknown. The answers are combined as AND (a failing part decides) unless an
unread "or" means the bullet might be an either/or; then it is unknown.

A bullet never passes on a part of it. A would-be `met` becomes `unknown`
when the bullet mentions a concept no rule checked (coverage guard: H3 K27M,
BRAF, or a value regex could not read) or has content words outside every
span a rule read (full-read guard). A bullet never fails on a misread
either, since a `not_met` hides the trial: one whose wording may make the
failing part not a requirement (a condition, a permission, timing after a
treatment, one arm of several, a heading, or "not eligible" under inclusion)
is `unknown` (not-met guard).

- Rule 3: a missing field gives `unknown` with the reason; it is never
  guessed. A `pending` result also gives `unknown`, with a reason starting
  with `PENDING_PREFIX`, so the shortlist can tell "waiting on the lab" apart.
  When several parts are unknown, a pending one is reported first.
- Rule 4: every row names the patient field it used and the criterion text.
  A `met` built from several parts lists every field in its reason.
- Rule 10: `Facts` has no mutation check fields, so the check cannot decide
  a criterion. A TMB criterion reads `tmb_clia` (a CLIA lab result) only.
- A bullet that matches no rule is `unknown`, "doctor review". Live trials
  have criteria the library does not cover; those are left to the doctor.

For an exclusion, `met` means the exclusion does not apply.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from agent.audit import audit
from agent.concepts import ConceptHit, detect
from agent.policy import Policy, get_policy
from agent.presurgery import PhraseMatch, inclusion_match
from agent.schemas import (
    Bucket,
    DoctorResults,
    EligibilityResult,
    EligibilitySummary,
    PituitaryResults,
    PriorTreatment,
    TrialRecord,
)

PENDING_PREFIX = "pending result: "
NO_RULE_FIELD = "doctor_review"
NO_RULE_REASON = "no automatic rule for this criterion; doctor must review"

CaseStage = Literal["preop", "post_pathology"]
CriterionType = Literal["inclusion", "exclusion"]


# ---------------------------------------------------------------------------
# Patient facts the rules may read
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Facts:
    """What the checker knows about a patient. `None` = not recorded.

    Built from doctor-entered results after pathology, or from what is known
    at the pre-op screen. No mutation check field lives here (rule 10).
    """

    stage: CaseStage
    bucket: Bucket | None
    age: int | None = None
    sex: str | None = None  # "female" | "male"; None = not recorded
    diagnosis: str | None = None
    grade: int | str | None = None
    idh: str | None = None
    codeletion_1p19q: str | None = None
    mgmt: str | None = None
    tmb_clia: float | None = None
    recurrent: bool | None = None
    recurrence_number: int | None = None
    prior_treatments: tuple[PriorTreatment, ...] | None = None
    comorbidities: tuple[str, ...] | None = None
    can_use_phone_or_tablet: bool | None = None
    pituitary: PituitaryResults | None = None
    radiology_report: str | None = None

    @classmethod
    def from_results(cls, r: DoctorResults) -> Facts:
        rec_n = r.recurrence_number
        if rec_n is None and not r.recurrent:
            rec_n = 0  # newly diagnosed: no recurrences, by definition
        return cls(
            stage="post_pathology",
            bucket=r.confirmed_bucket,
            age=r.age,
            sex=r.sex,
            diagnosis=r.diagnosis,
            grade=r.grade,
            idh=r.idh,
            codeletion_1p19q=r.codeletion_1p19q,
            mgmt=r.mgmt,
            tmb_clia=r.tmb_clia,
            recurrent=r.recurrent,
            recurrence_number=rec_n,
            prior_treatments=tuple(r.prior_treatments),
            comorbidities=None if r.comorbidities is None else tuple(r.comorbidities),
            can_use_phone_or_tablet=r.can_use_phone_or_tablet,
            pituitary=r.pituitary,
        )


# ---------------------------------------------------------------------------
# Findings: does the criterion's condition hold?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    holds: bool | None  # None = unknown
    field: str
    reason: str
    pending: bool = False


def _yes(f: str, reason: str) -> Finding:
    return Finding(True, f, reason)


def _no(f: str, reason: str) -> Finding:
    return Finding(False, f, reason)


# Fields that come from pathology. A missing one uses the policy.yaml reason
# template ("needs pathology: IDH status"); anything else is "not recorded".
PATHOLOGY_FIELDS = frozenset({"diagnosis", "grade", "idh", "codeletion_1p19q", "mgmt"})


def _missing(f: str, label: str, policy: Policy) -> Finding:
    if f in PATHOLOGY_FIELDS:
        return Finding(None, f, policy.eligibility.missing_field_reason_template.format(field=label))
    return Finding(None, f, f"not recorded: {label}; doctor must enter it")


def _pending(f: str, label: str) -> Finding:
    return Finding(None, f, f"{PENDING_PREFIX}{label} ordered, not back yet", pending=True)


def _review(f: str, reason: str) -> Finding:
    return Finding(None, f, f"doctor review: {reason}")


def _all_of(*fs: Finding, field: str | None = None) -> Finding:
    """Holds only if every part holds. A failing part decides; then unknowns.

    `field` names the evidence when every part holds (default: the first part's).
    """
    for f in fs:
        if f.holds is False:
            return f
    unknown = [f for f in fs if f.holds is None]
    if unknown:
        return next((f for f in unknown if f.pending), unknown[0])
    return Finding(True, field or fs[0].field, "; ".join(f.reason for f in fs))


def _wrong_bucket(facts: Facts, want: Bucket) -> Finding | None:
    """A doctor-confirmed bucket that rules the criterion out, or None."""
    if facts.bucket is not None and facts.bucket != want:
        return _no("confirmed_bucket", f"confirmed bucket is {facts.bucket}, not {want}")
    return None


def _any_of(*fs: Finding) -> Finding:
    """Holds if any part holds. Else unknown if any part is unknown, else fails."""
    for f in fs:
        if f.holds:
            return f
    unknown = [f for f in fs if f.holds is None]
    if unknown:
        return next((f for f in unknown if f.pending), unknown[0])
    return fs[0]


def _not(f: Finding, reason: str | None = None) -> Finding:
    if f.holds is None:
        return f
    return Finding(not f.holds, f.field, reason or f.reason, f.pending)


# ---------------------------------------------------------------------------
# Field readers
# ---------------------------------------------------------------------------

MARKER_LABELS = {
    "idh": "IDH status",
    "codeletion_1p19q": "1p/19q codeletion status",
    "mgmt": "MGMT promoter methylation status",
    "grade": "WHO grade",
}

# Diagnosis text the doctor entered may state a marker outright
# ("Glioblastoma, IDH-wildtype"). Reading that is not inference.
_DX_MARKERS = {
    "idh": [(re.compile(r"idh[- ]?mutant"), "mutant"), (re.compile(r"idh[- ]?wild[- ]?type"), "wildtype")],
    "codeletion_1p19q": [
        (re.compile(r"1p/19q[- ]?(co)?deleted"), "codeleted"),
        (re.compile(r"1p/19q[- ]?(intact|non[- ]?codeleted)"), "intact"),
    ],
}


def _marker(facts: Facts, name: str, policy: Policy) -> tuple[str | None, Finding | None]:
    """The marker value, or a Finding saying why it isn't available."""
    label = MARKER_LABELS[name]
    value = getattr(facts, name)
    if value in ("mutant", "wildtype", "codeleted", "intact", "methylated", "unmethylated"):
        return value, None
    dx = (facts.diagnosis or "").lower()
    for pat, v in _DX_MARKERS.get(name, []):
        if pat.search(dx):
            return v, None
    if value == "pending":
        return None, _pending(name, label)
    return None, _missing(name, label, policy)


def _grade(facts: Facts, policy: Policy) -> tuple[int | None, Finding | None]:
    g = facts.grade
    if isinstance(g, int):
        return g, None
    m = re.search(r"grade\s*([1-4])", (facts.diagnosis or "").lower())
    if m:
        return int(m.group(1)), None
    if g == "pending":
        return None, _pending("grade", MARKER_LABELS["grade"])
    return None, _missing("grade", MARKER_LABELS["grade"], policy)


def _has_treatment(facts: Facts, kinds: set[str], label: str, policy: Policy) -> Finding:
    if facts.prior_treatments is None:
        return _missing("prior_treatments", "prior treatments", policy)
    if any(t.type in kinds for t in facts.prior_treatments):
        return _yes("prior_treatments", f"prior {label} recorded")
    if any(t.type == "other" for t in facts.prior_treatments):
        return _review("prior_treatments", f"a prior treatment of type 'other' may be {label}")
    return _no("prior_treatments", f"no prior {label} recorded")


def _not_recurrent(facts: Facts, policy: Policy) -> Finding:
    if facts.recurrent is None:
        return _missing("recurrent", "recurrence status", policy)
    if facts.recurrent:
        return _no("recurrent", "tumor is recurrent, not newly diagnosed")
    return _yes("recurrent", "newly diagnosed (not recurrent)")


def _comorbidity(facts: Facts, terms: Iterable[str], label: str, policy: Policy,
                 category: bool = False) -> Finding:
    """Is a comorbidity present? An empty list means "none" (interfaces.md)."""
    if facts.comorbidities is None:
        return _missing("comorbidities", "comorbidities", policy)
    listed = [c.lower() for c in facts.comorbidities]
    for c in listed:
        if any(t in c for t in terms):
            return _yes("comorbidities", f"{label} listed: {c}")
    if listed and category:
        # A category ("autoimmune disease") can hide under a specific name.
        return _review("comorbidities", f"check whether {listed} include {label}")
    return _no("comorbidities", f"no {label} in comorbidities {listed}")


# ---------------------------------------------------------------------------
# Rules. Each takes (facts, regex match, policy) and returns a Finding about
# whether the criterion's condition holds. Every rule whose pattern matches a
# bullet runs (see `evaluate`); a rule whose match lies inside an earlier
# rule's match is skipped, so specific patterns come before general ones.
# ---------------------------------------------------------------------------

RuleFn = Callable[[Facts, re.Match[str], Policy], Finding]

GLIOMA_FAMILY = ("glioblastoma", "astrocytoma", "oligodendroglioma", "glioma")
ENTITY_BUCKET = {e: "glioma" for e in GLIOMA_FAMILY} | {"meningioma": "meningioma"}
_ENTITY_WORD = r"(?:glioblastoma|astrocytoma|oligodendroglioma|meningioma|glioma|gbm)s?"
_ENTITY_RE = re.compile(rf"\b({_ENTITY_WORD})\b", re.IGNORECASE)
# "glioblastoma or astrocytoma", "astrocytoma, oligodendroglioma and glioma": one span.
_ENTITY_LIST = re.compile(rf"\b{_ENTITY_WORD}(?:\s*(?:,|or|and/or|/)\s*(?:\w+[\s-]+){{0,2}}{_ENTITY_WORD})*\b",
                          re.IGNORECASE)
# A "glioma" with one of these words just before it is a different tumor
# type (diffuse midline glioma, pediatric-type high-grade glioma, DIPG), not
# any glioma. The checker does not decide those; the doctor does.
GLIOMA_SUBTYPE_WORDS = (
    "midline", "pontine", "brainstem", "brain stem", "optic", "pathway", "intrinsic", "pediatric-type",
    "paediatric-type", "pediatric type", "infant-type", "pilocytic", "angiocentric", "chordoid", "tectal",
    "spinal", "astroblastoma",
)


def _entity(facts: Facts, entity: str, policy: Policy) -> Finding:
    entity = entity.lower().rstrip("s")
    entity = "glioblastoma" if entity == "gbm" else entity
    want_bucket = ENTITY_BUCKET[entity]
    if facts.bucket is not None and facts.bucket != want_bucket:
        return _no("confirmed_bucket", f"confirmed bucket is {facts.bucket}, criterion needs {entity}")
    if facts.diagnosis is None:
        return _missing("diagnosis", "diagnosis", policy)
    dx = facts.diagnosis.lower()
    if entity == "glioma":
        ok = any(e in dx for e in GLIOMA_FAMILY)
    else:
        ok = entity in dx
    if ok:
        return _yes("diagnosis", f"diagnosis: {facts.diagnosis}")
    if _who_naming_overlap(entity, dx, facts):
        return _review("diagnosis", f"diagnosis is {facts.diagnosis}; criterion says {entity}. WHO naming "
                       "overlaps here (older or grade-based terms), so the doctor must decide")
    return _no("diagnosis", f"diagnosis is {facts.diagnosis}, not {entity}")


def _who_naming_overlap(entity: str, dx: str, facts: Facts) -> bool:
    """Astrocytoma and glioblastoma names overlap across WHO editions.

    "WHO grade 4 astrocytoma" or "diffuse astrocytoma, IDH-wildtype" may
    include glioblastoma, and an IDH-mutant grade 4 astrocytoma was once
    called glioblastoma. A mismatch between the two is not a confident no.
    """
    if entity == "astrocytoma" and "glioblastoma" in dx:
        return True
    if entity == "glioblastoma" and "astrocytoma" in dx:
        return facts.grade == 4 or "grade 4" in dx or "grade iv" in dx
    return False


def _entities(text: str, facts: Facts, policy: Policy) -> Finding | None:
    """The tumor types a bullet names, checked against the diagnosis.

    "astrocytoma or oligodendroglioma": any one listed type is enough. A
    glioma subtype (see GLIOMA_SUBTYPE_WORDS) is doctor review.
    """
    parts: list[Finding] = []
    seen: set[str] = set()
    for m in _ENTITY_RE.finditer(text):
        word = m.group(1).lower()
        before = text[max(0, m.start() - 30):m.start()].lower()
        if word.rstrip("s") == "glioma" and any(w in before for w in GLIOMA_SUBTYPE_WORDS):
            key = f"subtype:{before.strip()[-20:]}"
            if key not in seen:
                parts.append(_review("diagnosis", f"glioma subtype {(before.split() or [''])[-1]!r} glioma "
                                     "is not checked automatically"))
                seen.add(key)
            continue
        if word not in seen:
            seen.add(word)
            parts.append(_entity(facts, word, policy))
    return _any_of(*parts) if parts else None


def _histology(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    """'Histologically confirmed <entity>': the diagnosis part only.

    Markers, grade and recurrence in the same bullet are checked by the
    concept rules in `evaluate`, so none of them can pass on the diagnosis.
    """
    found = _entities(m.string, facts, policy)
    return found if found is not None else _review(NO_RULE_FIELD, "histology not recognized")


def _marker_is(facts: Facts, name: str, want: str, policy: Policy) -> Finding:
    value, why = _marker(facts, name, policy)
    if why:
        return why
    label = MARKER_LABELS[name]
    if value == want:
        return _yes(name, f"{label}: {value}")
    return _no(name, f"{label} is {value}, criterion needs {want}")


def _grade_in(facts: Facts, lo: int, hi: int, policy: Policy) -> Finding:
    g, why = _grade(facts, policy)
    if g is None:
        return why  # type: ignore[return-value]
    span = f"{lo}" if lo == hi else f"{lo} to {hi}"
    src = "grade" if isinstance(facts.grade, int) else "diagnosis"  # read from the diagnosis text
    if lo <= g <= hi:
        return _yes(src, f"WHO grade {g} is within {span}")
    return _no(src, f"WHO grade {g} is outside {span}")


def _age_in(facts: Facts, lo: float | None, hi: float | None, policy: Policy) -> Finding:
    if facts.age is None:
        return _missing("age", "age", policy)
    if lo is not None and hi is not None:
        span = f"{lo:g} to {hi:g}"
    elif lo is not None:
        span = f"{lo:g} or older"
    else:
        span = f"{hi:g} or younger"
    if (lo is not None and facts.age < lo) or (hi is not None and facts.age > hi):
        return _no("age", f"age {facts.age} is outside {span}")
    return _yes("age", f"age {facts.age} is within {span}")


def _imaging_suspicious_gbm(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    if wrong := _wrong_bucket(facts, "glioma"):
        return wrong
    new = _not_recurrent(facts, policy)
    if new.holds is not True:
        return new
    rep = facts.radiology_report
    if rep is None:
        return _missing("radiology_report", "radiology report", policy)
    if re.search(r"glioblastoma|high[- ]grade glioma", rep.lower()):
        return _yes("radiology_report", "radiology report raises suspicion for high-grade glioma")
    return _review("radiology_report", "report does not state suspected glioblastoma")


def _before_surgery(facts: Facts, match: PhraseMatch, policy: Policy) -> Finding:
    """A bullet timed before surgery (`agent.presurgery`, phrases from policy.yaml).

    Never `not_met`. A phrase cannot tell "enroll before surgery" from "MRI
    before surgery" or from a re-resection trial, and a not_met would hide the
    trial on a guess (rule 3). After surgery the doctor decides; a trial the
    doctor declined at `preop_review` is hidden by `agent.matching`, which
    cites that decision.
    """
    what = "may need enrollment before surgery" if match.enrollment else "is timed before surgery"
    if facts.stage == "post_pathology":
        return _review("stage", f"criterion {what} ({match.phrase!r}) and surgery or biopsy is done; "
                                "confirm whether the patient still qualifies")
    surgery = _has_treatment(facts, {"surgery"}, "surgery", policy)
    if surgery.holds:
        return _review("prior_treatments", f"criterion {what} ({match.phrase!r}) and a prior surgery "
                                           "is recorded; confirm whether it counts")
    return _yes("stage", "case is at the pre-op screen, before surgery")


def _newly_dx_no_rt_chemo(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    return _all_of(
        _not_recurrent(facts, policy),
        _not(_has_treatment(facts, {"radiation"}, "radiation", policy)),
        _not(_has_treatment(facts, {"temozolomide"}, "chemotherapy", policy)),
        field="prior_treatments",
    )


def _newly_dx_after_surgery(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    return _all_of(
        _not_recurrent(facts, policy),
        _has_treatment(facts, {"surgery"}, "surgery or biopsy", policy),
        field="prior_treatments",
    )


def _newly_dx(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    return _not_recurrent(facts, policy)


_NEGATED_BEFORE = re.compile(
    r"\b(?:no|not|without|never|nor|naive|na[iï]ve\s+to|free\s+of|other\s+than|except|excluding|recovered\s+from"
    r"|toxicit(?:y|ies)\s+(?:of|from)|effects?\s+of|complications?\s+(?:of|from))\b[^.;]{0,40}$",
    re.IGNORECASE,
)


def _negated(m: re.Match[str]) -> bool:
    """Negated or side-effect wording just before a match ("no prior radiation",
    "recovered from the toxic effects of prior surgery")."""
    return bool(_NEGATED_BEFORE.search(m.string[max(0, m.start() - 60):m.start()]))


def _treatment_rule(kinds: set[str], label: str) -> RuleFn:
    def rule(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
        if _negated(m):
            return _review("prior_treatments", f"negated or side-effect wording around prior {label}")
        return _has_treatment(facts, kinds, label, policy)
    return rule


def _rt_and_tmz(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    # Both parts cite prior_treatments, so the combined finding does too.
    return _all_of(
        _has_treatment(facts, {"radiation"}, "radiation", policy),
        _has_treatment(facts, {"temozolomide"}, "temozolomide", policy),
    )


def _resection_extent(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    if facts.prior_treatments is None:
        return _missing("prior_treatments", "prior treatments", policy)
    surg = [t for t in facts.prior_treatments if t.type == "surgery"]
    if not surg:
        return _no("prior_treatments", "no surgery recorded")
    extents = {t.extent for t in surg}
    if extents & {"gross_total", "subtotal"}:
        done = sorted(e for e in extents if e)
        return _yes("prior_treatments", f"resection extent: {', '.join(done)}")
    if None in extents:
        return _missing("prior_treatments", "extent of resection", policy)
    return _no("prior_treatments", "biopsy only, no resection")


def _pit_radiation_within(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    years = int(m.group("n"))
    if facts.prior_treatments is None:
        return _missing("prior_treatments", "prior treatments", policy)
    rt = [t for t in facts.prior_treatments if t.type == "pituitary_radiation"]
    if not rt:
        return _no("prior_treatments", "no pituitary radiation recorded")
    if any(t.weeks_ago is not None and t.weeks_ago <= years * 52 for t in rt):
        return _yes("prior_treatments", f"pituitary radiation within {years} years")
    if any(t.weeks_ago is None for t in rt):
        return _missing("prior_treatments", "date of pituitary radiation", policy)
    return _no("prior_treatments", f"pituitary radiation more than {years} years ago")


_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4}


def _first_or_second(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    if facts.recurrent is False:
        return _no("recurrent", "newly diagnosed, not a recurrence")
    if facts.recurrence_number is None:
        return _missing("recurrence_number", "recurrence number", policy)
    n = facts.recurrence_number
    if n in (1, 2):
        return _yes("recurrence_number", f"recurrence number {n}")
    return _no("recurrence_number", f"recurrence number {n}")


def _more_than_recurrences(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    raw = m.group("n")
    limit = int(raw) if raw.isdigit() else _WORDS[raw.lower()]
    if facts.recurrence_number is None:
        return _missing("recurrence_number", "recurrence number", policy)
    n = facts.recurrence_number
    if n > limit:
        return _yes("recurrence_number", f"recurrence number {n} is more than {limit}")
    return _no("recurrence_number", f"recurrence number {n} is not more than {limit}")


def _recurrent_glioma(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    if wrong := _wrong_bucket(facts, "glioma"):
        return wrong
    if facts.recurrent is None:
        return _missing("recurrent", "recurrence status", policy)
    if facts.recurrent:
        return _yes("recurrent", "recurrent glioma")
    return _no("recurrent", "newly diagnosed, not recurrent")


def _tmb(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    """CLIA TMB only. The research mutation count is not TMB (rule 10)."""
    thr = re.search(r"(\d+(?:\.\d+)?)\s*(?:mutations?|mut)\s*/\s*mb", m.string.lower())
    if thr is None:
        return _review("tmb_clia", "TMB threshold not recognized")
    cutoff = float(thr.group(1))
    if facts.tmb_clia is None:
        return Finding(None, "tmb_clia", "needs a CLIA-certified TMB result; the research "
                       "mutation check count is not TMB and cannot satisfy this")
    if facts.tmb_clia >= cutoff:
        return _yes("tmb_clia", f"CLIA TMB {facts.tmb_clia} mut/Mb ≥ {cutoff:g}")
    return _no("tmb_clia", f"CLIA TMB {facts.tmb_clia} mut/Mb < {cutoff:g}")


def _device(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    """Holds when the patient CAN take part from home with a phone or tablet."""
    v = facts.can_use_phone_or_tablet
    if v is None:
        return _missing("can_use_phone_or_tablet", "phone or tablet use", policy)
    return _yes("can_use_phone_or_tablet", "can use a phone or tablet") if v else _no(
        "can_use_phone_or_tablet", "cannot use a phone or tablet")


def _unable_device(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    return _not(_device(facts, m, policy))


AUTOIMMUNE_TERMS = (
    "autoimmune", "lupus", "rheumatoid", "multiple sclerosis", "crohn", "ulcerative colitis",
    "psoria", "type 1 diabetes", "hashimoto", "graves", "myasthenia", "celiac", "sjogren",
    "sjögren", "vasculitis", "sarcoid",
)


def _secreting(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    want = {"acth": "acth", "prolactin": "prolactin", "growth hormone": "growth_hormone"}[m.group("h").lower()]
    if wrong := _wrong_bucket(facts, "pituitary"):
        return wrong
    p = facts.pituitary
    if p is None:
        return _missing("pituitary", "pituitary hormone results", policy)
    h = p.secreting_hormone
    if h == "pending":
        return _pending("pituitary.secreting_hormone", "hormone testing")
    if h == "unknown":
        return _missing("pituitary.secreting_hormone", "secreting hormone", policy)
    if h == want:
        return _yes("pituitary.secreting_hormone", f"secreting hormone: {h}")
    return _no("pituitary.secreting_hormone", f"secreting hormone is {h}, criterion needs {want}")


def _persistent_or_not_candidate(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    p = facts.pituitary
    if p is None:
        return _missing("pituitary", "pituitary results", policy)
    if p.surgical_candidate is False:
        return _yes("pituitary.surgical_candidate", "not a surgical candidate")
    if facts.prior_treatments and any(t.type == "surgery" for t in facts.prior_treatments):
        return _review("pituitary", "surgery done; persistent disease after surgery is not recorded")
    if p.surgical_candidate is None:
        return _missing("pituitary.surgical_candidate", "surgical candidacy", policy)
    return _no("pituitary.surgical_candidate", "surgical candidate with no surgery yet")


@dataclass(frozen=True)
class Rule:
    """A pattern, the function that checks it, and the concepts it reads.

    `covers` names the `agent.concepts` concepts (plus "entity", the tumor
    type) the rule really checks. A concept the bullet mentions that no rule
    covers can never be passed over: see `evaluate`.
    """

    pattern: re.Pattern[str]
    fn: RuleFn
    covers: frozenset[str] = frozenset()


def _rule(pattern: str, fn: RuleFn, *covers: str) -> Rule:
    return Rule(re.compile(pattern, re.IGNORECASE), fn, frozenset(covers))


_I = re.IGNORECASE
RULES: list[Rule] = [
    _rule(r"imaging suspicious for newly diagnosed glioblastoma", _imaging_suspicious_gbm,
          "entity", "newly_diagnosed"),
    _rule(r"(?:histologically|histopathologically|pathologically) confirmed", _histology, "entity"),
    _rule(r"newly diagnosed;?\s*no prior radiation or chemotherapy", _newly_dx_no_rt_chemo, "newly_diagnosed"),
    _rule(r"newly diagnosed,?\s*after surgery or biopsy", _newly_dx_after_surgery, "newly_diagnosed"),
    _rule(r"^\s*newly diagnosed\s*$", _newly_dx, "newly_diagnosed"),
    _rule(r"prior radiation and temozolomide", _rt_and_tmz),
    _rule(r"pituitary radiation within the past (?P<n>\d+) years?", _pit_radiation_within),
    _rule(r"prior (?:brain )?(?:surgery|resection)(?: or biopsy)?(?: for this tumou?r)?"
          r"|surgery or biopsy for this tumou?r",
          _treatment_rule({"surgery"}, "surgery or biopsy")),
    _rule(r"prior radiation(?: therapy)?(?: to the (?:brain|head|tumou?r|meningioma))?",
          _treatment_rule({"radiation"}, "radiation")),
    _rule(r"prior temozolomide", _treatment_rule({"temozolomide"}, "temozolomide")),
    _rule(r"prior bevacizumab", _treatment_rule({"bevacizumab"}, "bevacizumab")),
    _rule(r"(?:prior (?:treatment|therapy) with (?:an? )?)?idh inhibitors?",
          _treatment_rule({"idh_inhibitor"}, "IDH inhibitor"), "idh"),
    _rule(r"(?:prior (?:immune )?)?checkpoint inhibitors?",
          _treatment_rule({"checkpoint_inhibitor"}, "checkpoint inhibitor")),
    _rule(r"gross total or subtotal resection", _resection_extent),
    _rule(r"first or second recurrence", _first_or_second, "recurrent"),
    _rule(r"more than (?P<n>\d+|one|two|three|four) prior recurrences?", _more_than_recurrences, "recurrent"),
    _rule(r"recurrent glioma", _recurrent_glioma, "recurrent", "entity"),
    _rule(r"tumou?r mutational burden(?: \(tmb\))?(?: of \d+(?:\.\d+)? mutations?/mb or (?:higher|more|greater))?"
          r"(?: by a clia[- ]certified assay)?|\btmb\b", _tmb),
    _rule(r"participation is fully remote(?: from home)?|remote participation", _device),
    _rule(r"unable to use a phone or tablet(?: for study visits)?", _unable_device),
    _rule(r"neurofibromatosis type 2|\bnf2\b",
          lambda f, m, p: _comorbidity(f, ("neurofibromatosis type 2", "nf2"), "neurofibromatosis type 2", p)),
    _rule(r"(?:active )?autoimmune disease",
          lambda f, m, p: _comorbidity(f, AUTOIMMUNE_TERMS, "active autoimmune disease", p, category=True)),
    _rule(r"(?P<h>acth|prolactin|growth hormone)[- ]secreting pituitary(?: adenoma)?(?: confirmed by hormone testing)?",
          _secreting),
    _rule(r"persistent disease after surgery,? or not a surgical candidate", _persistent_or_not_candidate),
]


# ---------------------------------------------------------------------------
# Criteria splitting
# ---------------------------------------------------------------------------

_BULLET = re.compile(r"^\s*(?:[*\-•·]|\d+[.)])\s+")
# "Inclusion Criteria:", and prefixed forms ClinicalTrials.gov often uses
# ("Key Exclusion Criteria:", "Main Inclusion Criteria:").
_HEADER = re.compile(
    r"^\s*(?:(?:key|main|major|primary|general|additional|other)\s+)?"
    r"(inclusion|exclusion)\s+criteria\s*:?\s*$",
    _I,
)


@dataclass(frozen=True)
class Criterion:
    type: CriterionType
    text: str


def split_criteria(text: str) -> list[Criterion]:
    """Bullets under 'Inclusion Criteria:' / 'Exclusion Criteria:' headers."""
    out: list[Criterion] = []
    kind: CriterionType = "inclusion"
    current: list[str] | None = None

    def flush() -> None:
        if current:
            joined = " ".join(" ".join(current).split())
            if joined:
                out.append(Criterion(kind, joined))

    for line in text.splitlines():
        h = _HEADER.match(line)
        if h:
            flush()
            current = None
            kind = h.group(1).lower()  # type: ignore[assignment]
            continue
        if _BULLET.match(line):
            flush()
            current = [_BULLET.sub("", line, count=1)]
        elif line.strip():
            if current is None:
                current = [line]
            else:
                current.append(line)
        else:
            flush()
            current = None
    flush()
    return out


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


def _pre_surgery_phrase(criterion: Criterion, policy: Policy) -> PhraseMatch | None:
    """A pre-surgery inclusion phrase from policy.yaml, firm or possible.

    Same matching as the pre-op screen (`agent.presurgery`).
    """
    if criterion.type != "inclusion":
        return None
    return inclusion_match(criterion.text, policy.pre_surgery_detection)


# ---------------------------------------------------------------------------
# Concept rules: requirements `agent.concepts` reads from any wording
# ---------------------------------------------------------------------------

# The Facts field each concept is checked against. h3k27m and braf have no
# Facts field, so a bullet naming them is never passed (rule 3).
CONCEPT_FIELD: dict[str, str] = {
    "idh": "idh", "codeletion_1p19q": "codeletion_1p19q", "mgmt": "mgmt", "grade": "grade",
    "age_range": "age", "pediatric": "age", "recurrent": "recurrent", "newly_diagnosed": "recurrent",
    "sex": "sex", "h3k27m": "h3k27m", "braf": "braf",
}
CONCEPT_LABEL: dict[str, str] = MARKER_LABELS | {
    "age_range": "an age range", "pediatric": "pediatric patients", "recurrent": "recurrent or progressive disease",
    "newly_diagnosed": "newly diagnosed disease", "sex": "sex", "h3k27m": "H3 K27M status", "braf": "BRAF status",
}

# Wording that states no requirement for a concept: "IDH-mutant or
# IDH-wildtype", "regardless of IDH status", "glioma of any grade".
_NO_REQUIREMENT: dict[str, re.Pattern[str]] = {
    "idh": re.compile(
        r"(?:regardless|irrespective|independent)\s+of\s+(?:the\s+)?(?:tumou?r'?s?\s+)?idh"
        r"|\bidh[- ]?mut\w*\s+(?:or|and/or)\s+idh[- ]?wild[- ]?type\b"
        r"|\bidh[- ]?wild[- ]?type\s+(?:or|and/or)\s+idh[- ]?mut",
        _I),
    "grade": re.compile(r"\bany\s+(?:who\s+)?grade\b"
                        r"|(?:regardless|irrespective)\s+of\s+(?:the\s+)?(?:who\s+)?grade", _I),
    "codeletion_1p19q": re.compile(r"(?:regardless|irrespective)\s+of\s+(?:the\s+)?1p", _I),
    "mgmt": re.compile(r"(?:regardless|irrespective)\s+of\s+(?:the\s+)?mgmt", _I),
}
_GRADE_NUMBER = re.compile(r"\bgrade\s*(?:[1-4]|iv|iii|ii|i)\b|\bwho\s+(?:[1-4]|iv|iii|ii)\b", _I)
_GRADE_WORD = re.compile(r"\bgrade\b", _I)
_HIGH_GRADE = re.compile(r"\bhigh[\s-]*grade\b", _I)
_LOW_GRADE = re.compile(r"\blow[\s-]*grade\b", _I)
_ADULT_WORDS = re.compile(r"\badults?\b|\bolder\b|\bover\b", _I)
_NEGATED_CONCEPT = re.compile(r"\b(?:no|not|without|non|absence\s+of|free\s+of|never|prior\s+to)\b[\s\w-]{0,25}$", _I)
_EITHER_WAY = re.compile(r"\bwith\s+or\s+without\b|\bor\s+not\b|\bwhether\b", _I)


def _span_at(text: str, span: str) -> tuple[int, int] | None:
    i = text.find(span)
    return (i, i + len(span)) if i >= 0 else None


def _negated_span(text: str, span: str) -> bool:
    at = _span_at(text, span)
    return at is not None and bool(_NEGATED_CONCEPT.search(text[max(0, at[0] - 40):at[0]]))


def _range(value: str) -> tuple[float | None, float | None]:
    lo, _, hi = value.partition("-")
    return (float(lo) if lo else None, float(hi) if hi else None)


def _word_grade(text: str) -> tuple[int, int] | None:
    """'High-grade glioma' with no numeric grade: WHO 3 to 4 (low-grade: 1 to 2)."""
    if _GRADE_NUMBER.search(text):
        return None
    high, low = len(_HIGH_GRADE.findall(text)), len(_LOW_GRADE.findall(text))
    if high + low != len(_GRADE_WORD.findall(text)) or (high and low):
        return None  # a bare "grade" somewhere, or both words
    return (3, 4) if high else (1, 2) if low else None


ConceptCall = Finding | bool | None  # a Finding; True = covered, no requirement; None = not read

_R132H = re.compile(r"r\s*132\s*h", re.IGNORECASE)


def _idh_r132h(facts: Facts, want: str, policy: Policy) -> Finding:
    """A criterion about the IDH1 R132H variant only (the antibody test).

    IDH-wildtype implies R132H-wildtype, and rules out R132H-positive. An
    IDH-mutant patient may carry another IDH1/IDH2 variant, so for them the
    criterion is unknown unless the variant is recorded (it is not in Facts).
    """
    value, why = _marker(facts, "idh", policy)
    if why:
        return why
    if value == "wildtype":
        if want == "wildtype":
            return _yes("idh", "IDH status: wildtype, so IDH1 R132H-wildtype")
        return _no("idh", "IDH status is wildtype, criterion needs IDH1 R132H-positive")
    return _review("idh", "IDH status is mutant, but the criterion is about the IDH1 R132H variant only; "
                   "the doctor must check which IDH variant the pathology found")


def _concept_rule(hit: ConceptHit, hits: dict[str, ConceptHit], text: str, kind: CriterionType,
                  facts: Facts, policy: Policy) -> ConceptCall:
    """Check one concept the bullet mentions against Facts, if its value is readable."""
    name = hit.name
    if name in _NO_REQUIREMENT and _NO_REQUIREMENT[name].search(text):
        return True
    if name == "idh" and hit.value and _R132H.search(text):
        return _idh_r132h(facts, hit.value, policy)
    if name in ("idh", "codeletion_1p19q", "mgmt"):
        return _marker_is(facts, name, hit.value, policy) if hit.value else None
    if name == "grade":
        if hit.value:
            lo, hi = _range(hit.value)
            return _grade_in(facts, int(lo or 1), int(hi or 4), policy)
        word = _word_grade(text)
        return _grade_in(facts, *word, policy) if word else None
    if name == "age_range":
        return _age_in(facts, *_range(hit.value), policy) if hit.value else None
    if name == "pediatric":
        age = hits.get("age_range")
        if age is not None:
            # "Pediatric patients (8-17 years of age)": the age range decides.
            hi = _range(age.value)[1] if age.value else None
            return True if hi is not None and hi < 18 else None
        if kind != "inclusion" or _ADULT_WORDS.search(text):
            return None
        if facts.age is None:
            return _missing("age", "age", policy)
        if facts.age >= 18:
            return _no("age", f"criterion is for pediatric patients ({hit.span!r}); patient is age {facts.age}")
        return None  # a child: what else the bullet asks is for the doctor
    if name in ("recurrent", "newly_diagnosed"):
        if {"recurrent", "newly_diagnosed"} <= set(hits) or _EITHER_WAY.search(text) or _negated_span(text, hit.span):
            return None
        if name == "newly_diagnosed":
            return _not_recurrent(facts, policy)
        if facts.recurrent is None:
            return _missing("recurrent", "recurrence status", policy)
        if facts.recurrent:
            return _yes("recurrent", f"recurrent tumor (criterion: {hit.span!r})")
        return _no("recurrent", f"tumor is newly diagnosed, not recurrent (criterion: {hit.span!r})")
    if name == "sex":
        if hit.value is None:
            return None  # "male or female", "any gender"
        if facts.sex is None:
            return _missing("sex", "sex", policy)
        if facts.sex == hit.value:
            return _yes("sex", f"sex {facts.sex} (criterion: {hit.span!r})")
        return _no("sex", f"sex {facts.sex}; criterion names {hit.value} ({hit.span!r})")
    return None  # h3k27m, braf: no Facts field


def _unread(hit: ConceptHit, facts: Facts, policy: Policy) -> Finding:
    """Why a mentioned concept that no rule checked leaves the bullet unknown."""
    name, label = hit.name, CONCEPT_LABEL[hit.name]
    if name in ("h3k27m", "braf"):
        return Finding(None, name, policy.eligibility.missing_field_reason_template.format(field=label))
    why: Finding | None = None
    if name in ("idh", "codeletion_1p19q", "mgmt"):
        why = _marker(facts, name, policy)[1]
    elif name == "grade":
        why = _grade(facts, policy)[1]
    elif name in ("age_range", "pediatric") and facts.age is None:
        why = _missing("age", "age", policy)
    elif name in ("recurrent", "newly_diagnosed") and facts.recurrent is None:
        why = _missing("recurrent", "recurrence status", policy)
    if why is not None:
        return why
    return Finding(None, f"{NO_RULE_FIELD}:{name}",
                   f"doctor review: mentions {label} ({hit.span!r}); no rule checked it")


def _guard(unread: list[ConceptHit], facts: Facts, policy: Policy, checked: Finding | None) -> Finding:
    """Unknown for a bullet with a concept no rule read. Pending reasons first."""
    found = [_unread(h, facts, policy) for h in unread]
    primary = next((f for f in found if f.pending), None) or next(
        (f for f in found if not f.reason.startswith("doctor review")), found[0])
    reason = primary.reason
    others = [CONCEPT_LABEL[h.name] for h, f in zip(unread, found) if f is not primary]
    if others:
        reason += f"; also mentions {', '.join(dict.fromkeys(others))}"
    if checked is not None:
        reason += f" (checked: {checked.reason})"
    field = primary.field
    if field.startswith(NO_RULE_FIELD) and len(unread) > 1:
        field = f"{NO_RULE_FIELD}:{','.join(h.name for h in unread)}"
    return Finding(None, field, reason, primary.pending)


# ---------------------------------------------------------------------------
# Combining every rule that read part of a bullet
# ---------------------------------------------------------------------------

_OR_WORD = re.compile(r"\bor\b|\beither\b", _I)
# Separators that may join the items of an exclusion list (see `evaluate`).
_LIST_SEP = re.compile(r"[,;/]|\band\b", _I)

# Words a bullet can hold that change nothing about what it asks: grammar,
# and the framing around the parts the rules read ("Histologically confirmed
# ... CNS WHO grade"). Any other word left over after every rule, tumor type
# and concept span is removed means the bullet was not fully read, so it
# cannot pass (see `evaluate`). Kept short on purpose: when in doubt a word
# is content, and the bullet goes to the doctor.
FILLER_WORDS = frozenset("""
    a an the and or of with to in for by at on as be is are must have has had who which that this
    patient patients participant participants subject subjects
    histologically histopathologically pathologically confirmed documented diagnosis diagnosed
    cns who tumor tumour tumors tumours years year age aged old
    mutant mutated mutation wildtype wild type promoter status
    enroll enrolled enrollment
""".split())
_WORD = re.compile(r"[a-z0-9]+", _I)
# A not_met decided only by a tumor type or concept rule (no pattern rule read
# the failing phrase) needs the bullet nearly fully read: at most this many
# content words outside the read spans.
NOT_MET_MAX_UNREAD_WORDS = 6
# A marker's form after the marker token: "IDH-mutant", "MGMT promoter unmethylated".
_FORM_TAIL = re.compile(
    r"[\s-]*(?:\d\b)?[\s-]*(?:promoter\s+)?(?:gene\s+)?"
    r"(?:mutant|mutated|mutation|wild[\s-]*type|co[\s-]*deleted|co[\s-]*deletion|intact|methylated|unmethylated)\b", _I)


def _joined(fs: list[Finding]) -> Finding:
    """All parts agree: cite every field used (rule 4)."""
    if len(fs) == 1:
        return fs[0]
    fields = list(dict.fromkeys(f.field for f in fs))
    reason = "; ".join(dict.fromkeys(f.reason for f in fs))
    return Finding(fs[0].holds, fs[0].field, f"{reason} (fields: {', '.join(fields)})")


def _first_unknown(fs: list[Finding]) -> Finding:
    return next((f for f in fs if f.pending), fs[0])


def _combine(parts: list[Finding], either_or: bool, unread: bool) -> Finding:
    """Whether the bullet's condition holds, from every rule that read part of it.

    The parts are read as AND: a failing part decides, then unknown parts
    (pending first), else every part holds. The exception is an "or" left
    outside the spans the rules read: the bullet may be "A or B", where a
    failing part decides nothing. If anything else in the bullet holds or is
    unknown, the answer is then unknown. This is the same for inclusions and
    exclusions: AND/OR confusion can turn a not_met into a met either way.
    For exclusions, `evaluate` also treats list separators as a possible
    either/or, since a list there excludes on any one item.
    """
    yes = [p for p in parts if p.holds is True]
    no = [p for p in parts if p.holds is False]
    unk = [p for p in parts if p.holds is None]
    if no:
        if either_or and (yes or unk or unread):
            if any(p.pending for p in unk):
                return _first_unknown(unk)
            return _review(no[0].field, f"parts of this criterion disagree and it may be an either/or "
                           f"({no[0].reason})")
        return no[0] if len(no) == 1 or yes or unk else _joined(no)
    return _first_unknown(unk) if unk else _joined(yes)


# Wording that makes a bullet's failing part not a reason to fail the patient
# (issue 48 live dry run). A rule can read "prior bevacizumab" correctly and
# still miss that the bullet only sets a condition on those who had it, allows
# it, or is one arm of several. A not_met in such a bullet becomes unknown for
# the doctor: a misread not_met hides the trial, which is a guess (rule 3).
# Word lists could move to policy.yaml later.


@dataclass(frozen=True)
class _Doubt:
    """One kind of wording that may make a failing part not a requirement.

    `treatment_only`: the wording only casts doubt on a failing treatment or
    recurrence part (condition, permission, timing, overlap); a failing tumor
    type, marker or grade elsewhere in the bullet still decides. Arm, heading
    and negation wording cast doubt on any failing part.
    `ignore`: spans removed before `pattern` is searched, for wording that
    looks like the cue but states the requirement itself ("is not permitted",
    "excluded if").
    """

    why: str
    pattern: re.Pattern[str]
    treatment_only: bool
    ignore: re.Pattern[str] | None = None

    def found_in(self, text: str) -> bool:
        if self.ignore is not None:
            text = self.ignore.sub(" ", text)
        return self.pattern.search(text) is not None


_TREATMENT_FIELDS = frozenset({"prior_treatments", "recurrent", "recurrence_number"})
_NEW_STATUS = r"(?:primary|newly[- ]diagnosed|new)"
_REC_STATUS = r"(?:recurrent|relapsed|refractory|progressive|metastasi[sz]ed)"
_TIME_SPAN = r"\d+\s*(?:days?|weeks?|months?|years?)"
_NOT_MET_DOUBT: list[_Doubt] = [
    _Doubt("it sets a condition", re.compile(
        r"\bif\b|\bunless\b|\bfor (?:patients|participants|subjects|those) (?:who|with|that)\b"
        r"|\bin (?:cohorts?|patients|participants|subjects) (?:who|with|that)\b", _I),
        treatment_only=True,
        # "Patients are excluded if they received X" states the exclusion; a
        # trailing "(if ...)" qualifies a requirement the bullet already made.
        ignore=re.compile(r"\b(?:excluded|exclude|ineligible|not\s+(?:be\s+)?eligible|eligible)\b[^,;.]{0,40}?\bif\b"
                          r"|\(\s*if\b[^)]*\)", _I)),
    _Doubt("it allows or limits something rather than requiring it", re.compile(
        r"\bpermitted\b|\ballowed\b|\bacceptable\b|\bmay (?:have )?(?:received|be|include|undergo)\b"
        r"|\bno limit\b|\bregardless\b", _I),
        treatment_only=True,
        # "is not permitted", "no prior X allowed" forbid; they do not allow.
        ignore=re.compile(r"\b(?:not|never|no longer)\s+(?:be\s+)?(?:permitted|allowed|acceptable)\b"
                          r"|\bno\b[^,;.]{0,60}?\b(?:permitted|allowed)\b", _I)),
    _Doubt("it sets timing or recovery after a treatment", re.compile(
        rf"\binterval\b|\b{_TIME_SPAN}\s+since\b|\bsince (?:the )?(?:last|completion|end|prior)\b"
        r"|\bmust have elapsed\b|\brecovered\b|\bwound heal|\btoxicit(?:y|ies)\b"
        rf"|[≤≥<>]\s*{_TIME_SPAN}\b|\b{_TIME_SPAN}\s+(?:before|prior|after|from)\b",
        _I), treatment_only=True),
    _Doubt("it depends on details the record lacks", re.compile(r"\boverlap|\bwould\b|\btarget volume\b", _I),
           treatment_only=True),
    _Doubt("it names one arm or cohort of several", re.compile(
        r"\b(?:arm|stratum|strata|cohort|group|part)\s+(?:[a-z]|\d{1,2}|[ivx]{1,4})\b(?=\s*[:(\-–]|\s*$)"
        # A slash joining a new-tumor status to a recurrent one ("primary/relapsed")
        # lists alternatives; "recurrent/progressive" are synonyms and still ask.
        rf"|\b{_NEW_STATUS}\s*/(?:\s*\w+\s*/)*\s*{_REC_STATUS}\b|\b{_REC_STATUS}\s*/(?:\s*\w+\s*/)*\s*{_NEW_STATUS}\b",
        _I), treatment_only=False),
    # A lead-in ending in ":" ("Female patients:") introduces sub-items; one
    # that says "must" ("must have recurrent glioblastoma and:") still asks.
    _Doubt("it is a heading or fragment, not a requirement", re.compile(
        r"^(?!.*\b(?:must|required?|shall)\b).*:\s*$|^\s*(?:i\.e\.|e\.g\.)", _I), treatment_only=False),
]
# Under "Inclusion Criteria", a sentence that says who is NOT eligible reads
# backwards for the rules ("Patients with IDH2 mutations are not eligible").
_INCLUSION_NEGATION = re.compile(
    r"\bnot eligible\b|\bineligible\b|\bare excluded\b|\bwill be excluded\b|\bnot allowed\b|^\s*no\b", _I)


def _not_met_doubt(text: str, kind: CriterionType, failing: Iterable[Finding]) -> str | None:
    """Why a not_met in this bullet cannot be trusted, or None.

    `failing` are the parts that made it not_met.
    """
    fields = {f.field for f in failing}
    treatment_only = bool(fields) and fields <= _TREATMENT_FIELDS
    for doubt in _NOT_MET_DOUBT:
        if doubt.treatment_only and not treatment_only:
            continue
        if doubt.found_in(text):
            return doubt.why
    if kind == "inclusion" and _INCLUSION_NEGATION.search(text):
        return "it says who is not eligible, inside the inclusion criteria"
    return None


# In the reason of every row the not-met guard turned unknown (live_eval counts them).
NOT_MET_DOUBT_MARKER = "but this may not rule the patient out"


def _doubted(out: Finding, why: str) -> Finding:
    return _review(out.field, f"{out.reason}, {NOT_MET_DOUBT_MARKER}: {why}")


def _passes(f: Finding, kind: CriterionType) -> bool:
    """Would this finding make the row `met`?"""
    return f.holds is (True if kind == "inclusion" else False)


def _leftover(text: str, read: list[tuple[int, int]]) -> tuple[str, list[str]]:
    """(text outside the read spans, its content words)."""
    rest = "".join(" " if any(a <= i < b for a, b in read) else ch for i, ch in enumerate(text))
    return rest, [w for w in _WORD.findall(rest.lower()) if w not in FILLER_WORDS]


def evaluate(criterion: Criterion, facts: Facts, policy: Policy) -> Finding:
    """Whether the criterion's condition holds for the patient.

    1. Every rule whose pattern matches runs (a match inside an earlier
       rule's match is skipped), plus the pre-surgery phrase from policy.yaml.
    2. Tumor types the bullet names are checked against the diagnosis, and
       every concept from `agent.concepts` that no rule covered and whose
       value is readable is checked against Facts.
    3. The parts are combined by `_combine`.
    4. Coverage guard: a concept the bullet mentions that nothing checked
       turns a would-be `met` into `unknown` naming it.
    5. Full-read guard: a would-be `met` also needs every content word of
       the bullet inside a span some rule read; otherwise `unknown`.

    6. Not-met guard: a `not_met` hides the trial, so a bullet whose wording
       may make the failing part not a requirement (a condition, a permission,
       timing after a treatment, one arm of several, a heading, or a "not
       eligible" sentence under inclusion; see `_NOT_MET_DOUBT`) is `unknown`.

    Guards 4 and 5 stop wrong passes; guard 6 stops wrong failures. Both
    leave the call to the doctor (rule 3).
    """
    text = criterion.text
    hits = {h.name: h for h in detect(text)}
    parts: list[Finding] = []
    covered: set[str] = set()
    read: list[tuple[int, int]] = []

    pre_surgery = _pre_surgery_phrase(criterion, policy)
    if pre_surgery is not None:
        parts.append(_before_surgery(facts, pre_surgery, policy))
        read.append((pre_surgery.start, pre_surgery.end))
    for rule in RULES:
        m = rule.pattern.search(text)
        if m is None or any(a <= m.start() and m.end() <= b for a, b in read):
            continue
        parts.append(rule.fn(facts, m, policy))
        covered |= rule.covers
        read.append(m.span())
    specific = list(parts)  # parts from a pattern rule or the policy phrase
    if "entity" not in covered:
        found = _entities(text, facts, policy)
        if found is not None:
            parts.append(found)
            covered.add("entity")
    read += [m.span() for m in _ENTITY_LIST.finditer(text)]
    for name, hit in hits.items():
        if name not in covered:
            call = _concept_rule(hit, hits, text, criterion.type, facts, policy)
            if call is None:
                continue
            covered.add(name)
            if isinstance(call, Finding):
                parts.append(call)
            if name in _NO_REQUIREMENT:
                read += [m.span() for m in _NO_REQUIREMENT[name].finditer(text)]
            if name == "idh" and isinstance(call, Finding):
                read += [m.span() for m in _R132H.finditer(text)]  # read by _idh_r132h
        if (at := _span_at(text, hit.span)) is not None:
            tail = _FORM_TAIL.match(text, at[1]) if name in MARKER_LABELS else None
            read.append((at[0], tail.end() if tail else at[1]))

    unread = [h for n, h in hits.items() if n not in covered]
    if not parts:
        return _guard(unread, facts, policy, None) if unread else Finding(None, NO_RULE_FIELD, NO_RULE_REASON)
    rest, words = _leftover(text, read)
    either_or = bool(_OR_WORD.search(rest))
    if criterion.type == "exclusion":
        # An exclusion list ("prior bevacizumab, checkpoint inhibitors") excludes
        # on any one item. A pattern rule that finds its own clause applies
        # decides: the exclusion applies, whatever the other items say.
        applies = next((p for p in specific if p.holds is True), None)
        if applies is not None:
            why = _not_met_doubt(text, criterion.type, [applies])
            return _doubted(applies, why) if why else applies
        # A comma, semicolon, slash or "and" between the read parts may join
        # list items, not qualifiers, so parts that disagree cannot pass.
        either_or = either_or or bool(_LIST_SEP.search(rest))
    out = _combine(parts, either_or, bool(unread or words))
    if out.holds is None:
        return out
    if not _passes(out, criterion.type):
        # A not_met hides the trial, so wording that may make the failing part
        # not a requirement (a condition, a permission, one arm of several, a
        # heading) leaves it to the doctor.
        if why := _not_met_doubt(text, criterion.type, [p for p in parts if p.holds is out.holds]):
            return _doubted(out, why)
        # A pattern rule that read the deciding phrase may decide
        # it; a tumor type or concept found anywhere in a long bullet may be
        # context ("history of malignancy other than glioma"), not the ask.
        # An exclusion that applies only through such parts needs a full read:
        # the tumor type in "proton therapy for glioma" is a qualifier.
        limit = NOT_MET_MAX_UNREAD_WORDS if criterion.type == "inclusion" else 0
        if any(p.holds is out.holds for p in specific) or len(words) <= limit:
            return out
        return _review(out.field, f"{out.reason}, but the criterion has wording no rule read: "
                       f"{' '.join(dict.fromkeys(words))[:80]!r}")
    if unread:
        return _guard(unread, facts, policy, out)
    if words:
        shown = " ".join(dict.fromkeys(words))
        return _review(NO_RULE_FIELD, f"wording no rule read: {shown[:80]!r} (checked: {out.reason})")
    return out


def to_row(trial_id: str, criterion: Criterion, f: Finding) -> EligibilityResult:
    """Map 'condition holds' to 'patient passes'. Exclusions flip."""
    if f.holds is None:
        status = "unknown"
    elif criterion.type == "inclusion":
        status = "met" if f.holds else "not_met"
    else:
        status = "not_met" if f.holds else "met"
    return EligibilityResult(
        trial_id=trial_id,
        criterion_type=criterion.type,
        criterion_text=criterion.text,
        status=status,
        evidence_field=f.field,
        reason=f.reason,
    )


def _years(x: float) -> str:
    return f"{x:g} years"


def structured_age_row(trial: TrialRecord, facts: Facts, policy: Policy) -> EligibilityResult | None:
    """The registry's structured age limits (ClinicalTrials.gov minimumAge /
    maximumAge, NCI min/max_age_in_years) as one inclusion row.

    Used only when no criteria bullet already decided age. The criterion text
    names the structured limits it read (rule 4). No age recorded: unknown.
    """
    lo, hi = trial.min_age_years, trial.max_age_years
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None:
        span = f"{_years(lo)} to {_years(hi)}"
    elif lo is not None:
        span = f"{_years(lo)} or older"
    else:
        span = f"up to {_years(hi)}"  # type: ignore[arg-type]
    text = f"Age {span} (trial registry age limits)"
    if facts.age is None:
        f = _missing("age", "age", policy)
    elif (lo is not None and facts.age < lo) or (hi is not None and facts.age > hi):
        f = _no("age", f"age {facts.age} is outside {span}")
    else:
        f = _yes("age", f"age {facts.age} is within {span}")
    return to_row(trial.trial_id, Criterion("inclusion", text), f)


def structured_sex_row(trial: TrialRecord, facts: Facts, policy: Policy) -> EligibilityResult | None:
    """The registry's structured sex eligibility (ClinicalTrials.gov
    eligibilityModule.sex, NCI eligibility.structured.sex) as one inclusion row.

    None when the trial accepts all sexes or the registry gives no value. The
    criterion text names the registry limit it read (rule 4). No sex recorded:
    unknown, never guessed (rule 3).
    """
    limit = trial.sex_eligibility
    if limit is None or limit == "ALL":
        return None
    want = limit.lower()  # "female" | "male", same values as DoctorResults.sex
    text = f"Sex: {want} only (trial registry sex eligibility)"
    if facts.sex is None:
        f = _missing("sex", "sex", policy)
    elif facts.sex == want:
        f = _yes("sex", f"sex {facts.sex} matches {want} only")
    else:
        f = _no("sex", f"sex {facts.sex}; trial enrolls {want} patients only")
    return to_row(trial.trial_id, Criterion("inclusion", text), f)


def check_trial(
    trial: TrialRecord,
    facts: Facts,
    *,
    policy: Policy | None = None,
    case_id: str | None = None,
) -> list[EligibilityResult]:
    """One row per criterion of `trial`. Audited (rule 7)."""
    policy = policy or get_policy()
    rows = [to_row(trial.trial_id, c, evaluate(c, facts, policy)) for c in split_criteria(trial.criteria_text)]
    if not rows:
        rows = [EligibilityResult(
            trial_id=trial.trial_id,
            criterion_text=trial.criteria_text.strip() or "(no criteria text)",
            status="unknown",
            evidence_field=NO_RULE_FIELD,
            reason="criteria text could not be split into criteria; doctor must review",
        )]
    if not any(r.evidence_field == "age" for r in rows):
        age_row = structured_age_row(trial, facts, policy)
        if age_row is not None:
            rows.insert(0, age_row)
    if not any(r.evidence_field == "sex" for r in rows):
        sex_row = structured_sex_row(trial, facts, policy)
        if sex_row is not None:
            rows.insert(0, sex_row)
    s = summarize(rows)
    audit(
        "agent",
        "eligibility_checked",
        {"trial_id": trial.trial_id, "stage": facts.stage, **s.model_dump(),
         "pending": sum(is_pending(r) for r in rows)},
        case_id=case_id,
    )
    return rows


def is_pending(row: EligibilityResult) -> bool:
    """Unknown because a result is ordered and not back yet."""
    return row.status == "unknown" and row.reason.startswith(PENDING_PREFIX)


def summarize(rows: Iterable[EligibilityResult]) -> EligibilitySummary:
    counts = {"met": 0, "not_met": 0, "unknown": 0}
    for r in rows:
        counts[r.status] += 1
    return EligibilitySummary(**counts)


def passes(rows: list[EligibilityResult]) -> bool:
    """Shortlist rule (interfaces.md): no not_met, none waiting on a pending result."""
    return all(r.status != "not_met" and not is_pending(r) for r in rows)
