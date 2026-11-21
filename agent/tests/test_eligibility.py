"""F8: eligibility checker and F8.6 agreement with the gold labels."""

from __future__ import annotations

import dataclasses

import pytest

from agent import eligibility as el
from agent import eligibility_eval, policy
from agent.schemas import MUTATION_CHECK_FIELDS, DoctorResults, PriorTreatment
from agent.trials import search_trials


@pytest.fixture(scope="module")
def trials():
    return {t.trial_id: t for t in search_trials()}


@pytest.fixture
def policy_with(tmp_path):
    old = policy._default

    def make(edit):
        path = tmp_path / "policy.yaml"
        path.write_text(edit(policy.POLICY_PATH.read_text(encoding="utf-8")), encoding="utf-8")
        policy.set_policy_store(policy.PolicyStore(path))

    yield make
    policy.set_policy_store(old)


def results(**kw) -> DoctorResults:
    base = dict(
        case_id="t-1", confirmed_bucket="glioma", diagnosis="Glioblastoma, IDH-wildtype", grade=4,
        idh="wildtype", codeletion_1p19q="unknown", mgmt="unmethylated", recurrent=False, age=50,
        zip="77840", broad_sequencing_available=False,
        prior_treatments=[{"type": "surgery", "extent": "subtotal", "weeks_ago": 2}],
    )
    base.update(kw)
    return DoctorResults(**base)


def rows_by_text(trial, facts):
    return {r.criterion_text: r for r in el.check_trial(trial, facts)}


# --- agreement with gold labels (F8.6) --------------------------------------


def test_full_agreement_with_gold_labels_from_profile(trials):
    rep = eligibility_eval.run("profile", trials)
    assert rep.rows >= 100
    assert rep.missing == 0
    assert rep.disagreements == [], rep.disagreements


def test_entered_results_only_disagree_by_saying_unknown(trials):
    """Fields the demo leaves out of enter_results are unknown, never guessed."""
    rep = eligibility_eval.run("entered", trials)
    assert rep.missing == 0
    assert all(d.got == "unknown" for d in rep.disagreements)
    assert all(r.startswith("not recorded:") for r in (d.reason for d in rep.disagreements))


# --- criteria splitting ------------------------------------------------------


def test_split_criteria_sections_and_continuations():
    text = "Inclusion Criteria:\n\n* Age 18 years or older\n* Line one\n  continues here\n\nExclusion Criteria:\n\n- Prior bevacizumab\n1. Numbered item"
    got = el.split_criteria(text)
    assert [(c.type, c.text) for c in got] == [
        ("inclusion", "Age 18 years or older"),
        ("inclusion", "Line one continues here"),
        ("exclusion", "Prior bevacizumab"),
        ("exclusion", "Numbered item"),
    ]


# --- rule 3: unknown and pending ----------------------------------------------


def test_pending_marker_is_unknown_and_blocks_shortlist(trials):
    f = el.Facts.from_results(results(diagnosis=None, idh="pending", grade="pending", mgmt="pending"))
    rows = rows_by_text(trials["NCT99000004"], f)
    dx = rows["Histologically confirmed glioblastoma, IDH-wildtype, CNS WHO grade 4"]
    assert dx.status == "unknown"
    mgmt = rows["MGMT promoter unmethylated"]
    assert mgmt.status == "unknown" and el.is_pending(mgmt)
    assert mgmt.reason.startswith(el.PENDING_PREFIX)
    assert not el.passes(list(rows.values()))


def test_unknown_not_pending_does_not_block_shortlist(trials):
    f = el.Facts.from_results(results())  # comorbidities not recorded
    rows = el.check_trial(trials["NCT99000004"], f)
    assert el.passes(rows)


def test_missing_pathology_uses_policy_template(trials, policy_with):
    policy_with(lambda t: t.replace('"needs pathology: {field}"', '"awaiting path: {field}"'))
    f = el.Facts.from_results(results(mgmt="unknown"))
    row = rows_by_text(trials["NCT99000004"], f)["MGMT promoter unmethylated"]
    assert row.status == "unknown"
    assert row.reason == "awaiting path: MGMT promoter methylation status"


