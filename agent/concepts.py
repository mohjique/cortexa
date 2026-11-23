"""Concept detectors: which eligibility concepts a criterion bullet mentions.

Regex only, deterministic, no patient data. `detect(text)` returns one
`ConceptHit` per concept the text mentions, with the matched span (evidence,
safety rule 4) and, where the text states it plainly, a normalized value:

    idh               "mutant" | "wildtype" | None (both forms, "regardless of IDH status", or no form)
    codeletion_1p19q  "codeleted" | "intact" | None
    mgmt              "methylated" | "unmethylated" | None
    grade             "lo-hi" such as "2-3" or "4-4" | None ("high-grade" alone, toxicity grades skipped)
    age_range         "lo-hi", "lo-" or "-hi" in years | None (conflicting bounds)
    sex               "female" | "male" | None ("male or female", "any gender")
    others            None

`value=None` means "this bullet reads the concept but no single requirement
could be read from it". It is never a default for a patient field (rule 3):
callers must treat it as "doctor review", not as a pass.

Hits are ordered by where their first span starts, then by `CONCEPTS` order.

Choices worth knowing:

- Recurrent: "recurrent", "recurrence", "progressive", "progression",
  "progressed", "relapsed", "refractory". "Disease progression" is often a
  stopping rule or a time point, not a requirement ("treat until disease
  progression", "time to progression", "risk of recurrence"). A match is
  skipped when one of `RECURRENT_SKIP_BEFORE` comes just before it, or one
  of `RECURRENT_SKIP_AFTER` (seizures, infections, "progressive multifocal
  leukoencephalopathy") just after. Everything else is reported, including
  "no evidence of progression": the concept is mentioned there, and the
  caller decides what the bullet asks.
- Grade: CTCAE toxicity grades ("resolved to Grade 1", "Grade >= 2 changes
  in AST", "hypertension Grade 2") are skipped when a word from
  `TOXICITY_WORDS` is nearby. Several different grade expressions in one
  bullet give value None rather than a merged range.
- Age: a number counts only with an age anchor ("age", "aged", "years of
  age", "years old", "between A and B years", "Adults (>= 18 years)"), so
  "disease free for >= 3 years" is not an age. Strict bounds are tightened
  by one year ("> 18 years" -> "19-"), which errs toward not_met rather than
  a false met. A conditional bullet ("Patients 2-17 years of age must have
  a blood pressure ...") still reports its range; regex cannot tell a
  condition from a requirement.
- Pediatric: "child-bearing"/"childbearing", "children's" (hospital names)
  and "pediatric-type" (a WHO tumor family that occurs in adults) do not
  count.
- Sex: words inside a reproductive context (`SEX_SKIP_CONTEXT`: pregnancy,
  contraception, childbearing, "sexually active", "if female" in creatinine
  formulas) are ignored, so "women of childbearing potential" and "men who
  are sexually active" are not a sex restriction.
- IDH mentions inside "IDH inhibitor"/"targeting IDH" still count as an IDH
  mention with value None.

The word lists are module constants so they are easy to review. They could
move to `policies/policy.yaml` later, like the pre-surgery phrases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, get_args

ConceptName = Literal["recurrent", "newly_diagnosed", "idh", "codeletion_1p19q", "mgmt",
                      "grade", "age_range", "pediatric", "sex", "h3k27m", "braf"]

CONCEPTS: tuple[ConceptName, ...] = get_args(ConceptName)


@dataclass(frozen=True)
class ConceptHit:
    name: ConceptName
    span: str            # matched text, for evidence (rule 4)
    value: str | None    # normalized requirement when readable, else None (see module docstring)


_I = re.IGNORECASE
_SENTENCE_END = re.compile(r"[.;]\s|[.;]$")


def _clause_after(text: str, end: int, n: int = 60) -> str:
    """Up to n chars after `end`, cut at the next sentence end."""
    s = text[end:end + n]
    m = _SENTENCE_END.search(s)
    return s[:m.start()] if m else s


def _sentence(text: str, start: int, end: int) -> str:
    """The sentence (between '.'/';' breaks) holding text[start:end]."""
    a = max((m.end() for m in _SENTENCE_END.finditer(text, 0, start)), default=0)
    b = _SENTENCE_END.search(text, end)
    return text[a:b.start() if b else len(text)]


def _reproductive(text: str, start: int, end: int) -> bool:
    """Pregnancy, contraception or menopause talk in the match's sentence."""
    return bool(_SEX_SKIP.search(_sentence(text, start, end)))


