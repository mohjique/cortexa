"""F9 shortlist + F10 no-match plan, on the synthetic patients and demo cache."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent import matching, mutation, policy, workflow
from agent.schemas import (
    CaseState,
    ClassifierOutput,
    DoctorDecisionAdapter,
    DoctorResults,
    MutationCheckRan,
    NoMatchPlan,
    Shortlist,
    ShortlistTrial,
)
from agent.store import CaseRecord
from agent.workflow import AgentHooks, submit_decision

PATIENTS = Path(__file__).resolve().parents[2] / "clinical" / "patients"
FIXTURES = Path(__file__).parent / "fixtures"


def patient(case_id: str) -> dict:
    return json.loads((PATIENTS / f"{case_id}.json").read_text(encoding="utf-8"))


def with_results() -> list[str]:
    return [p.stem for p in sorted(PATIENTS.glob("demo-*.json"))
            if json.loads(p.read_text(encoding="utf-8"))["doctor_decisions"].get("enter_results")]


def record(case_id: str, **over) -> CaseRecord:
    r = DoctorResults(**(patient(case_id)["doctor_decisions"]["enter_results"] | over))
    now = datetime.now(timezone.utc)
    st = CaseState(case_id=case_id, stage="matching", waiting_on="agent",
                   confirmed_bucket=r.confirmed_bucket, updated_at=now)
    return CaseRecord(state=st, created_at=now, results=r)


def match(case_id: str, **over):
    return matching.match_case(record(case_id, **over), policy.get_policy())


@pytest.fixture
def policy_with(tmp_path):
    old = policy._default

    def make(edit):
        path = tmp_path / "policy.yaml"
        path.write_text(edit(policy.POLICY_PATH.read_text(encoding="utf-8")), encoding="utf-8")
        policy.set_policy_store(policy.PolicyStore(path))

    yield make
    policy.set_policy_store(old)


# --- every demo patient lands where the clinical team expects --------------


@pytest.mark.parametrize("case_id", with_results())
def test_patient_matches_expected_outcome(case_id):
    p = patient(case_id)
    out = match(case_id)
    if p["expected_no_match"]:
        assert isinstance(out, NoMatchPlan)
    else:
        assert isinstance(out, Shortlist)
        eligible = [t.trial_id for t in out.trials if t.group == "eligible"]
        needs_test = {t.trial_id: t.needs_test_fields for t in out.trials if t.group == "needs_test"}
        assert eligible == p["expected_trials"]
        assert needs_test == p["expected_needs_test"]


# --- shortlist ----------------------------------------------------------------


def test_shortlist_trial_has_site_status_label_and_evidence():
    out = match("demo-014")
    assert isinstance(out, Shortlist)
    (t,) = out.trials
    assert t.status_label == policy.get_policy().trial_search.status_labels[t.recruitment_status]
    assert t.nearest_site and t.nearest_site.city == "Houston" and t.nearest_site.drive_minutes
    assert t.summary.not_met == 0
    assert all(r.evidence_field and r.criterion_text for r in t.eligibility)
    assert out.origin.zip == "77840"


def test_not_yet_recruiting_trial_shown_with_its_label():
    out = match("demo-022")
    assert isinstance(out, Shortlist)
    assert out.trials[0].recruitment_status == "NOT_YET_RECRUITING"
    assert out.trials[0].status_label.startswith("Not yet recruiting")


def test_tmb_criterion_never_met_from_mutation_check():
    """demo-016 has SBS11 detected and many mutations; the TMB row stays unknown."""
    out = match("demo-016", recurrence_number=1)
    assert isinstance(out, Shortlist)
    t = {t.trial_id: t for t in out.trials}["NCT99000009"]
    tmb = [r for r in t.eligibility if "mutational burden" in r.criterion_text]
    assert len(tmb) == 1 and tmb[0].status == "unknown" and tmb[0].evidence_field == "tmb_clia"


def test_no_distance_cutoff_far_trial_is_shortlisted():
    out = match("demo-015")  # oligodendroglioma; NCT99000007 is in New Orleans, ~376 mi
    assert isinstance(out, Shortlist)
    (t,) = out.trials
    assert t.trial_id == "NCT99000007" and t.nearest_site and t.nearest_site.distance_miles > 300


def test_shortlist_grouped_then_sorted_nearest_first():
    out = match("demo-016", recurrence_number=1, can_use_phone_or_tablet=True)
    assert isinstance(out, Shortlist)
    groups = [t.group for t in out.trials]
    assert groups == sorted(groups, key=["eligible", "needs_test"].index)
    for g in ("eligible", "needs_test"):
        miles = [t.nearest_site.distance_miles for t in out.trials if t.group == g and t.nearest_site]
        assert miles == sorted(miles)
    assert sum(1 for t in out.trials if t.nearest_site) == 3


# --- shortlist groups (PR #30) --------------------------------------------------


def test_demo_016_groups():
    out = match("demo-016")
    assert isinstance(out, Shortlist)
    by_id = {t.trial_id: t for t in out.trials}
    assert by_id["NCT99000002"].group == "eligible"
    assert by_id["NCT99000002"].needs_test_fields == []
    assert by_id["NCT99000008"].group == "eligible"  # remote; phone use is routine
    tmb = by_id["NCT99000009"]
    assert tmb.group == "needs_test" and tmb.needs_test_fields == ["tmb_clia"]
    assert out.trials[-1].trial_id == "NCT99000009"


@pytest.mark.parametrize("case_id", ["demo-016", "demo-017", "demo-018", "demo-019"])
def test_tmb_trial_needs_a_test_never_eligible(case_id):
    """Rule 10: SBS11/mutation counts never make the immunotherapy trial a match."""
    out = match(case_id)
    assert isinstance(out, Shortlist)
    t = {t.trial_id: t for t in out.trials}["NCT99000009"]
    assert t.group == "needs_test" and "tmb_clia" in t.needs_test_fields


def test_clia_tmb_result_decides_the_trial():
    high = {t.trial_id: t for t in match("demo-016", tmb_clia=12.0).trials}
    assert high["NCT99000009"].group == "eligible" and high["NCT99000009"].needs_test_fields == []
    low = {t.trial_id for t in match("demo-016", tmb_clia=3.0).trials}
    assert "NCT99000009" not in low


def test_key_fields_come_from_policy(policy_with):
    policy_with(lambda y: y.replace("key_unknown_fields: [idh, codeletion_1p19q, mgmt, tmb_clia]",
                                    "key_unknown_fields: [idh, can_use_phone_or_tablet]"))
    out = match("demo-016")
    assert isinstance(out, Shortlist)
    by_id = {t.trial_id: t for t in out.trials}
    assert by_id["NCT99000009"].group == "eligible"
    assert by_id["NCT99000008"].needs_test_fields == ["can_use_phone_or_tablet"]


def test_schema_rejects_inconsistent_group():
    out = match("demo-016")
    assert isinstance(out, Shortlist)
    t = {t.trial_id: t for t in out.trials}["NCT99000009"].model_dump()
    with pytest.raises(ValueError):
        ShortlistTrial.model_validate(t | {"group": "eligible"})
    with pytest.raises(ValueError):
        ShortlistTrial.model_validate(t | {"needs_test_fields": ["idh"]})
    d = out.model_dump()
    d["trials"] = list(reversed(d["trials"]))
    with pytest.raises(ValueError):
        Shortlist.model_validate(d)


def test_search_is_nationwide(monkeypatch):
    calls = []
    real = matching.search_trials

    def spy(**kw):
        calls.append(kw)
        return real(**kw)

    monkeypatch.setattr(matching, "search_trials", spy)
    match("demo-014")
    assert len(calls) == 1 and set(calls[0]) == {"condition", "case_id"}  # no origin, no radius


# --- no-match plan --------------------------------------------------------------


def test_no_match_plan_alerts_and_logs():
    out = match("demo-012")  # MGMT-methylated GBM: no eligible trial anywhere
    assert isinstance(out, NoMatchPlan)
    assert out.searched.match_count == 0
    assert out.alert and out.alert.notify == "care_team_in_app"
    assert out.alert.query.markers == {"grade": 4, "idh": "wildtype", "mgmt": "methylated"}
    assert out.tumor_board_log and out.tumor_board_log.entry == "No current match"


def test_near_miss_fails_one_rule():
    out = match("demo-012")  # MGMT methylated GBM vs MGMT-unmethylated trial
    assert isinstance(out, NoMatchPlan)
    ids = {m.trial_id: m for m in out.near_misses}
    assert ids["NCT99000004"].near_miss_type == "fails_one_rule"
    assert ids["NCT99000004"].blocking[0].evidence_field == "mgmt"
    # Fails only on new-vs-recurrent: not a near miss.
    assert "NCT99000008" not in ids and "NCT99000009" not in ids


def test_near_miss_pending_result():
    out = match("demo-012", mgmt="pending")
    assert isinstance(out, NoMatchPlan)
    m = {m.trial_id: m for m in out.near_misses}["NCT99000004"]
    assert m.near_miss_type == "pending_result"
    assert all(b.status == "unknown" for b in m.blocking)


def test_remote_trial_is_shortlisted():
    out = match("demo-018", can_use_phone_or_tablet=True, recurrence_number=1)
    assert isinstance(out, Shortlist)
    remote = [t for t in out.trials if t.trial_id == "NCT99000008"]
    assert remote and remote[0].remote_participation


# --- policy.yaml changes behavior without code (rule 8) ------------------------


def test_status_edit_drops_not_yet_recruiting(policy_with):
    policy_with(lambda t: t.replace("statuses: [RECRUITING, NOT_YET_RECRUITING]", "statuses: [RECRUITING]")
                .replace('    NOT_YET_RECRUITING: "Not yet recruiting: enrollment has not opened"\n', ""))
    assert isinstance(match("demo-022"), NoMatchPlan)


def test_no_match_options_can_be_turned_off(policy_with):
    policy_with(lambda t: t.replace("alert_on_new_trials: true", "alert_on_new_trials: false")
                .replace("show_near_misses: true ", "show_near_misses: false "))
    out = match("demo-012")
    assert isinstance(out, NoMatchPlan)
    assert out.alert is None and out.near_misses == []


# --- end to end through the workflow ----------------------------------------------


@pytest.fixture
def real_hooks():
    old = workflow.get_hooks()
    old_source = mutation.set_catalog_source(mutation.demo_catalog)
    workflow.set_hooks(AgentHooks())
    yield
    workflow.set_hooks(old)
    mutation.set_catalog_source(old_source)


def decide(case_id, gate, decision):
    return submit_decision(DoctorDecisionAdapter.validate_python({
        "case_id": case_id, "gate": gate, "decision": decision,
        "doctor_id": "dr-synthetic-01", "ts": "2026-11-21T14:05:40Z",
    }))


def test_recurrent_glioma_end_to_end(case_store, real_hooks, audit_log):
    cid = "demo-016"
    p = patient(cid)
    case_store.create(cid)
    out = json.loads((FIXTURES / "classifier_output.json").read_text()) | {
        "case_id": cid, "suspected_bucket": "glioma"}
    workflow.attach_classification(cid, ClassifierOutput.model_validate(out))
    decide(cid, "confirm_bucket", {"bucket": "glioma", "overrode_model": False})
    rec = case_store.get(cid)
    assert rec.state.stage == "preop_screen" and rec.preop
    decide(cid, "preop_review", p["doctor_decisions"]["preop_review"] | {"screen_id": rec.preop.screen_id})
    assert case_store.get(cid).state.stage == "awaiting_pathology"
    rec = decide(cid, "enter_results", p["doctor_decisions"]["enter_results"])
    assert "stage_done:mutation_check" in [e.action for e in audit_log.read(cid)]
    assert rec.state.stage == "review_shortlist"
    assert rec.shortlist and rec.shortlist.trials[0].trial_id == "NCT99000002"  # nearest first
    t = rec.shortlist.trials[0]
    assert isinstance(t.mutation_context, MutationCheckRan)  # context only
    acts = [e.action for e in audit_log.read(cid)]
    assert "shortlist_built" in acts and "eligibility_checked" in acts
