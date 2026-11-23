"""LLM criteria parses (issue 56): validated, evaluated and combined in code.

No model runs here. The parses are hand-written stand-ins for model output,
so the tests pin what code does with a parse: reject it, decide from it, or
leave the regex result standing.
"""

from __future__ import annotations

import pytest

from agent import live_eval
from agent.criteria_parse import (
    FIELDS,
    PARSED_PREFIX,
    Invalid,
    Parse,
    criterion_key,
    decide,
    dict_source,
    evaluate_parse,
    validate_parse,
)
from agent.eligibility import Criterion, Facts, check_trial, evaluate, to_row
from agent.policy import get_policy
from agent.schemas import PriorTreatment, TrialRecord

POLICY = get_policy()

RADIATION_12W = Criterion("inclusion", "Participants must be ≥12 weeks from completion of radiation.")
RADIATION_12W_PARSE = {
    "kind": "requirement",
    "requirements": [{"field": "prior_treatments.radiation.weeks_ago", "op": ">=", "value": 12,
                      "quote": "≥12 weeks from completion of radiation"}],
}
BEV_EXCLUSION = Criterion("exclusion", "Participants with prior treatment with bevacizumab (Avastin) are excluded.")
BEV_PARSE = {
    "kind": "requirement",
    "requirements": [{"field": "prior_treatments.bevacizumab", "op": "==", "value": True,
                      "quote": "prior treatment with bevacizumab (Avastin)"}],
}


def _facts(**kw) -> Facts:
    base = dict(stage="post_pathology", bucket="glioma", age=41, sex="female",
                diagnosis="Astrocytoma, IDH-mutant", grade=3, idh="mutant", codeletion_1p19q="intact",
                mgmt="unknown", recurrent=True, recurrence_number=1,
                prior_treatments=(PriorTreatment(type="surgery", extent="gross_total", weeks_ago=120),
                                  PriorTreatment(type="radiation", weeks_ago=110),
                                  PriorTreatment(type="temozolomide", weeks_ago=60, cycles=12)))
    return Facts(**(base | kw))  # type: ignore[arg-type]


def _status(c: Criterion, raw, facts: Facts | None = None) -> tuple[str, str]:
    facts = facts or _facts()
    f = decide(c, evaluate(c, facts, POLICY), raw, facts, POLICY)
    row = to_row("NCT00000000", c, f)
    return row.status, row.reason


# ---------------------------------------------------------------------------
# Validation: anything off-schema is rejected, never guessed
# ---------------------------------------------------------------------------


def _req(field, op, value, quote):
    return {"kind": "requirement", "requirements": [{"field": field, "op": op, "value": value, "quote": quote}]}


@pytest.mark.parametrize("raw, why", [
    ("not json", "not a JSON object"),
    ({"kind": "requirement", "requirements": [], "extra": 1}, "schema"),
    ({"kind": "requirement", "requirements": []}, "at least one requirement"),
    (_req("kps", ">=", 70, "≥12 weeks"), "unknown field"),
    (_req("sbs11_detected", "==", True, "≥12 weeks"), "unknown field"),  # rule 10
    (_req("prior_treatments.radiation.weeks_ago", "in", [12], "≥12 weeks"), "op"),
    (_req("prior_treatments.radiation.weeks_ago", ">=", "12", "≥12 weeks"), "value"),
    (_req("prior_treatments.radiation.weeks_ago", ">=", True, "≥12 weeks"), "value"),
    (_req("prior_treatments.radiation.weeks_ago", ">=", 12, "≥ 6 weeks from radiation"), "quote not found"),
    ({"kind": "requirement", "applies": "conditional", **{"requirements": RADIATION_12W_PARSE["requirements"]}},
     "condition"),
    ({"kind": "heading", "requirements": RADIATION_12W_PARSE["requirements"]}, "no requirements"),
])
def test_invalid_parses_are_rejected(raw, why):
    out = validate_parse(raw, RADIATION_12W)
    assert isinstance(out, Invalid)
    assert why in out.reason


def test_negated_quote_needs_a_negative_requirement():
    c = Criterion("inclusion", "No prior bevacizumab.")
    bad = validate_parse(_req("prior_treatments.bevacizumab", "==", True, "No prior bevacizumab"), c)
    assert isinstance(bad, Invalid) and "negates" in bad.reason
    assert isinstance(validate_parse(_req("prior_treatments.bevacizumab", "==", False, "No prior bevacizumab"), c),
                      Parse)
    c2 = Criterion("inclusion", "No more than 2 prior recurrences.")
    assert isinstance(validate_parse(_req("recurrence_number", "<=", 2, "No more than 2 prior recurrences"), c2),
                      Parse)


def test_valid_parse_and_whitespace_in_quotes():
    raw = _req("prior_treatments.radiation.weeks_ago", ">=", 12, "≥12  weeks from\ncompletion of radiation")
    assert isinstance(validate_parse(raw, RADIATION_12W), Parse)