def _clause_before(text: str, start: int, n: int = 40) -> str:
    """Up to n chars before `start`, cut at the previous sentence end."""
    s = text[max(0, start - n):start]
    cut = [m.end() for m in _SENTENCE_END.finditer(s)]
    return s[cut[-1]:] if cut else s


def _one_value(forms: set[str], neutral: bool) -> str | None:
    return next(iter(forms)) if len(forms) == 1 and not neutral else None


# ---------------------------------------------------------------------------
# Word lists
# ---------------------------------------------------------------------------

RECURRENT_WORDS = (
    r"recurren(?:t|ce|ces)", r"progressive", r"progression", r"progressed", r"progressing",
    r"relapsed?", r"relapsing", r"refractory",
)
RECURRENT_SKIP_BEFORE = (
    r"until", r"till", r"time\s+to", r"risk\s+of", r"rate\s+of", r"free\s+of", r"prevent(?:ion\s+of)?",
    r"in\s+case\s+of", r"at\s+the\s+time\s+of", r"date\s+of",
)
RECURRENT_SKIP_AFTER = (
    r"multifocal\s+leukoencephalopathy", r"supranuclear", r"seizures?", r"infections?", r"fevers?",
    r"thrombos[ie]s", r"pneumonias?", r"urinary", r"-?remitting", r"-free",
)
NEWLY_DIAGNOSED_WORDS = (r"newly[\s-]+diagnosed", r"new(?:ly)?\s+diagnosed", r"new\s+diagnosis", r"first\s+diagnosis")
PEDIATRIC_WORDS = (r"pa?ediatric", r"children", r"child", r"childhood", r"adolescents?")
PEDIATRIC_NOT = (r"child[\s-]?bearing", r"children'?s\b", r"pa?ediatric[\s-]+type",
                 r"(?:father|bear|have|having|conceive)\s+(?:a\s+)?child(?:ren)?")
TOXICITY_WORDS = (
    r"ctcae", r"toxicit", r"adverse", r"\bae\b", r"\baes\b", r"resolved", r"recovered", r"recovery",
    r"changes\s+in", r"hypertension", r"neuropathy", r"infusion", r"common\s+terminology",
    r"hemorrhage", r"bleeding", r"lymphopenia", r"elevation", r"intolerance",
)
SEX_SKIP_CONTEXT = (
    r"pregnan", r"lactat", r"breast[\s-]?feed", r"nursing", r"child[\s-]?bearing", r"reproductive",
    r"contracept", r"fertil", r"father", r"menopaus", r"sexually", r"sperm", r"wocbp", r"pocbp",
    r"hysterectomy", r"oophorectomy", r"menstruat", r"partners?\b", r"birth\s+control", r"\bif\s+(?:fe)?male",
    r"impregnat", r"condom", r"\bhcg\b", r"vasectomy",
)

_RECURRENT = re.compile(r"\b(?:" + "|".join(RECURRENT_WORDS) + r")\b", _I)
_REC_SKIP_BEFORE = re.compile(r"(?:" + "|".join(RECURRENT_SKIP_BEFORE) + r")\s+(?:\w+\s+){0,2}$", _I)
_REC_SKIP_AFTER = re.compile(r"^\s*(?:" + "|".join(RECURRENT_SKIP_AFTER) + r")", _I)
_NEWLY = re.compile(r"\b(?:" + "|".join(NEWLY_DIAGNOSED_WORDS) + r")\b", _I)
_PEDIATRIC = re.compile(r"\b(?:" + "|".join(PEDIATRIC_WORDS) + r")\b", _I)
_PEDIATRIC_NOT = re.compile(r"\b(?:" + "|".join(PEDIATRIC_NOT) + r")", _I)
_TOXICITY = re.compile("|".join(TOXICITY_WORDS), _I)
_SEX_SKIP = re.compile("|".join(SEX_SKIP_CONTEXT), _I)


