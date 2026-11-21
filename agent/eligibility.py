"""Eligibility checker (F8): one `met | not_met | unknown` row per criterion.

Deterministic code makes every call. The trial's criteria text is split into
inclusion and exclusion bullets, and each bullet is matched against a small
rule library. A rule reads named patient fields and answers whether the
criterion's condition holds: yes, no or unknown.

- Rule 3: a missing field gives `unknown` with the reason; it is never
  guessed. A `pending` result also gives `unknown`, with a reason starting
  with `PENDING_PREFIX`, so the shortlist can tell "waiting on the lab" apart.
- Rule 4: every row names the patient field it used and the criterion text.
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
from agent.policy import Policy, get_policy
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
# whether the criterion's condition holds. First matching rule wins, so
# specific patterns come before general ones.
# ---------------------------------------------------------------------------

RuleFn = Callable[[Facts, re.Match[str], Policy], Finding]

GLIOMA_FAMILY = ("glioblastoma", "astrocytoma", "oligodendroglioma", "glioma")
ENTITY_BUCKET = {e: "glioma" for e in GLIOMA_FAMILY} | {"meningioma": "meningioma"}
_ENTITY_RE = re.compile(r"\b(glioblastoma|astrocytoma|oligodendroglioma|meningioma|glioma)\b")
_GRADE_RE = re.compile(r"grade\s*([1-4])(?:\s*(?:or|to|-|–|and)\s*([1-4]))?")


def _entity(facts: Facts, entity: str, policy: Policy) -> Finding:
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
    return _no("diagnosis", f"diagnosis is {facts.diagnosis}, not {entity}")


def _histology(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    """'Histologically confirmed <entity>, <IDH>, <1p/19q>, CNS WHO grade X or Y'."""
    text = m.string.lower()
    ent = _ENTITY_RE.search(text)
    parts: list[Finding] = []
    if ent:
        parts.append(_entity(facts, ent.group(1), policy))
    for want in ("mutant", "wildtype"):
        if re.search(rf"idh[- ]?{want[:4]}", text):
            parts.append(_marker_is(facts, "idh", want, policy))
    if re.search(r"1p/19q[- ]?co[- ]?deleted|1p/19q[- ]?codeletion", text):
        parts.append(_marker_is(facts, "codeletion_1p19q", "codeleted", policy))
    g = _GRADE_RE.search(text)
    if g:
        lo = int(g.group(1))
        hi = int(g.group(2) or lo)
        parts.append(_grade_in(facts, lo, hi, policy))
    if not parts:
        return _review(NO_RULE_FIELD, "histology not recognized")
    out = _all_of(*parts)
    if out.holds:
        return _yes("diagnosis", f"diagnosis: {facts.diagnosis}" + (
            f", grade {facts.grade}" if g else ""))
    return out


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
        return why
    span = f"{lo}" if lo == hi else f"{lo} to {hi}"
    if lo <= g <= hi:
        return _yes("grade", f"WHO grade {g} is within {span}")
    return _no("grade", f"WHO grade {g} is outside {span}")


def _marker_rule(name: str, want: str) -> RuleFn:
    return lambda facts, m, policy: _marker_is(facts, name, want, policy)


def _age(facts: Facts, m: re.Match[str], policy: Policy) -> Finding:
    if facts.age is None:
        return _missing("age", "age", policy)
    lo = int(m.group("lo"))
    hi = int(m.group("hi")) if m.groupdict().get("hi") else None
    if facts.age < lo or (hi is not None and facts.age > hi):
        span = f"{lo}+" if hi is None else f"{lo} to {hi}"
        return _no("age", f"age {facts.age} is outside {span}")
    return _yes("age", f"age {facts.age}")


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


def _before_surgery(facts: Facts, phrase: str, policy: Policy) -> Finding:
    """Phrases come from pre_surgery_detection.inclusion_phrases in policy.yaml."""
    if facts.stage == "post_pathology":
        return _no("stage", f"needs enrollment {phrase!r}; surgery or biopsy is already done")
    surgery = _has_treatment(facts, {"surgery"}, "surgery", policy)
    if surgery.holds:
        return _no("prior_treatments", "prior surgery recorded")
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


def _treatment_rule(kinds: set[str], label: str) -> RuleFn:
    return lambda facts, m, policy: _has_treatment(facts, kinds, label, policy)


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
    limit = int(raw) if raw.isdigit() else _WORDS[raw]
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


_I = re.IGNORECASE
RULES: list[tuple[re.Pattern[str], RuleFn]] = [
    (re.compile(r"imaging suspicious for newly diagnosed glioblastoma", _I), _imaging_suspicious_gbm),
    (re.compile(r"histologically confirmed", _I), _histology),
    (re.compile(r"\bage\s+(?P<lo>\d+)\s*(?:to|-|–)\s*(?P<hi>\d+)\s*years", _I), _age),
    (re.compile(r"\bage\s*(?:≥|>=)?\s*(?P<lo>\d+)\s*(?:years?)?\s*(?:or|and)\s*(?:older|over)", _I), _age),
    (re.compile(r"newly diagnosed;?\s*no prior radiation or chemotherapy", _I), _newly_dx_no_rt_chemo),
    (re.compile(r"newly diagnosed,?\s*after surgery or biopsy", _I), _newly_dx_after_surgery),
    (re.compile(r"^\s*newly diagnosed\s*$", _I), _newly_dx),
    (re.compile(r"prior radiation and temozolomide", _I), _rt_and_tmz),
    (re.compile(r"pituitary radiation within the past (?P<n>\d+) years?", _I), _pit_radiation_within),
    (re.compile(r"prior (?:brain )?surgery|prior resection|surgery or biopsy for this tumor", _I),
     _treatment_rule({"surgery"}, "surgery or biopsy")),
    (re.compile(r"prior radiation", _I), _treatment_rule({"radiation"}, "radiation")),
    (re.compile(r"prior temozolomide", _I), _treatment_rule({"temozolomide"}, "temozolomide")),
    (re.compile(r"prior bevacizumab", _I), _treatment_rule({"bevacizumab"}, "bevacizumab")),
    (re.compile(r"idh inhibitor", _I), _treatment_rule({"idh_inhibitor"}, "IDH inhibitor")),
    (re.compile(r"checkpoint inhibitor", _I), _treatment_rule({"checkpoint_inhibitor"}, "checkpoint inhibitor")),
    (re.compile(r"gross total or subtotal resection", _I), _resection_extent),
    (re.compile(r"first or second recurrence", _I), _first_or_second),
    (re.compile(r"more than (?P<n>\d+|one|two|three|four) prior recurrences?", _I), _more_than_recurrences),
    (re.compile(r"recurrent glioma", _I), _recurrent_glioma),
    (re.compile(r"tumou?r mutational burden|\btmb\b", _I), _tmb),
    (re.compile(r"participation is fully remote|remote participation", _I), _device),
    (re.compile(r"unable to use a phone or tablet", _I), _unable_device),
    (re.compile(r"neurofibromatosis type 2|\bnf2\b", _I),
     lambda f, m, p: _comorbidity(f, ("neurofibromatosis type 2", "nf2"), "neurofibromatosis type 2", p)),
    (re.compile(r"autoimmune disease", _I),
     lambda f, m, p: _comorbidity(f, AUTOIMMUNE_TERMS, "active autoimmune disease", p, category=True)),
    (re.compile(r"(?P<h>acth|prolactin|growth hormone)[- ]secreting pituitary", _I), _secreting),
    (re.compile(r"persistent disease after surgery,? or not a surgical candidate", _I),
     _persistent_or_not_candidate),
    (re.compile(r"mgmt promoter unmethylated", _I), _marker_rule("mgmt", "unmethylated")),
    (re.compile(r"mgmt promoter methylated", _I), _marker_rule("mgmt", "methylated")),
    (re.compile(r"1p/19q[- ]?codelet(?:ion|ed)", _I), _marker_rule("codeletion_1p19q", "codeleted")),
    (re.compile(r"idh[- ]?mutant", _I), _marker_rule("idh", "mutant")),
    (re.compile(r"idh[- ]?wild[- ]?type", _I), _marker_rule("idh", "wildtype")),
]


# ---------------------------------------------------------------------------
# Criteria splitting
# ---------------------------------------------------------------------------

_BULLET = re.compile(r"^\s*(?:[*\-•·]|\d+[.)])\s+")
_HEADER = re.compile(r"^\s*(inclusion|exclusion)\s+criteria\s*:?\s*$", _I)


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


def _pre_surgery_phrase(criterion: Criterion, policy: Policy) -> str | None:
    """An inclusion phrase from policy.yaml that means 'enroll before surgery'."""
    if criterion.type != "inclusion":
        return None
    low = criterion.text.lower()
    return next((p for p in policy.pre_surgery_detection.inclusion_phrases if p.lower() in low), None)


def evaluate(criterion: Criterion, facts: Facts, policy: Policy) -> Finding:
    """Whether the criterion's condition holds for the patient."""
    phrase = _pre_surgery_phrase(criterion, policy)
    if phrase is not None:
        return _before_surgery(facts, phrase, policy)
    for pat, fn in RULES:
        m = pat.search(criterion.text)
        if m:
            return fn(facts, m, policy)
    return Finding(None, NO_RULE_FIELD, NO_RULE_REASON)


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