def test_unmatched_criterion_goes_to_doctor_review():
    c = el.Criterion("inclusion", "Karnofsky performance status of 70 or higher")
    f = el.evaluate(c, el.Facts.from_results(results()), policy.get_policy())
    assert f.holds is None and f.field == el.NO_RULE_FIELD


def test_newly_diagnosed_recurrence_number_defaults_to_zero():
    f = el.Facts.from_results(results(recurrent=False))
    assert f.recurrence_number == 0


# --- exclusions flip ------------------------------------------------------------


def test_exclusion_met_means_it_does_not_apply(trials):
    rows = rows_by_text(trials["NCT99000004"], el.Facts.from_results(results()))
    assert rows["Prior temozolomide"].status == "met"
    tmz = [{"type": "surgery", "extent": "subtotal"}, {"type": "temozolomide", "cycles": 6}]
    rows = rows_by_text(trials["NCT99000004"], el.Facts.from_results(results(prior_treatments=tmz)))
    assert rows["Prior temozolomide"].status == "not_met"


# --- rule 10: mutation check never decides --------------------------------------


def test_facts_carry_no_mutation_check_field():
    names = {f.name for f in dataclasses.fields(el.Facts)}
    assert not names & MUTATION_CHECK_FIELDS


TMB = "Tumor mutational burden of 10 mutations/Mb or higher by a CLIA-certified assay"


@pytest.mark.parametrize("tmb, want", [(None, "unknown"), (12.0, "met"), (4.0, "not_met")])
def test_tmb_reads_clia_result_only(trials, tmb, want):
    f = el.Facts.from_results(results(recurrent=True, recurrence_number=1, tmb_clia=tmb))
    row = rows_by_text(trials["NCT99000009"], f)[TMB]
    assert row.status == want
    assert row.evidence_field == "tmb_clia"


# --- policy-driven pre-surgery rule ----------------------------------------------

BEFORE = "Must enroll before planned surgical resection"


def test_before_surgery_met_preop_not_met_after(trials):
    pre = el.Facts(stage="preop", bucket="glioma", age=50, recurrent=False, prior_treatments=())
    assert rows_by_text(trials["NCT99000003"], pre)[BEFORE].status == "met"
    post = el.Facts.from_results(results())
    assert rows_by_text(trials["NCT99000003"], post)[BEFORE].status == "not_met"


def test_before_surgery_phrase_comes_from_policy(trials, policy_with):
    policy_with(lambda t: t.replace('    - "before planned surgical resection"\n', ""))
    pre = el.Facts(stage="preop", bucket="glioma", age=50, recurrent=False, prior_treatments=())
    row = rows_by_text(trials["NCT99000003"], pre)[BEFORE]
    # "before resection" is still listed but is not a substring; no other rule fits.
    assert row.status == "unknown" and row.evidence_field == el.NO_RULE_FIELD


# --- other rules ----------------------------------------------------------------


def test_grade_out_of_range_fails_on_grade(trials):
    f = el.Facts.from_results(results(confirmed_bucket="meningioma", diagnosis="Meningioma, CNS WHO grade 1",
                                      grade=1, idh="unknown", mgmt="unknown"))
    row = rows_by_text(trials["NCT99000005"], f)["Histologically confirmed meningioma, CNS WHO grade 2 or 3"]
    assert row.status == "not_met" and row.evidence_field == "grade"


def test_wrong_bucket_fails_histology(trials):
    f = el.Facts.from_results(results(confirmed_bucket="meningioma", diagnosis="Meningioma, CNS WHO grade 2",
                                      grade=2, idh="unknown", mgmt="unknown"))
    rows = el.check_trial(trials["NCT99000004"], f)
    assert any(r.status == "not_met" and r.evidence_field == "confirmed_bucket" for r in rows)


def test_autoimmune_category_needs_doctor_review():
    f = el.Facts.from_results(results(comorbidities=["hypertension"]))
    c = el.Criterion("exclusion", "Active autoimmune disease")
    assert el.evaluate(c, f, policy.get_policy()).holds is None
    f = el.Facts.from_results(results(comorbidities=["rheumatoid arthritis"]))
    assert el.to_row("NCT99000009", c, el.evaluate(c, f, policy.get_policy())).status == "not_met"
    f = el.Facts.from_results(results(comorbidities=[]))
    assert el.to_row("NCT99000009", c, el.evaluate(c, f, policy.get_policy())).status == "met"


