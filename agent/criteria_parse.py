"""Parsed criteria (issue 56): an LLM reads a bullet, code decides.

A language model may turn one criterion bullet into a structured parse. It
sees only the public registry text of that bullet and whether it is an
inclusion or exclusion, never patient facts. This module never calls a model:
it takes the model's raw output, validates it, evaluates it against `Facts`
with deterministic code, and combines the result with the regex checker in
`agent.eligibility`. The LLM never decides eligibility (safety rules 1-4).

Parse shape (`Parse`):

    {"kind": "requirement",
     "applies": "always" | "conditional",
     "condition": [Req, ...],          # only for "conditional": who the bullet applies to
     "combine": "all" | "any",         # how `requirements` join (AND / OR)
     "requirements": [Req, ...]}
    Req = {"field": "...", "op": "...", "value": ..., "quote": "exact words from the bullet"}

    {"kind": "heading" | "arm_option" | "unreadable"}

Every parse describes the condition the bullet states, as written. For an
exclusion that is the excluded condition; `to_row` flips it, as for regex.

Validation (`validate_parse`), all in code. Anything that fails is ignored and
the regex result stands:

- the JSON matches `Parse` (extra keys rejected);
- every `field` is in `FIELDS`, a closed list of `Facts` fields. No mutation
  check field is on it (rule 10);
- `op` and `value` are allowed for that field;
- every `quote` appears word for word in the bullet (whitespace collapsed);
- a quote with negation ("no", "not", "without", ...) carries a negative
  requirement (`== false` or `!=`), unless it is a numeric comparison.

Evaluation (`evaluate_parse`) reads `Facts` with the same helpers the regex
rules use, so a missing field is `unknown` with the policy reason (rule 3)
and a pending result stays pending.

Combining with regex (`decide`):

- No parse, or an invalid / unreadable one: the regex finding, unchanged.
- Regex and parse both definite: agree -> that answer; disagree -> unknown.
- Regex definite, parse unknown (or a heading / arm option): unknown.
- Regex unknown, parse definite: the parse decides only if
  (a) every concept `agent.concepts` finds in the bullet maps to a parsed
      field (coverage check), and
  (b) the quotes cover every content word of the bullet (full-read check),
  and a parsed `not_met` also passes the regex not-met wording guard.

With no parse source (the default), `check_trial` behaves exactly as before.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.concepts import ConceptName, detect
from agent.eligibility import (
    GLIOMA_FAMILY,
    MARKER_LABELS,
    NO_RULE_FIELD,
    Criterion,
    Facts,
    Finding,
    _all_of,
    _any_of,
    _entity,
    _grade,
    _has_treatment,
    _leftover,
    _marker,
    _missing,
    _no,
    _not,
    _not_met_doubt,
    _passes,
    _review,
    _yes,
    evaluate,
)
from agent.policy import Policy
from agent.schemas import TreatmentType

PARSED_PREFIX = "parsed: "

# Where raw parses come from: a bullet in, the model's raw JSON (or None) out.
# PR 2 adds the model-backed, cached source; tests and live_eval pass a dict.
ParseSource = Callable[[Criterion], Any]

Op = Literal["==", "!=", ">=", "<=", ">", "<", "in"]
_NUM_OPS = frozenset({">=", "<=", ">", "<", "=="})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Req(_Strict):
    field: str = Field(min_length=1)
    op: Op
    value: Any
    quote: str = Field(min_length=1)


class Parse(_Strict):
    kind: Literal["requirement", "heading", "arm_option", "unreadable"]
    applies: Literal["always", "conditional"] = "always"
    condition: tuple[Req, ...] = ()
    combine: Literal["all", "any"] = "all"
    requirements: tuple[Req, ...] = ()


# ---------------------------------------------------------------------------
# Field vocabulary: what a parse may ask about, and how
# ---------------------------------------------------------------------------

TREATMENTS: tuple[str, ...] = tuple(t for t in get_args(TreatmentType) if t != "other")
ENTITIES: frozenset[str] = frozenset(GLIOMA_FAMILY) | {"meningioma"}


@dataclass(frozen=True)
class FieldSpec:
    ops: frozenset[str]
    check: Callable[[Any], bool]  # is this value allowed?


def _num(lo: float, hi: float) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi


def _one_of(*allowed: Any) -> Callable[[Any], bool]:
    return lambda v: not isinstance(v, (list, dict)) and v in allowed


def _list_of(allowed: frozenset[str]) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, list) and bool(v) and all(isinstance(x, str) and x in allowed for x in v)


_EQ = frozenset({"==", "!="})
_BOOL = _one_of(True, False)

FIELDS: dict[str, FieldSpec] = {
    "age": FieldSpec(_NUM_OPS, _num(0, 120)),
    "sex": FieldSpec(frozenset({"=="}), _one_of("female", "male")),
    "diagnosis": FieldSpec(frozenset({"in"}), _list_of(ENTITIES)),
    "grade": FieldSpec(_NUM_OPS, _num(1, 4)),
    "idh": FieldSpec(_EQ, _one_of("mutant", "wildtype")),
    "codeletion_1p19q": FieldSpec(_EQ, _one_of("codeleted", "intact")),
    "mgmt": FieldSpec(_EQ, _one_of("methylated", "unmethylated")),
    "recurrent": FieldSpec(frozenset({"=="}), _BOOL),
    "recurrence_number": FieldSpec(_NUM_OPS, _num(0, 20)),
    "tmb_clia": FieldSpec(_NUM_OPS, _num(0, 1000)),
    "can_use_phone_or_tablet": FieldSpec(frozenset({"=="}), _BOOL),
    **{f"prior_treatments.{t}": FieldSpec(frozenset({"=="}), _BOOL) for t in TREATMENTS},
    **{f"prior_treatments.{t}.weeks_ago": FieldSpec(_NUM_OPS, _num(0, 1000)) for t in TREATMENTS},
    "prior_treatments.temozolomide.cycles": FieldSpec(_NUM_OPS, _num(0, 100)),
    "prior_treatments.surgery.extent": FieldSpec(
        frozenset({"in"}), _list_of(frozenset({"gross_total", "subtotal", "biopsy_only"}))),
}

# Which parsed fields read each concept `agent.concepts` can find (coverage check).
# A concept with no field here (H3 K27M, BRAF) can never be covered: unknown.
CONCEPT_FIELDS: dict[ConceptName, frozenset[str]] = {
    "recurrent": frozenset({"recurrent", "recurrence_number"}),
    "newly_diagnosed": frozenset({"recurrent", "recurrence_number"}),
    "idh": frozenset({"idh"}),
    "codeletion_1p19q": frozenset({"codeletion_1p19q"}),
    "mgmt": frozenset({"mgmt"}),
    "grade": frozenset({"grade"}),
    "age_range": frozenset({"age"}),
    "pediatric": frozenset({"age"}),
    "sex": frozenset({"sex"}),
    "h3k27m": frozenset(),
    "braf": frozenset(),
}


def _squash(text: str) -> str:
    return " ".join(text.split())


def criterion_key(criterion: Criterion) -> str:
    """Cache key for a bullet's parse: same type + text parses the same way.

    The parse source adds model and prompt version on top (issue 56, PR 2).
    """
    return hashlib.sha256(f"{criterion.type}\n{_squash(criterion.text)}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Invalid:
    reason: str


# Negation inside a quote ("No prior bevacizumab", "not received radiation").
# A quote that negates must carry a negative requirement (== false, or !=), so
# a parse cannot read "no prior X" as "prior X". Numeric comparisons ("no more
# than 2 recurrences" -> <= 2) carry their own direction and are exempt.
_NEGATION = re.compile(r"\b(?:no|not|without|never|nor|none|neither|absence|free\s+of|other\s+than|except"
                       r"|excluding|naive|na[iï]ve)\b", re.IGNORECASE)
_DIRECTED_OPS = frozenset({">=", "<=", ">", "<"})


def _negative(r: Req) -> bool:
    return r.op == "!=" or r.value is False


def _check_req(r: Req, text: str) -> str | None:
    spec = FIELDS.get(r.field)
    if spec is None:
        return f"unknown field {r.field!r}"
    if r.op not in spec.ops:
        return f"op {r.op!r} not allowed for {r.field}"
    if not spec.check(r.value):
        return f"value {r.value!r} not allowed for {r.field}"
    if _squash(r.quote) not in text:
        return f"quote not found in the criterion: {r.quote[:60]!r}"
    if r.op not in _DIRECTED_OPS and _NEGATION.search(r.quote) and not _negative(r):
        return f"quote negates but the requirement is positive: {r.quote[:60]!r}"
    return None


def validate_parse(raw: Any, criterion: Criterion) -> Parse | Invalid:
    """The model's output as a checked `Parse`, or why it is rejected."""
    if not isinstance(raw, Mapping):
        return Invalid("parse is not a JSON object")
    try:
        p = Parse.model_validate(dict(raw))
    except ValidationError as e:
        return Invalid(f"parse does not match the schema: {e.errors()[0]['msg']}")
    if p.kind != "requirement":
        if p.condition or p.requirements:
            return Invalid(f"a {p.kind} parse has no requirements")
        return p
    if not p.requirements:
        return Invalid("a requirement parse needs at least one requirement")
    if (p.applies == "conditional") != bool(p.condition):
        return Invalid("a condition goes with applies=conditional, and only then")
    text = _squash(criterion.text)
    for r in (*p.condition, *p.requirements):
        if why := _check_req(r, text):
            return Invalid(why)
    return p