# ---------------------------------------------------------------------------
# Simple concepts
# ---------------------------------------------------------------------------


def _recurrent(text: str) -> tuple[int, ConceptHit] | None:
    for m in _RECURRENT.finditer(text):
        if _REC_SKIP_BEFORE.search(_clause_before(text, m.start(), 30)):
            continue
        if _REC_SKIP_AFTER.search(text[m.end():m.end() + 40]):
            continue
        return m.start(), ConceptHit("recurrent", m.group(0), None)
    return None


def _first(name: ConceptName, pat: re.Pattern[str], text: str) -> tuple[int, ConceptHit] | None:
    m = pat.search(text)
    return (m.start(), ConceptHit(name, m.group(0), None)) if m else None


def _pediatric(text: str) -> tuple[int, ConceptHit] | None:
    blocked = [(m.start(), m.end()) for m in _PEDIATRIC_NOT.finditer(text)]
    for m in _PEDIATRIC.finditer(text):
        if any(a <= m.start() < b for a, b in blocked):
            continue
        return m.start(), ConceptHit("pediatric", m.group(0), None)
    return None


_H3K27M = re.compile(r"\bH3(?:\.[13])?(?:F3A)?\s*[-]?\s*K27(?:M\b|[\s-]*(?:altered|alteration|mutant|mutated|mutation))?"
                     r"|\bK27M\b", _I)
_BRAF = re.compile(r"\bBRAF(?:\s*V600[EK]?)?|\bKIAA1549[\s:-]*BRAF", _I)


# ---------------------------------------------------------------------------
# Markers: IDH, 1p/19q, MGMT. A form counts only right next to the marker
# token ("IDH-mutant", "mutant IDH", "absence of IDH mutation"), so a form
# that belongs to another marker in the same sentence is not borrowed.
# ---------------------------------------------------------------------------

_IDH_TOKEN = re.compile(
    r"isocitrate\s+dehydrogenase(?:\s*[12])?(?:\s*\(\s*IDH[12]?\s*\))?"
    r"|\bIDH(?:\s*[-]?\s*[12])?(?:\s*(?:/|and/or|and|or)\s*(?:IDH\s*)?2)?(?![a-qs-z])",
    _I,
)
_IDH_WT_FORM = r"(?:wild[\s-]*type|wt\b|non[\s-]*mutant|non[\s-]*mutated|neg(?:ative)?\b)"
_IDH_MUT_FORM = r"(?:mut(?:ant|ated|ation|ations)?\b|mut\.?|positive\b|\+)"
_R132 = r"(?:R132[A-Z]?(?:\s*/\s*[A-Z])*\s*[-]?\s*)"
_IDH_AFTER_NEUTRAL = re.compile(r"^[\s\-:()]*(?:gene\s+)?(?:mutation(?:al)?\s+)?(?:status|testing|test|analysis)\b", _I)
_IDH_AFTER_WT = re.compile(r"^[\s\-:()]*(?:\(\s*IDH\s*\)\s*)?" + _R132 + r"?(?:gene\s+)?" + _IDH_WT_FORM, _I)
_IDH_AFTER_MUT = re.compile(r"^[\s\-:()]*(?:gene[\s-]+)?(?:" + _R132 + r"(?:" + _IDH_MUT_FORM + r")?|" + _IDH_MUT_FORM + r")", _I)
_IDH_BEFORE_WT = re.compile(r"(?:absence\s+of|\bno|without|negative(?:\s+for)?|lack\s+of|non[\s-]*)\s*(?:an?\s+)?$", _I)
_IDH_BEFORE_MUT = re.compile(r"\b(?:mutant|mutated|mutation\s+(?:in|of))\s*$", _I)
_IDH_BEFORE_NEUTRAL = re.compile(r"(?:regardless\s+of|irrespective\s+of|independent\s+of|any)\s*(?:the\s+)?$", _I)
_OR_WT = re.compile(r"^\s*(?:or|and|and/or|/|,)\s*(?:IDH[12]?[\s-]*)?" + _IDH_WT_FORM, _I)
_OR_MUT = re.compile(r"^\s*(?:or|and|and/or|/|,)\s*(?:IDH[12]?[\s-]*)?(?:mutant|mutated)\b", _I)


