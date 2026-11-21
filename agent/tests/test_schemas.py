"""Round-trip every fixture through its schema, then check that the rules reject bad data."""

import copy
import json
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, TypeAdapter, ValidationError

from agent import schemas as s

FIXTURES = Path(__file__).parent / "fixtures"
POLICY_FILE = Path(__file__).parents[2] / "policies" / "policy.yaml"


def policy_context() -> s.PolicyContext:
    search = yaml.safe_load(POLICY_FILE.read_text())["trial_search"]
    return {"statuses": search["statuses"], "status_labels": search["status_labels"]}


POLICY = policy_context()

# fixture file -> model class or TypeAdapter
CASES = {
    "case_state.json": s.CaseState,
    "classifier_output.json": s.ClassifierOutput,
    "decision_confirm_bucket.json": s.DoctorDecisionAdapter,
    "decision_watch_or_treat.json": s.DoctorDecisionAdapter,
    "decision_surgery_or_meds.json": s.DoctorDecisionAdapter,
    "decision_enter_results.json": s.DoctorDecisionAdapter,
    "decision_review_shortlist.json": s.DoctorDecisionAdapter,
    "results_glioma.json": s.DoctorResults,
    "results_pituitary_meds.json": s.DoctorResults,
    "preop_screen.json": s.PreOpScreen,
    "mutation_input.json": s.MutationCheckInput,
    "mutation_output_ran.json": s.MutationCheckOutputAdapter,
    "mutation_output_skipped.json": s.MutationCheckOutputAdapter,
    "trial_record.json": s.TrialRecord,
    "eligibility_result.json": s.EligibilityResult,
    "shortlist.json": s.Shortlist,
    "no_match_plan.json": s.NoMatchPlan,
    "audit_entry.json": s.AuditEntry,
}


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def validate(schema, data, context=POLICY):
    if isinstance(schema, TypeAdapter):
        return schema, schema.validate_python(data, context=context)
    return schema, schema.model_validate(data, context=context)


def dump(schema, obj) -> dict:
    if isinstance(schema, TypeAdapter):
        return schema.dump_python(obj, mode="json", exclude_unset=True)
    return obj.model_dump(mode="json", exclude_unset=True)


def test_every_fixture_has_a_case():
    assert {p.name for p in FIXTURES.glob("*.json")} == set(CASES)


@pytest.mark.parametrize("name", sorted(CASES))
def test_round_trip(name):
    raw = load(name)
    schema, obj = validate(CASES[name], raw)
    out = dump(schema, obj)
    assert out == raw
    _, again = validate(schema, out)
    assert again == obj


def test_decision_dispatches_on_gate():
    rec = s.DoctorDecisionAdapter.validate_python(load("decision_watch_or_treat.json"))
    assert isinstance(rec, s.WatchOrTreatRecord)
    assert rec.decision.rescan_in_months == 6


# --- rejections ------------------------------------------------------------


def mutate(name: str, fn):
    data = copy.deepcopy(load(name))
    fn(data)
    return data


def _set(path: str, value):
    """Return fn that sets a dotted path (ints index lists)."""

    def fn(data):
        *head, last = path.split(".")
        node = data
        for key in head:
            node = node[int(key)] if key.isdigit() else node[key]
        node[int(last) if last.isdigit() else last] = value

    return fn