# ---------------------------------------------------------------------------
# Evaluation against Facts (deterministic, same helpers as the regex rules)
# ---------------------------------------------------------------------------

_CMP: dict[str, Callable[[Any, Any], bool]] = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def _compare(field: str, label: str, have: Any, r: Req) -> Finding:
    want = f"{r.op} {r.value}"
    if _CMP[r.op](have, r.value):
        return _yes(field, f"{label} {have} meets {want}")
    return _no(field, f"{label} {have} does not meet {want}")


def _treatment(facts: Facts, r: Req, policy: Policy) -> Finding:
    _, kind, *attr = r.field.split(".")
    label = kind.replace("_", " ")
    has = _has_treatment(facts, {kind}, label, policy)
    if not attr:  # prior_treatments.<kind> == true/false
        return has if r.value else _not(has)
    if has.holds is not True:
        # "at least 4 weeks since radiation" with no radiation, or a history
        # that may hold it under "other": the bullet's wording decides; review.
        return has if has.holds is None else _review(
            "prior_treatments", f"no prior {label} recorded; criterion sets {attr[0]} {r.op} {r.value}")
    given = [t for t in facts.prior_treatments or () if t.type == kind]
    name = attr[0]
    if name == "extent":
        vals = [t.extent for t in given]
        if None in vals:
            return _missing("prior_treatments", f"{label} extent", policy)
        ok = any(v in r.value for v in vals)
        return (_yes if ok else _no)("prior_treatments", f"{label} extent {vals}, criterion needs one of {r.value}")
    vals = [getattr(t, name) for t in given]
    if None in vals:
        return _missing("prior_treatments", f"{label} {name.replace('_', ' ')}", policy)
    # weeks_ago: the most recent course decides; cycles: the total.
    have = min(vals) if name == "weeks_ago" else sum(vals)
    return _compare("prior_treatments", f"{label} {name.replace('_', ' ')}", have, r)


