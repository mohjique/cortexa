"""Round-trip every fixture through its schema, then check that the rules reject bad data."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from agent import schemas as s
from agent.policy import POLICY_PATH, parse_policy

FIXTURES = Path(__file__).parent / "fixtures"
POLICY = parse_policy(POLICY_PATH.read_text(encoding="utf-8")).status_context()

# fixture file -> model class or TypeAdapter
CASES = {
    "case_state.json": s.CaseState,
    "classifier_output.json": s.ClassifierOutput,
    "decision_confirm_bucket.json": s.DoctorDecisionAdapter,
    "decision_watch_or_treat.json": s.DoctorDecisionAdapter,
    "decision_preop_review.json": s.DoctorDecisionAdapter,
    "decision_surgery_or_meds.json": s.DoctorDecisionAdapter,
    "decision_enter_results.json": s.DoctorDecisionAdapter,
    "decision_review_shortlist.json": s.DoctorDecisionAdapter,
    "results_glioma.json": s.DoctorResults,
    "results_pituitary_meds.json": s.DoctorResults,
    "preop_screen.json": s.PreOpScreen,
    "mutation_input.json": s.MutationCheckInput,
    "mutation_input_counts.json": s.MutationCheckInput,
    "mutation_output_ran.json": s.MutationCheckOutputAdapter,
    "mutation_output_ran_limits.json": s.MutationCheckOutputAdapter,
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
    ("decision_preop_review.json", _set("decision.tests_reviewed", False), "tests_reviewed"),
    ("decision_preop_review.json", _set("decision.pre_surgery_trials.0.action", "maybe"), "action"),
    (
        "decision_preop_review.json",
        _set("decision.pre_surgery_trials.0", {"trial_id": "NCT99000003", "action": "not_pursuing"}),
        "reason is required",
    ),
    ("decision_preop_review.json", _set("decision.pre_surgery_trials.0.reason", "   "), "at least 1"),
    (
        "decision_preop_review.json",
        _set(
            "decision.pre_surgery_trials",
            [{"trial_id": "NCT99000003", "action": "pursue_before_surgery"}] * 2,
        ),
        "only once",
    ),
    ("decision_enter_results.json", _set("decision.case_id", "demo-999"), "must match"),
    # results
    ("results_glioma.json", _set("zip", "123 Main St, Bryan TX"), "pattern"),
    ("results_glioma.json", _set("grade", 5), "less than or equal"),
    ("results_glioma.json", _set("idh", "probably mutant"), "idh"),
    ("results_glioma.json", _set("sequencing", "panel"), "conflicts"),
    ("results_glioma.json", _set("pituitary", {"treatment_path": "surgery", "secreting_hormone": "none", "size": "macro", "cavernous_sinus_invasion": "no"}), "only allowed"),
    ("results_glioma.json", _set("grade", "probably 4"), "grade"),
    ("results_glioma.json", _set("recurrence_number", 0), "1 or more"),
    ("results_glioma.json", _set("tmb_clia", -1), "greater than or equal"),
    ("results_glioma.json", _set("prior_treatments", ["surgery"]), "prior_treatments"),
    ("results_glioma.json", _set("prior_treatments.1.extent", "subtotal"), "extent is only allowed for surgery"),
    ("results_glioma.json", _set("prior_treatments.0.cycles", 3), "cycles is only allowed for temozolomide"),
    ("results_glioma.json", _set("prior_treatments.0.type", "chemo_maybe"), "type"),
    ("results_glioma.json", _set("prior_treatments.0.extent", "most_of_it"), "extent"),
    ("results_pituitary_meds.json", _set("recurrence_number", 2), "must be 0"),
    ("results_pituitary_meds.json", _set("pituitary.surgical_candidate", "maybe"), "surgical_candidate"),
    ("results_pituitary_meds.json", _set("pituitary", None), "required"),
    ("results_pituitary_meds.json", _set("idh", "wildtype"), "must be 'unknown'"),
    # pre-op
    ("preop_screen.json", _set("tests_needed.0.decides_trials", 9), "decides_trials"),
    ("preop_screen.json", _set("tests_needed.1.mentions_trials", 0), "mentions_trials"),
    ("preop_screen.json", _set("tests_needed.1.mention_trial_ids", ["NCT99000001"]), "not both"),
    ("preop_screen.json", _set("tests_needed.0.evidence.0.requires", None), "mention_trial_ids"),
    ("preop_screen.json", _set("tests_needed.2.evidence", [{"trial_id": "NCT99000007", "criterion_text": "x"}]),
     "mention_trial_ids"),
    ("preop_screen.json", lambda d: d["tests_needed"][0]["evidence"].pop(), "cited criterion"),
    ("preop_screen.json", _set("tests_needed.0.field", "tmb"), "field"),
    # mutation
    ("mutation_input.json", _set("snvs.0.context", "AAG"), "middle base"),
    ("mutation_input.json", _set("snvs.0.alt", "C"), "must differ"),
    ("mutation_output_ran.json", _set("ran", False), "skip_reason"),
    ("mutation_output_skipped.json", _set("skip_reason", "felt like it"), "skip_reason"),
    # mutation: counts_96 input (issue #6)
    ("mutation_input.json", _set("counts_96", [0] * 96), "exactly one of snvs or counts_96"),
    ("mutation_input_counts.json", lambda d: d.pop("counts_96"), "exactly one of snvs or counts_96"),
    ("mutation_input_counts.json", _set("counts_96", [1] * 95), "at least 96"),
    ("mutation_input_counts.json", _set("counts_96", [1] * 97), "at most 96"),
    ("mutation_input_counts.json", _set("counts_96.0", -1), "greater than or equal"),
    ("mutation_input_counts.json", _set("counts_96.0", 1.5), "valid integer"),
    # mutation: optional limit fields (issue #6)
    ("mutation_output_ran_limits.json", _set("sbs11_fraction_ci95", [0.3, 0.1]), "low must not exceed high"),
    ("mutation_output_ran_limits.json", _set("sbs11_fraction_ci95", [0.0, 1.2]), "less than or equal"),
    ("mutation_output_ran_limits.json", _set("sbs11_fraction_ci95", [0.1]), "sbs11_fraction_ci95"),
    ("mutation_output_ran_limits.json", _set("critical_fraction", 0.7), "must not exceed detection_limit_95"),
    ("mutation_output_ran_limits.json", _set("detection_limit_95", 1.5), "less than or equal"),
    ("mutation_output_ran_limits.json", _set("summary", "  "), "at least 1"),
    (
        "mutation_output_ran_limits.json",
        lambda d: d.update(critical_fraction=0.53, detection_limit_95=None),
        "must be inconclusive",
    ),
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
    ("eligibility_result.json", _set("criterion_type", "maybe"), "criterion_type"),
    # shortlist
    ("shortlist.json", _set("trials.0.status_label", "Open"), "status_label"),
    ("shortlist.json", _set("trials.0.summary.met", 5), "does not match"),
    ("shortlist.json", _set("trials.0.eligibility.0.status", "not_met"), "does not match"),
    ("shortlist.json", _set("trials.0.eligibility.0.trial_id", "NCT99000002"), "belong"),
    ("shortlist.json", _set("origin.zip", "7784"), "pattern"),
    # no-match
    ("no_match_plan.json", _set("searched.match_count", 2), "match_count"),
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


# --- no distance cutoff: nearest first, unknown distance last ---------------


def _add_trial(d, trial_id, miles):
    """Copy trial 0 as another trial; miles=None makes it remote-only."""
    t = copy.deepcopy(d["trials"][0])
    t["trial_id"] = trial_id
    for row in t["eligibility"]:
        row["trial_id"] = trial_id
    if miles is None:
        t["nearest_site"], t["remote_participation"] = None, True
    else:
        t["nearest_site"]["distance_miles"] = miles
    d["trials"].append(t)


def test_shortlist_keeps_far_and_remote_trials_nearest_first():
    def far_then_remote(d):
        _add_trial(d, "NCT99000007", 376.1)
        _add_trial(d, "NCT99000008", None)

    out = s.Shortlist.model_validate(mutate("shortlist.json", far_then_remote))
    assert [t.trial_id for t in out.trials] == ["NCT99000001", "NCT99000007", "NCT99000008"]


@pytest.mark.parametrize(
    "trial_id,miles,reverse",
    [("NCT99000007", 10.0, False), ("NCT99000008", None, True)],
    ids=["nearer_trial_after_farther", "remote_before_located"],
)
def test_shortlist_out_of_order_is_rejected(trial_id, miles, reverse):
    def misordered(d):
        _add_trial(d, trial_id, miles)  # appended after the 84.6 mi trial
        if reverse:
            d["trials"].reverse()

    with pytest.raises(ValidationError, match="nearest first"):
        s.Shortlist.model_validate(mutate("shortlist.json", misordered))


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


# --- safety rule 10: mutation check never decides eligibility ---------------


def _tmb_row(**kw) -> dict:
    row = {
        "trial_id": "NCT99000009",
        "criterion_type": "inclusion",
        "criterion_text": "High tumor mutational burden by CLIA-certified assay",
        "status": "met",
        "evidence_field": "mutation_context",
        "reason": "x",
    }
    row.update(kw)
    return row


@pytest.mark.parametrize(
    "field",
    [
        "mutation_context",
        "sbs11_call",
        "exposures",
        "total_mutations",
        "mutation_context.sbs11_call",
        "mutation_context.total_mutations",
        "counts_96",
        "sbs11_fraction_ci95",
        "detection_limit_95",
        "critical_fraction",
    ],
)
@pytest.mark.parametrize("status", ["met", "not_met"])
def test_mutation_check_field_cannot_decide(field, status):
    with pytest.raises(ValidationError, match="safety rule 10"):
        s.EligibilityResult.model_validate(_tmb_row(evidence_field=field, status=status))


def test_mutation_check_field_may_be_unknown_context():
    row = _tmb_row(status="unknown", reason="needs CLIA TMB; mutation check is context only")
    assert s.EligibilityResult.model_validate(row).status == "unknown"


def test_mutation_limits_are_optional():
    """Existing producers that send none of the issue #6 fields still validate."""
    out = s.MutationCheckRan.model_validate(load("mutation_output_ran.json"))
    assert out.sbs11_fraction_ci95 is None and out.detection_limit_95 is None
    assert out.critical_fraction is None and out.summary is None