def _idh(text: str) -> tuple[int, ConceptHit] | None:
    first = None
    forms: set[str] = set()
    neutral = False
    for m in _IDH_TOKEN.finditer(text):
        first = first or m
        before = _clause_before(text, m.start(), 30)
        after = _clause_after(text, m.end(), 50)
        if _IDH_BEFORE_NEUTRAL.search(before) or _IDH_AFTER_NEUTRAL.search(after):
            neutral = True
            continue
        wt = _IDH_AFTER_WT.match(after)
        mut = None if wt else _IDH_AFTER_MUT.match(after)
        if wt:
            forms.add("wildtype")
            if _OR_MUT.match(after[wt.end():]):
                forms.add("mutant")
        elif _IDH_BEFORE_WT.search(before):
            forms.add("wildtype")
        elif mut:
            forms.add("mutant")
            if _OR_WT.match(after[mut.end():]):
                forms.add("wildtype")
        elif _IDH_BEFORE_MUT.search(before):
            forms.add("mutant")
    if first is None:
        return None
    return first.start(), ConceptHit("idh", first.group(0), _one_value(forms, neutral))


_CODEL_TOKEN = re.compile(r"\b1p\s*(?:/|-|and|\s)?\s*19q\b", _I)
_CODEL_INTACT = r"(?:non[\s-]*co[\s-]*delet(?:ed|ion)|intact|retained|not\s+co[\s-]*deleted|without\s+co[\s-]*deletion)"
_CODEL_CODEL = r"(?:co[\s-]*delet(?:ed|ion)|deleted|deletion|loss)"
_CODEL_AFTER_NEUTRAL = re.compile(r"^[\s\-:()]*(?:co[\s-]*deletion\s+)?(?:status|testing|test|analysis)\b", _I)
_CODEL_AFTER_INTACT = re.compile(r"^[\s\-:()]*(?:chromosom\w*\s+)?" + _CODEL_INTACT, _I)
_CODEL_AFTER_CODEL = re.compile(r"^[\s\-:()]*(?:chromosom\w*\s+)?" + _CODEL_CODEL, _I)
_CODEL_BEFORE_NEG = re.compile(
    r"(?:absence\s+of|\bno|without|lack\s+of|\bnot|non)[\s-]*(?:an?\s+|the\s+)?(?:chromosom\w*\s+)?$", _I)
_CODEL_BEFORE_NEUTRAL = re.compile(r"(?:regardless\s+of|irrespective\s+of|any)\s*(?:the\s+)?$", _I)
_CODEL_BEFORE_CODEL = re.compile(r"co[\s-]*deleted\s*$", _I)
_OR_INTACT = re.compile(r"^\s*(?:or|and|and/or|/|,)\s*(?:1p\s*/?\s*19q[\s-]*)?" + _CODEL_INTACT, _I)


def _codeletion(text: str) -> tuple[int, ConceptHit] | None:
    first = None
    forms: set[str] = set()
    neutral = False
    for m in _CODEL_TOKEN.finditer(text):
        first = first or m
        before = _clause_before(text, m.start(), 40)
        after = _clause_after(text, m.end(), 50)
        if _CODEL_BEFORE_NEUTRAL.search(before) or _CODEL_AFTER_NEUTRAL.search(after):
            neutral = True
            continue
        negated = bool(_CODEL_BEFORE_NEG.search(before))
        intact = _CODEL_AFTER_INTACT.match(after)
        codel = None if intact else _CODEL_AFTER_CODEL.match(after)
        if intact:
            forms.add("intact")
        elif codel:
            forms.add("intact" if negated else "codeleted")
            if _OR_INTACT.match(after[codel.end():]):
                forms.add("intact")
        elif _CODEL_BEFORE_CODEL.search(before):
            forms.add("codeleted")
    if first is None:
        return None
    return first.start(), ConceptHit("codeletion_1p19q", first.group(0), _one_value(forms, neutral))