def evaluate_req(r: Req, facts: Facts, policy: Policy) -> Finding:
    """Whether one validated requirement holds for the patient."""
    f = r.field
    if f.startswith("prior_treatments."):
        return _treatment(facts, r, policy)
    if f == "diagnosis":
        return _any_of(*(_entity(facts, e, policy) for e in r.value))
    if f in ("idh", "codeletion_1p19q", "mgmt"):
        value, why = _marker(facts, f, policy)
        if why:
            return why
        return _compare(f, MARKER_LABELS[f], value, r)
    if f == "grade":
        g, why = _grade(facts, policy)
        if why:
            return why
        return _compare("grade", "WHO grade", g, r)
    have = getattr(facts, f)
    if have is None:
        return _missing(f, f.replace("_", " "), policy)
    return _compare(f, f.replace("_", " "), have, r)


def _join(fs: list[Finding], combine: str) -> Finding:
    return _all_of(*fs) if combine == "all" else _any_of(*fs)


def evaluate_parse(p: Parse, criterion: Criterion, facts: Facts, policy: Policy) -> Finding:
    """Whether the bullet's condition holds, read from the parse."""
    if p.kind == "heading":
        return _review(NO_RULE_FIELD, "parser read this as a heading, not a requirement")
    if p.kind == "arm_option":
        return _review(NO_RULE_FIELD, "parser read this as one arm or cohort of several; doctor picks the arm")
    if p.kind == "unreadable":
        return _review(NO_RULE_FIELD, "parser could not read this criterion")
    if p.applies == "conditional":
        cond = _all_of(*(evaluate_req(r, facts, policy) for r in p.condition))
        if cond.holds is None:
            return cond
        if cond.holds is False:
            # Not one of the patients the bullet is about: it asks nothing of them.
            # Inclusion: passes. Exclusion: the excluded condition does not hold.
            return Finding(criterion.type == "inclusion", cond.field,
                           f"criterion applies only when {cond_text(p)}; {cond.reason}")
    return _join([evaluate_req(r, facts, policy) for r in p.requirements], p.combine)