def test_no_mutation_check_field_in_vocabulary():
    """Rule 10: the mutation check never decides a criterion. TMB comes from a CLIA lab only."""
    for name in FIELDS:
        assert not any(w in name for w in ("mutation", "signature", "sbs", "hypermut")), name
    assert [f for f in FIELDS if "tmb" in f] == ["tmb_clia"]


# ---------------------------------------------------------------------------
# Evaluation against Facts
# ---------------------------------------------------------------------------


def _eval(c: Criterion, raw, facts: Facts):
    p = validate_parse(raw, c)
    assert isinstance(p, Parse), p
    return evaluate_parse(p, c, facts, POLICY)


def test_weeks_since_treatment_uses_most_recent_course():
    facts = _facts(prior_treatments=(PriorTreatment(type="radiation", weeks_ago=110),
                                     PriorTreatment(type="radiation", weeks_ago=8)))
    f = _eval(RADIATION_12W, RADIATION_12W_PARSE, facts)
    assert f.holds is False and f.field == "prior_treatments"


def test_weeks_since_treatment_with_no_such_treatment_is_review():
    f = _eval(RADIATION_12W, RADIATION_12W_PARSE, _facts(prior_treatments=()))
    assert f.holds is None and "no prior radiation" in f.reason


def test_missing_marker_uses_policy_reason():
    c = Criterion("inclusion", "MGMT promoter methylated tumor.")
    f = _eval(c, _req("mgmt", "==", "methylated", "MGMT promoter methylated"), _facts(mgmt=None))
    assert f.holds is None and f.reason == "needs pathology: MGMT promoter methylation status"


def test_pending_marker_stays_pending():
    c = Criterion("inclusion", "MGMT promoter methylated tumor.")
    f = _eval(c, _req("mgmt", "==", "methylated", "MGMT promoter methylated"), _facts(mgmt="pending"))
    assert f.holds is None and f.pending


CONDITIONAL_BEV = Criterion(
    "inclusion", "If received prior bevacizumab, at least 6 weeks must have elapsed since the last dose.")
CONDITIONAL_BEV_PARSE = {
    "kind": "requirement", "applies": "conditional",
    "condition": [{"field": "prior_treatments.bevacizumab", "op": "==", "value": True,
                   "quote": "received prior bevacizumab"}],
    "requirements": [{"field": "prior_treatments.bevacizumab.weeks_ago", "op": ">=", "value": 6,
                      "quote": "at least 6 weeks must have elapsed since the last dose"}],
}


def test_conditional_that_does_not_apply_passes():
    f = _eval(CONDITIONAL_BEV, CONDITIONAL_BEV_PARSE, _facts())
    assert f.holds is True and "applies only when" in f.reason


def test_conditional_that_applies_checks_the_requirement():
    facts = _facts(prior_treatments=(PriorTreatment(type="bevacizumab", weeks_ago=3),))
    assert _eval(CONDITIONAL_BEV, CONDITIONAL_BEV_PARSE, facts).holds is False


def test_conditional_exclusion_that_does_not_apply_does_not_exclude():
    c = Criterion("exclusion", "If received prior bevacizumab, last dose within 6 weeks.")
    raw = {"kind": "requirement", "applies": "conditional",
           "condition": [{"field": "prior_treatments.bevacizumab", "op": "==", "value": True,
                          "quote": "received prior bevacizumab"}],
           "requirements": [{"field": "prior_treatments.bevacizumab.weeks_ago", "op": "<", "value": 6,
                             "quote": "last dose within 6 weeks"}]}
    assert to_row("NCT00000000", c, _eval(c, raw, _facts())).status == "met"


def test_any_combine():
    c = Criterion("inclusion", "IDH-mutant or 1p/19q codeleted glioma")
    raw = {"kind": "requirement", "combine": "any", "requirements": [
        {"field": "idh", "op": "==", "value": "mutant", "quote": "IDH-mutant"},
        {"field": "codeletion_1p19q", "op": "==", "value": "codeleted", "quote": "1p/19q codeleted"}]}
    assert _eval(c, raw, _facts()).holds is True
    assert _eval(c, raw, _facts(idh="wildtype", diagnosis="Glioblastoma")).holds is False


# ---------------------------------------------------------------------------
# Combining with the regex checker
# ---------------------------------------------------------------------------


def test_no_parse_is_exactly_regex():
    facts = _facts()
    regex = evaluate(RADIATION_12W, facts, POLICY)
    assert decide(RADIATION_12W, regex, None, facts, POLICY) is regex


@pytest.mark.parametrize("raw", [{"kind": "unreadable"}, {"bad": "parse"},
                                 _req("age", ">=", 18, "not in the text")])
def test_invalid_or_unreadable_parse_leaves_regex(raw):
    facts = _facts()
    regex = evaluate(RADIATION_12W, facts, POLICY)
    assert decide(RADIATION_12W, regex, raw, facts, POLICY) is regex