_MGMT_TOKEN = re.compile(r"\bMGMT\b", _I)
_MGMT_UNMETH = r"(?:un[\s-]*methylated|non[\s-]*methylated|not\s+methylated|methylation[\s-]*negative|hypo[\s-]*methylated)"
_MGMT_METH = (r"(?:hyper[\s-]*methylated|methylated|methylation[\s-]*positive"
              r"|methylation(?!\s*(?:-|\s)?(?:status|testing|test|analysis|result|negative)))")
_MGMT_LEAD = r"^[\s\-:()]*(?:gene\s+)?(?:promo(?:t|te)[eo]?r\s*(?:region\s*)?)?(?:is\s+|status\s*(?:is|:)?\s*)?"
_MGMT_AFTER_NEUTRAL = re.compile(_MGMT_LEAD + r"(?:methylation\s*)?(?:status|testing|test|analysis)\b", _I)
_MGMT_AFTER_UNMETH = re.compile(_MGMT_LEAD + _MGMT_UNMETH, _I)
_MGMT_AFTER_METH = re.compile(_MGMT_LEAD + _MGMT_METH, _I)
_MGMT_BEFORE_UNMETH = re.compile(_MGMT_UNMETH + r"[\s-]*(?:of\s+(?:the\s+)?)?$", _I)
_MGMT_BEFORE_METH = re.compile(r"(?<![a-z])(?:hyper[\s-]*)?methylated[\s-]*(?:of\s+(?:the\s+)?)?$", _I)
_MGMT_BEFORE_NEUTRAL = re.compile(r"(?:regardless\s+of|irrespective\s+of|any)\s*(?:the\s+)?$", _I)
_OR_UNMETH = re.compile(r"^\s*(?:or|and|and/or|/|,)\s*" + _MGMT_UNMETH, _I)
_OR_METH = re.compile(r"^\s*(?:or|and|and/or|/|,)\s*(?:hyper[\s-]*)?methylated\b", _I)


def _mgmt(text: str) -> tuple[int, ConceptHit] | None:
    first = None
    forms: set[str] = set()
    neutral = False
    for m in _MGMT_TOKEN.finditer(text):
        first = first or m
        before = _clause_before(text, m.start(), 30)
        after = _clause_after(text, m.end(), 50)
        if _MGMT_BEFORE_NEUTRAL.search(before) or _MGMT_AFTER_NEUTRAL.match(after):
            neutral = True
            continue
        un = _MGMT_AFTER_UNMETH.match(after)
        me = None if un else _MGMT_AFTER_METH.match(after)
        if un:
            forms.add("unmethylated")
            if _OR_METH.match(after[un.end():]):
                forms.add("methylated")
        elif me:
            forms.add("methylated")
            if _OR_UNMETH.match(after[me.end():]):
                forms.add("unmethylated")
        elif _MGMT_BEFORE_UNMETH.search(before):
            forms.add("unmethylated")
        elif _MGMT_BEFORE_METH.search(before):
            forms.add("methylated")
    if first is None:
        return None
    return first.start(), ConceptHit("mgmt", first.group(0), _one_value(forms, neutral))