def cond_text(p: Parse) -> str:
    return " and ".join(r.quote for r in p.condition)


# ---------------------------------------------------------------------------
# Combining with the regex checker
# ---------------------------------------------------------------------------


def _fields(p: Parse) -> set[str]:
    out = set()
    for r in (*p.condition, *p.requirements):
        out.add(r.field)
        if r.field.startswith("prior_treatments."):
            out.add("prior_treatments")
    return out


def _uncovered_concepts(p: Parse, text: str) -> list[str]:
    have = _fields(p)
    return [h.name for h in detect(text) if not (CONCEPT_FIELDS[h.name] & have)]


# Framing words a bullet may hold outside the quotes, per criterion type, on
# top of eligibility.FILLER_WORDS. They restate the bullet's type, so they
# change nothing. "excluded" under Inclusion reads backwards: not framing there.
FRAMING_WORDS: dict[str, frozenset[str]] = {
    "inclusion": frozenset({"eligible", "include", "included", "inclusion"}),
    "exclusion": frozenset({"exclude", "excluded", "exclusion", "ineligible"}),
}


def _unread_words(p: Parse, criterion: Criterion) -> list[str]:
    """Content words of the bullet that no quote covers (full-read check)."""
    text = criterion.text
    spans: list[tuple[int, int]] = []
    low = _squash(text).lower()
    for r in (*p.condition, *p.requirements):
        q = _squash(r.quote).lower()
        start = low.find(q)
        while start != -1:
            spans.append((start, start + len(q)))
            start = low.find(q, start + 1)
    return [w for w in _leftover(low, spans)[1] if w not in FRAMING_WORDS[criterion.type]]


def _tag(f: Finding, note: str = "") -> Finding:
    return Finding(f.holds, f.field, f"{PARSED_PREFIX}{f.reason}{note}", f.pending)


def decide(criterion: Criterion, regex: Finding, raw: Any, facts: Facts, policy: Policy) -> Finding:
    """The final finding for a bullet, from the regex finding and a raw parse."""
    if raw is None:
        return regex
    p = validate_parse(raw, criterion)
    if isinstance(p, Invalid) or p.kind == "unreadable":
        return regex
    parsed = evaluate_parse(p, criterion, facts, policy)
    if regex.holds is not None:
        if parsed.holds is None:
            # The parse sees a doubt the rules missed (a heading, an arm, a missing field).
            return _review(parsed.field, f"the rules read: {regex.reason}; but the "
                           f"{parsed.reason.removeprefix('doctor review: ')}")
        if parsed.holds == regex.holds:
            return regex
        return _review(regex.field, f"the rules and the parser disagree (rules: {regex.reason}; "
                       f"parser: {parsed.reason})")
    # Regex is unknown from here. The parse decides only if code can check it
    # read the whole bullet; otherwise the regex reason stands.
    if parsed.holds is None:
        return regex
    text = criterion.text
    if _uncovered_concepts(p, text) or _unread_words(p, criterion):
        return regex
    if not _passes(parsed, criterion.type) and _not_met_doubt(text, criterion.type, [parsed]):
        return regex
    return _tag(parsed)


def evaluate_with_parse(criterion: Criterion, facts: Facts, policy: Policy,
                        source: ParseSource | None) -> Finding:
    """The regex finding, refined by the bullet's parse when a source is given."""
    regex = evaluate(criterion, facts, policy)
    if source is None:
        return regex
    return decide(criterion, regex, source(criterion), facts, policy)


def dict_source(parses: Mapping[str, Any]) -> ParseSource:
    """A parse source backed by {criterion_key: raw parse}."""
    return lambda c: parses.get(criterion_key(c))