def test_parse_decides_when_regex_is_unknown_and_bullet_fully_quoted():
    status, reason = _status(RADIATION_12W, RADIATION_12W_PARSE)
    assert status == "met" and reason.startswith(PARSED_PREFIX)


def test_exclusion_framing_word_is_not_unread():
    assert _status(BEV_EXCLUSION, BEV_PARSE)[0] == "met"
    bev = _facts(prior_treatments=(PriorTreatment(type="bevacizumab", weeks_ago=10),))
    assert _status(BEV_EXCLUSION, BEV_PARSE, bev)[0] == "not_met"


def test_inclusion_saying_excluded_is_not_framing():
    """'are excluded' under Inclusion reads backwards; it must not count as read."""
    c = Criterion("inclusion", "Participants with prior treatment with bevacizumab (Avastin) are excluded.")
    assert _status(c, BEV_PARSE)[0] == "unknown"


def test_unquoted_words_block_the_parse():
    c = Criterion("inclusion", "Age: 18 years or older except where otherwise specified in subprotocol.")
    status, reason = _status(c, _req("age", ">=", 18, "18 years or older"))
    assert status == "unknown" and not reason.startswith(PARSED_PREFIX)


def test_concept_missing_from_parse_blocks_it():
    """The quote covers the IDH words, but the parse never checks IDH."""
    c = Criterion("inclusion", "Glioma, IDH-mutant per local testing")
    assert evaluate(c, _facts(), POLICY).holds is None
    status, _ = _status(c, _req("diagnosis", "in", ["glioma"], "Glioma, IDH-mutant per local testing"))
    assert status == "unknown"
    ok = {"kind": "requirement", "requirements": [
        {"field": "diagnosis", "op": "in", "value": ["glioma"], "quote": "Glioma"},
        {"field": "idh", "op": "==", "value": "mutant", "quote": "IDH-mutant per local testing"}]}
    assert _status(c, ok)[0] == "met"


def test_parsed_not_met_needs_the_wording_guard():
    """A not_met from a parse still goes through the regex not-met guard."""
    c = Criterion("inclusion", "Prior bevacizumab is permitted.")
    raw = _req("prior_treatments.bevacizumab", "==", True, "Prior bevacizumab is permitted")
    assert _status(c, raw)[0] == "unknown"


def test_disagreement_with_regex_is_unknown():
    c = Criterion("inclusion", "Age ≥ 18 years")
    assert evaluate(c, _facts(), POLICY).holds is True
    status, reason = _status(c, _req("age", ">=", 65, "Age ≥ 18 years"))
    assert status == "unknown" and "disagree" in reason


def test_agreement_with_regex_keeps_regex_row():
    c = Criterion("inclusion", "Age ≥ 18 years")
    regex = evaluate(c, _facts(), POLICY)
    out = decide(c, regex, _req("age", ">=", 18, "Age ≥ 18 years"), _facts(), POLICY)
    assert out is regex


def test_parse_doubt_overrides_definite_regex():
    c = Criterion("inclusion", "Age ≥ 18 years")
    status, _ = _status(c, {"kind": "arm_option"})
    assert status == "unknown"


# ---------------------------------------------------------------------------
# check_trial and live_eval wiring
# ---------------------------------------------------------------------------


def _trial(text: str) -> TrialRecord:
    return TrialRecord(trial_id="NCT99000001", title="t", source="ctgov", recruitment_status="RECRUITING",
                       conditions=["glioma"], criteria_text=text, remote_participation=False,
                       fetched_at="2026-01-01T00:00:00Z", from_cache=True)


def test_check_trial_without_parses_is_unchanged():
    text = f"Inclusion Criteria:\n* {RADIATION_12W.text}\nExclusion Criteria:\n* {BEV_EXCLUSION.text}"
    t = _trial(text)
    assert check_trial(t, _facts()) == check_trial(t, _facts(), parses=None)
    rows = check_trial(t, _facts(), parses=dict_source({criterion_key(RADIATION_12W): RADIATION_12W_PARSE,
                                                        criterion_key(BEV_EXCLUSION): BEV_PARSE}))
    assert [r.status for r in rows] == ["met", "met"]
    assert all(r.reason.startswith(PARSED_PREFIX) for r in rows)


def test_live_eval_with_parses_counts_recall():
    labels = live_eval.load_labels()
    fixtures = live_eval.load_fixtures()
    c = Criterion("inclusion", "Participants must be ≥12 weeks from completion of radiation.")
    base, _ = live_eval.report("recurrent_astro", fixtures, labels)
    with_parse, fm = live_eval.report("recurrent_astro", fixtures, labels,
                                      dict_source({criterion_key(c): RADIATION_12W_PARSE}))
    assert with_parse.decided_by_parse == 1
    assert with_parse.label_met_as_met == base.label_met_as_met + 1
    assert with_parse.false_met == 0 and fm == []