BAD = [
    # strictness
    ("case_state.json", _set("extra_field", 1), "extra"),
    ("case_state.json", _set("updated_at", "2026-11-21T14:03:12"), "timezone"),
    # classifier
    ("classifier_output.json", _set("probabilities.glioma", 0.5), "sum to 1"),
    ("classifier_output.json", lambda d: d["probabilities"].pop("no_tumor"), "exactly these keys"),
    ("classifier_output.json", _set("confidence", 1.2), "less than or equal"),
    # gates: wrong decision shape for gate
    ("decision_confirm_bucket.json", _set("gate", "surgery_or_meds"), "choice"),
    ("decision_confirm_bucket.json", _set("gate", "approve_everything"), "gate"),
    ("decision_watch_or_treat.json", lambda d: d["decision"].pop("rescan_in_months"), "required when choice is watch"),
    ("decision_watch_or_treat.json", _set("decision.choice", "treat"), "only allowed when choice is watch"),
    ("decision_review_shortlist.json", _set("decision.reject.0.reason", "  "), "at least 1"),
    ("decision_review_shortlist.json", _set("decision.keep", ["NCT99000004"]), "only once"),
    ("decision_enter_results.json", _set("decision.case_id", "demo-999"), "must match"),
    # results
    ("results_glioma.json", _set("zip", "123 Main St, Bryan TX"), "pattern"),
    ("results_glioma.json", _set("grade", 5), "less than or equal"),
    ("results_glioma.json", _set("idh", "probably mutant"), "idh"),
    ("results_glioma.json", _set("sequencing", "panel"), "conflicts"),
    ("results_glioma.json", _set("pituitary", {"treatment_path": "surgery", "secreting_hormone": "none", "size": "macro", "cavernous_sinus_invasion": "no"}), "only allowed"),
    ("results_pituitary_meds.json", _set("pituitary", None), "required"),
    ("results_pituitary_meds.json", _set("idh", "wildtype"), "must be 'unknown'"),
    # pre-op
    ("preop_screen.json", _set("tests_needed.0.decides_trials", 9), "decides_trials"),
    # mutation
    ("mutation_input.json", _set("snvs.0.context", "AAG"), "middle base"),
    ("mutation_input.json", _set("snvs.0.alt", "C"), "must differ"),
    ("mutation_output_ran.json", _set("ran", False), "skip_reason"),
    ("mutation_output_skipped.json", _set("skip_reason", "felt like it"), "skip_reason"),
    # trial
    ("trial_record.json", _set("max_age_years", 10), "must not exceed"),
    ("trial_record.json", _set("recruitment_status", "COMPLETED"), "not in policy statuses"),
    ("trial_record.json", _set("recruitment_status", "recruiting"), "pattern"),
    ("preop_screen.json", _set("pre_surgery_trials.0.recruitment_status", "COMPLETED"), "not in policy"),
    ("shortlist.json", _set("statuses_searched", ["RECRUITING", "WITHDRAWN"]), "not in policy"),
    # eligibility: evidence required
    ("eligibility_result.json", _set("evidence_field", ""), "at least 1"),
    ("eligibility_result.json", lambda d: d.pop("criterion_text"), "criterion_text"),
    ("eligibility_result.json", _set("status", "likely"), "status"),
    # shortlist
    ("shortlist.json", _set("trials.0.status_label", "Open"), "status_label"),
    ("shortlist.json", _set("trials.0.summary.met", 5), "does not match"),
    ("shortlist.json", _set("trials.0.eligibility.0.status", "not_met"), "does not match"),
    ("shortlist.json", _set("trials.0.eligibility.0.trial_id", "NCT99000002"), "belong"),
    ("shortlist.json", _set("trials.0.nearest_site", None), "nearest_site required"),
    ("shortlist.json", _set("origin.zip", "7784"), "pattern"),
    # no-match
    ("no_match_plan.json", _set("searched.match_count", 2), "match_count"),
    ("no_match_plan.json", _set("widened.radius_miles", 50), "at least the searched"),
    ("no_match_plan.json", _set("near_misses.0.blocking.0.status", "unknown"), "exactly one not_met"),
    ("no_match_plan.json", _set("near_misses.0.near_miss_type", "pending_result"), "cannot have a not_met"),
    ("no_match_plan.json", _set("alert.notify", "patient_sms"), "notify"),
    # audit
    ("audit_entry.json", _set("actor", "llm"), "actor"),
]


@pytest.mark.parametrize("name,fn,match", BAD, ids=[f"{n}:{m}" for n, _, m in BAD])
def test_rejects_bad_data(name, fn, match):
    with pytest.raises(ValidationError, match=match):
        validate(CASES[name], mutate(name, fn))


def test_shortlist_trial_with_not_met_is_rejected():
    def make_consistent_not_met(d):
        t = d["trials"][0]
        t["eligibility"][0]["status"] = "not_met"
        t["summary"] = {"met": 1, "not_met": 1, "unknown": 1}

    with pytest.raises(ValidationError, match="cannot have any not_met"):
        s.Shortlist.model_validate(mutate("shortlist.json", make_consistent_not_met))


# --- policy-driven statuses (safety rule 8) ---------------------------------


def test_status_only_format_checked_without_policy():
    data = mutate("trial_record.json", _set("recruitment_status", "COMPLETED"))
    assert s.TrialRecord.model_validate(data).recruitment_status == "COMPLETED"


def test_policy_edit_adds_status_without_code_change():
    """Staff add a status + label in policy.yaml; schemas accept it, no code edit."""
    ctx: s.PolicyContext = {
        "statuses": POLICY["statuses"] + ["ENROLLING_BY_INVITATION"],
        "status_labels": {**POLICY["status_labels"], "ENROLLING_BY_INVITATION": "By invitation only"},
    }

    def invite_only(d):
        d["trials"][0]["recruitment_status"] = "ENROLLING_BY_INVITATION"
        d["trials"][0]["status_label"] = "By invitation only"

    data = mutate("shortlist.json", invite_only)
    with pytest.raises(ValidationError, match="not in policy"):
        s.Shortlist.model_validate(data, context=POLICY)
    assert s.Shortlist.model_validate(data, context=ctx).trials[0].status_label == "By invitation only"


def test_policy_status_without_label_is_rejected():
    ctx: s.PolicyContext = {
        "statuses": ["RECRUITING", "NOT_YET_RECRUITING"],
        "status_labels": {"NOT_YET_RECRUITING": POLICY["status_labels"]["NOT_YET_RECRUITING"]},
    }
    with pytest.raises(ValidationError, match="no status_label for RECRUITING"):
        s.Shortlist.model_validate(load("shortlist.json"), context=ctx)


def test_policy_has_label_for_every_status():
    assert set(POLICY["statuses"]) <= set(POLICY["status_labels"])


def test_all_models_forbid_extra():
    for obj in vars(s).values():
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
            assert obj.model_config.get("extra") == "forbid", obj.__name__