def test_check_trial_is_audited(trials, audit_log):
    el.check_trial(trials["NCT99000001"], el.Facts.from_results(results()), case_id="t-1")
    entries = [e for e in audit_log.read() if e.action == "eligibility_checked"]
    assert entries and entries[-1].case_id == "t-1"
    assert entries[-1].detail["trial_id"] == "NCT99000001"


def test_other_treatment_type_is_doctor_review():
    f = el.Facts.from_results(results(prior_treatments=[PriorTreatment(type="other", detail="lomustine")]))
    c = el.Criterion("inclusion", "Newly diagnosed; no prior radiation or chemotherapy")
    out = el.evaluate(c, f, policy.get_policy())
    assert out.holds is None and out.reason.startswith("doctor review")


# --- review fixes (PR #29 review) -------------------------------------------


def post(**kw) -> el.Facts:
    return el.Facts(stage="post_pathology", bucket="glioma", **kw)


def test_recurrence_count_word_is_case_insensitive():
    c = el.Criterion("exclusion", "MORE THAN TWO PRIOR RECURRENCES")
    f = el.evaluate(c, post(recurrent=True, recurrence_number=3), policy.get_policy())
    assert f.holds is True  # 3 > 2: exclusion applies
    assert el.to_row("NCT99000002", c, f).status == "not_met"


@pytest.mark.parametrize("prefix", ["Key ", "Main ", "KEY ", ""])
def test_prefixed_headers_set_criterion_type(prefix):
    text = (f"{prefix}Inclusion Criteria:\n* Age 18 years or older\n\n"
            f"{prefix}Exclusion Criteria:\n* Prior bevacizumab")
    assert [(c.type, c.text) for c in el.split_criteria(text)] == [
        ("inclusion", "Age 18 years or older"),
        ("exclusion", "Prior bevacizumab"),
    ]


def test_histology_or_list_accepts_any_listed_entity():
    c = el.Criterion("inclusion", "Histologically confirmed astrocytoma or oligodendroglioma")
    f = el.evaluate(c, post(diagnosis="Oligodendroglioma, IDH-mutant and 1p/19q-codeleted",
                            idh="mutant", codeletion_1p19q="codeleted", grade=2), policy.get_policy())
    assert f.holds is True
    f = el.evaluate(c, post(diagnosis="Glioblastoma, IDH-wildtype", idh="wildtype", grade=4),
                    policy.get_policy())
    assert f.holds is False


def test_histology_both_idh_forms_is_no_idh_requirement():
    c = el.Criterion("inclusion", "Histologically confirmed IDH-mutant or IDH-wildtype glioma")
    for idh, dx in (("mutant", "Astrocytoma, IDH-mutant"), ("wildtype", "Glioblastoma, IDH-wildtype")):
        assert el.evaluate(c, post(diagnosis=dx, idh=idh, grade=3), policy.get_policy()).holds is True


def test_histology_met_reason_cites_every_field_used():
    c = el.Criterion("inclusion", "Histologically confirmed astrocytoma, IDH-mutant, CNS WHO grade 2 or 3")
    f = el.evaluate(c, post(diagnosis="Astrocytoma", idh="mutant", grade=3), policy.get_policy())
    assert f.holds is True
    assert "IDH status: mutant" in f.reason and "WHO grade 3" in f.reason
    assert "fields: diagnosis, idh, grade" in f.reason


def test_histology_met_reason_uses_resolved_grade():
    """Grade read from the diagnosis text, grade field still pending."""
    c = el.Criterion("inclusion", "Histologically confirmed glioblastoma, IDH-wildtype, CNS WHO grade 4")
    f = el.evaluate(c, post(diagnosis="Glioblastoma, IDH-wildtype, CNS WHO grade 4",
                            idh="wildtype", grade="pending"), policy.get_policy())
    assert f.holds is True
    assert "pending" not in f.reason and "WHO grade 4" in f.reason