# ---------------------------------------------------------------------------
# Grade
# ---------------------------------------------------------------------------

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
_G = r"(?:[1-4]|iv|iii|ii|i)(?![a-z0-9])"
_GRADE_NUM = re.compile(
    r"\b(?:grade|WHO(?:\s+CNS)?)\s*(?P<cmp>≥|>=|≤|<=)?\s*(?P<a>" + _G + r")"
    r"(?:\s*(?:-|–|to|or|and|/|,)\s*(?:WHO\s+)?(?:grade\s*)?(?P<b>" + _G + r"))?",
    _I,
)
_GRADE_WORD = re.compile(r"\b(?:high|low)[\s-]*grade\b|\bgrade\b", _I)


def _num(g: str) -> int:
    return int(g) if g.isdigit() else _ROMAN[g.lower()]


def _is_toxicity(text: str, start: int, end: int) -> bool:
    return bool(_TOXICITY.search(text[max(0, start - 100):end + 40]))


def _grade(text: str) -> tuple[int, ConceptHit] | None:
    nums: list[tuple[int, str, str]] = []
    for m in _GRADE_NUM.finditer(text):
        if _is_toxicity(text, m.start(), m.end()):
            continue
        if m.group(0).lower().startswith("who") and not re.match(r"who(?:\s+cns)?\s*(?:iv|iii|ii|[1-4])\b", m.group(0), _I):
            continue
        a = _num(m.group("a"))
        b = _num(m.group("b")) if m.group("b") else a
        lo, hi = min(a, b), max(a, b)
        cmp = m.group("cmp")
        if cmp in ("≥", ">="):
            lo, hi = a, 4
        elif cmp in ("≤", "<="):
            lo, hi = 1, a
        nums.append((m.start(), m.group(0), f"{lo}-{hi}"))
    words = [m for m in _GRADE_WORD.finditer(text) if not _is_toxicity(text, m.start(), m.end())]
    if not nums and not words:
        return None
    values = {v for _, _, v in nums}
    value = next(iter(values)) if len(values) == 1 else None
    cands = [(s, span) for s, span, _ in nums] + [(m.start(), m.group(0)) for m in words]
    start, span = min(cands, key=lambda c: c[0])
    return start, ConceptHit("grade", span, value)


# ---------------------------------------------------------------------------
# Age
# ---------------------------------------------------------------------------

_BS = r"\\?"  # ClinicalTrials.gov escapes < and > as \< and \>
_GE = rf"(?:≥|>=|{_BS}>\s*=|=\s*{_BS}>|at\s+least|over|older\s+than|more\s+than)"
_GT = rf"(?:{_BS}>(?!\s*=))"
_LE = rf"(?:≤|<=|{_BS}<\s*=|=\s*{_BS}<|up\s+to|no\s+(?:more|older)\s+than|not\s+older\s+than)"
_LT = rf"(?:{_BS}<(?!\s*=)|under|younger\s+than|less\s+than)"
_UNIT = r"(?:years?|yrs?|months?)"
_N = r"\d{1,3}(?:\.\d+)?"