def test_too_few_mutations_is_inconclusive_with_null_detection_limit():
    """demo-017: 30 mutations, 95% detection unreachable even at 100% SBS11."""
    data = mutate(
        "mutation_output_ran_limits.json",
        lambda d: d.update(sbs11_call="inconclusive", critical_fraction=0.5347, detection_limit_95=None),
    )
    assert s.MutationCheckRan.model_validate(data).sbs11_call == "inconclusive"


def test_counts_input_dumps_without_snvs_key():
    """The mutation check rejects a request carrying both keys, even if one is null."""
    req = s.MutationCheckInput.model_validate(load("mutation_input_counts.json"))
    assert "snvs" not in req.model_dump(mode="json", exclude_none=True)


def test_clia_tmb_can_decide():
    assert s.EligibilityResult.model_validate(_tmb_row(evidence_field="tmb_clia")).status == "met"


def test_grade_accepts_pending_and_unknown():
    for value in ("pending", "unknown", 2, None):
        data = mutate("results_glioma.json", _set("grade", value))
        assert s.DoctorResults.model_validate(data).grade == value


def test_optional_profile_fields_default_to_not_recorded():
    data = load("results_glioma.json")
    for key in ("tmb_clia", "recurrence_number", "comorbidities", "can_use_phone_or_tablet"):
        data.pop(key)
    r = s.DoctorResults.model_validate(data)
    assert r.comorbidities is None and r.can_use_phone_or_tablet is None  # unknown, not "none"


def test_all_models_forbid_extra():
    for obj in vars(s).values():
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
            assert obj.model_config.get("extra") == "forbid", obj.__name__