_AGE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("between", re.compile(rf"\bbetween\s+(?:the\s+ages?\s+of\s+)?(?P<lo>{_N})\s*(?P<u1>{_UNIT})?"
                           rf"\s*(?:and|-|to)\s*(?P<hi>{_N})\s*(?P<u2>{_UNIT})", _I)),
    ("range", re.compile(rf"(?P<lo>{_N})\s*(?P<u1>{_UNIT})?\s*(?:-|–|to)\s*(?P<hi>{_N})(?:\s*(?P<u2>{_UNIT}))?", _I)),
    ("ge", re.compile(rf"{_GE}\s*(?P<lo>{_N})\s*(?P<u1>{_UNIT})?", _I)),
    ("gt", re.compile(rf"{_GT}\s*(?P<lo>{_N})\s*(?P<u1>{_UNIT})?", _I)),
    ("le", re.compile(rf"{_LE}\s*(?P<hi>{_N})\s*(?P<u2>{_UNIT})?", _I)),
    ("lt", re.compile(rf"{_LT}\s*(?P<hi>{_N})\s*(?P<u2>{_UNIT})?", _I)),
    ("orolder", re.compile(rf"(?P<lo>{_N})\s*(?P<u1>{_UNIT})?(?:\s+(?:of\s+age|old))?"
                           r"\s*(?:or|and)\s+(?:older|over|above|more|greater)", _I)),
    ("plus", re.compile(rf"(?P<lo>{_N})\s*\+\s*(?P<u1>{_UNIT})", _I)),
    ("orunder", re.compile(rf"(?P<hi>{_N})\s*(?P<u2>{_UNIT})?(?:\s+(?:of\s+age|old))?\s*or\s+(?:under|younger|less)", _I)),
]
_AGE_WORD_BEFORE = re.compile(r"(?:\bage[sd]?\b|\baged\b)[\s:,()]*(?:must\s+be\s+|of\s+|is\s+|are\s+|between\s+)?$", _I)
_AGE_PEOPLE_BEFORE = re.compile(
    r"\b(?:adults?|patients?|participants?|subjects?|children|males?|females?|individuals?|persons?|people)\b"
    r"[\s:,()]*(?:must\s+be\s+|who\s+are\s+|of\s+|is\s+|are\s+|between\s+)?$",
    _I,
)
_AGE_ANCHOR_AFTER = re.compile(
    r"^\s*(?:\(inclusive\)\s*)?(?:of\s+age|old"
    r"|at\s+(?:the\s+time\s+of\s+)?(?:consent|enrollment|registration|diagnosis))", _I)
_AGE_JOIN = re.compile(r"^\s*(?:years?|yrs?)?\s*(?:old|of\s+age)?\s*(?:,|and|&)?\s*$", _I)
_AGE_WORD_NEAR = re.compile(r"\bage[sd]?\b|\baged\b|years?\s+old|of\s+age", _I)


def _years(n: str, unit: str | None) -> float:
    v = float(n)
    return v / 12 if unit and unit.lower().startswith("month") else v


def _fmt(x: float) -> str:
    return f"{x:g}"


def _age(text: str) -> tuple[int, ConceptHit] | None:
    """Age bounds with an age anchor next to them. See the module docstring."""
    cands: list[tuple[int, int, str, re.Match[str]]] = []
    for kind, pat in _AGE_PATTERNS:  # earlier kinds win overlaps
        for m in pat.finditer(text):
            if not any(a < m.end() and m.start() < b for a, b, _, _ in cands):
                cands.append((m.start(), m.end(), kind, m))
    cands.sort(key=lambda c: c[0])

    def anchored(start: int, end: int, kind: str, m: re.Match[str]) -> bool:
        gd = m.groupdict()
        unit = gd.get("u1") or gd.get("u2")
        in_years = unit is not None and not unit.lower().startswith("month")
        before = _clause_before(text, start, 30)
        after = _clause_after(text, end, 30)
        if _AGE_WORD_BEFORE.search(before) or _AGE_ANCHOR_AFTER.match(after):
            return True  # "age 18 to 70", "aged >= 18", "8-17 years of age"
        if unit is None:
            return False
        if _AGE_PEOPLE_BEFORE.search(before):
            return True  # "Adults (>= 18 years)", "Patients 18 to 70 years"
        if kind == "between" and in_years:
            return True
        if kind in ("orolder", "orunder") and _AGE_WORD_NEAR.search(before + m.group(0)):
            return True
        # A bullet that is only the bound: "18 years or older", ">= 18 years".
        return in_years and not before.strip(" \t*-•:")

    ok = [anchored(*c) for c in cands]
    # "Age >= 2 and <= 16 years": a bound joined by "and" to an accepted one.
    changed = True
    while changed:
        changed = False
        for i in range(len(cands)):
            if ok[i]:
                continue
            for j in (i - 1, i + 1):
                if 0 <= j < len(cands) and ok[j]:
                    a, b = (cands[j][1], cands[i][0]) if j < i else (cands[i][1], cands[j][0])
                    if _AGE_JOIN.match(text[a:b]):
                        ok[i] = changed = True
                        break

    groups: list[list[tuple[int, str, float | None, float | None]]] = []
    prev_end: int | None = None
    for (start, end, kind, m), good in zip(cands, ok):
        if not good or _reproductive(text, start, end):
            continue  # "POCBP ... < 55 years of age must have FSH" is not an age limit
        gd = m.groupdict()
        u_lo = gd.get("u1") or gd.get("u2")
        u_hi = gd.get("u2") or gd.get("u1")
        lo = _years(gd["lo"], u_lo) if gd.get("lo") else None
        hi = _years(gd["hi"], u_hi) if gd.get("hi") else None
        if kind == "gt" and lo is not None:
            lo += 1
        if kind == "lt" and hi is not None:
            hi -= 1
        item = (start, m.group(0), lo, hi)
        if prev_end is not None and _AGE_JOIN.match(text[prev_end:start]):
            groups[-1].append(item)
        else:
            groups.append([item])
        prev_end = end
    if not groups:
        return None
    values = {_age_value(g) for g in groups}
    value = next(iter(values)) if len(values) == 1 else None
    first = groups[0][0]
    return first[0], ConceptHit("age_range", first[1].strip(), value)


def _age_value(group: list[tuple[int, str, float | None, float | None]]) -> str | None:
    los = {g[2] for g in group if g[2] is not None}
    his = {g[3] for g in group if g[3] is not None}
    if len(los) > 1 or len(his) > 1:
        return None
    lo, hi = next(iter(los), None), next(iter(his), None)
    if lo is not None and hi is not None and lo > hi:
        return None
    return f"{_fmt(lo) if lo is not None else ''}-{_fmt(hi) if hi is not None else ''}"


# ---------------------------------------------------------------------------
# Sex
# ---------------------------------------------------------------------------

_SEX_WORD = re.compile(r"\b(?P<f>females?|women|woman|girls?)\b|\b(?P<m>males?|men|man|boys?)\b", _I)
_SEX_ANY = re.compile(r"(?:regardless\s+of|irrespective\s+of|any|of\s+any\s+(?:\w+\s+){0,3}(?:and|or))\s+(?:gender|sex)\b"
                      r"|\bboth\s+(?:sexes|genders)\b", _I)


def _sex(text: str) -> tuple[int, ConceptHit] | None:
    hits: list[tuple[int, str, str]] = []
    for m in _SEX_WORD.finditer(text):
        if _reproductive(text, m.start(), m.end()):
            continue
        hits.append((m.start(), m.group(0), "female" if m.group("f") else "male"))
    anym = _SEX_ANY.search(text)
    if anym and not _reproductive(text, anym.start(), anym.end()):
        hits.append((anym.start(), anym.group(0), "any"))
    if not hits:
        return None
    hits.sort(key=lambda h: h[0])
    kinds = {k for _, _, k in hits}
    value = next(iter(kinds)) if len(kinds) == 1 and "any" not in kinds else None
    return hits[0][0], ConceptHit("sex", hits[0][1], value)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_DETECTORS = {
    "recurrent": _recurrent,
    "newly_diagnosed": lambda t: _first("newly_diagnosed", _NEWLY, t),
    "idh": _idh,
    "codeletion_1p19q": _codeletion,
    "mgmt": _mgmt,
    "grade": _grade,
    "age_range": _age,
    "pediatric": _pediatric,
    "sex": _sex,
    "h3k27m": lambda t: _first("h3k27m", _H3K27M, t),
    "braf": lambda t: _first("braf", _BRAF, t),
}
assert tuple(_DETECTORS) == CONCEPTS


def detect(text: str) -> list[ConceptHit]:
    """Every concept mentioned in `text`, one hit per concept (its first span)."""
    found: list[tuple[int, int, ConceptHit]] = []
    for order, name in enumerate(CONCEPTS):
        r = _DETECTORS[name](text)
        if r is not None:
            found.append((r[0], order, r[1]))
    return [h for _, _, h in sorted(found, key=lambda f: (f[0], f[1]))]


def mentioned(text: str) -> set[ConceptName]:
    """Names of the concepts `text` mentions."""
    return {h.name for h in detect(text)}
