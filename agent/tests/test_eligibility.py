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
    f = el.evaluate(c, post(diagnosis="Ependymoma", idh="wildtype", grade=2), policy.get_policy())
    assert f.holds is False


def test_astrocytoma_vs_glioblastoma_is_doctor_review_not_a_no():
    """WHO naming overlaps: never a confident not_met between the two (issue 48)."""
    gbm = post(diagnosis="Glioblastoma, IDH-wildtype", idh="wildtype", grade=4)
    for text in ("Diffuse astrocytoma, IDH-wildtype (grade 2-4)", "Histologically confirmed WHO Grade 4 astrocytoma"):
        f = el.evaluate(el.Criterion("inclusion", text), gbm, policy.get_policy())
        assert f.holds is None and f.field == "diagnosis" and "WHO naming" in f.reason
    g4_astro = post(diagnosis="Astrocytoma, IDH-mutant, CNS WHO grade 4", idh="mutant", grade=4)
    f = el.evaluate(el.Criterion("inclusion", "Histologically confirmed glioblastoma"), g4_astro, policy.get_policy())
    assert f.holds is None and "WHO naming" in f.reason
    # A grade 3 astrocytoma is still not a glioblastoma.
    g3_astro = post(diagnosis="Astrocytoma, IDH-mutant", idh="mutant", grade=3)
    f = el.evaluate(el.Criterion("inclusion", "Histologically confirmed glioblastoma"), g3_astro, policy.get_policy())
    assert f.holds is False


def test_idh1_r132h_criterion():
    """R132H-wildtype rules out one IDH1 variant only; a mutant patient is unknown."""
    c = el.Criterion("inclusion", "WHO grade 2-4 glioma that is IDH1 R132H-wildtype")
    wt = post(diagnosis="Glioblastoma, IDH-wildtype", idh="wildtype", grade=4)
    mut = post(diagnosis="Astrocytoma, IDH-mutant", idh="mutant", grade=3)
    assert el.evaluate(c, wt, policy.get_policy()).holds is True
    f = el.evaluate(c, mut, policy.get_policy())
    assert f.holds is None and f.field == "idh" and "R132H" in f.reason
    pos = el.Criterion("inclusion", "Astrocytoma, IDH1 R132H-positive")
    assert el.evaluate(pos, mut, policy.get_policy()).holds is None
    pending = post(diagnosis="Glioblastoma", idh="pending", grade=4)
    assert el.is_pending(el.to_row("NCT99000001", c,el.evaluate(c, pending, policy.get_policy())))


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


# --- issue 48: every rule runs, nothing passes on a partial read -----------------


def ev(kind: str, text: str, facts: el.Facts) -> el.Finding:
    return el.evaluate(el.Criterion(kind, text), facts, policy.get_policy())  # type: ignore[arg-type]


def status(kind: str, text: str, facts: el.Facts) -> str:
    c = el.Criterion(kind, text)  # type: ignore[arg-type]
    return el.to_row("NCT00000000", c, el.evaluate(c, facts, policy.get_policy())).status


NEW_GBM = dict(diagnosis="Glioblastoma, IDH-wildtype", idh="wildtype", grade=4, mgmt="unmethylated",
               codeletion_1p19q="intact", recurrent=False, recurrence_number=0, age=54,
               prior_treatments=(PriorTreatment(type="surgery", extent="biopsy_only"),))
REC_GBM = NEW_GBM | dict(recurrent=True, recurrence_number=1, prior_treatments=(
    PriorTreatment(type="surgery", extent="gross_total"), PriorTreatment(type="radiation"),
    PriorTreatment(type="temozolomide")))


def test_multi_rule_and_every_part_must_hold():
    text = "Histologically confirmed recurrent glioblastoma, IDH-wildtype"
    f = ev("inclusion", text, post(**NEW_GBM))
    assert f.holds is False and f.field == "recurrent"  # diagnosis and IDH pass; recurrence fails
    f = ev("inclusion", text, post(**REC_GBM))
    assert f.holds is True and f.field == "diagnosis"
    assert "fields: diagnosis, recurrent, idh" in f.reason  # rule 4: every field used


def test_recurrent_glioblastoma_for_newly_diagnosed_patient():
    for text in ("Recurrent glioblastoma", "Progressive glioblastoma after prior surgery or biopsy",
                 "Tissue-based diagnosis of recurrent glioblastoma, IDH-wildtype by a neuropathologist"):
        assert status("inclusion", text, post(**NEW_GBM)) == "not_met", text
    assert status("inclusion", "Recurrent glioblastoma", post(**REC_GBM)) == "met"


def test_negated_recurrence_is_not_read_as_recurrent():
    f = ev("inclusion", "No evidence of progression", post(**REC_GBM))
    assert f.holds is None


def test_coverage_guard_downgrades_met_on_unchecked_concept():
    f = ev("inclusion", "Histologically confirmed glioblastoma, H3 K27M-altered", post(**NEW_GBM))
    assert f.holds is None and f.field == "h3k27m"
    assert f.reason.startswith("needs pathology: H3 K27M status")  # rule 3 wording from policy.yaml
    f = ev("inclusion", "Histologically confirmed glioblastoma, IDH testing done", post(**NEW_GBM))
    assert f.holds is None and f.field == "doctor_review:idh" and "IDH status" in f.reason


def test_coverage_guard_lets_a_failing_part_stand():
    f = ev("inclusion", "Histologically confirmed oligodendroglioma, H3 K27M-altered", post(**NEW_GBM))
    assert f.holds is False and f.field == "diagnosis"


def test_full_read_guard_downgrades_met_with_unread_wording():
    f = ev("exclusion", "Prior bevacizumab given within a clinical trial", post(**NEW_GBM))
    assert f.holds is None and f.field == el.NO_RULE_FIELD and "given" in f.reason
    assert status("exclusion", "Prior bevacizumab", post(**NEW_GBM)) == "met"


def test_exclusion_direction_never_passes_on_unknown_or_unread():
    # Condition unknown -> unknown, not met.
    assert status("exclusion", "Prior radiation", post(**NEW_GBM | dict(prior_treatments=None))) == "unknown"
    # Condition False on what was read, but a concept unread -> unknown, not met.
    assert status("exclusion", "Prior bevacizumab for H3 K27M-altered glioma", post(**NEW_GBM)) == "unknown"
    # Every part read and failing -> met; holding -> not_met.
    meningioma = post(diagnosis="Meningioma, CNS WHO grade 2", grade=2, age=60, prior_treatments=())
    meningioma = dataclasses.replace(meningioma, bucket="meningioma")
    assert status("exclusion", "Prior radiation to the meningioma", meningioma) == "met"
    assert status("exclusion", "Prior radiation", post(**REC_GBM)) == "not_met"


def test_exclusion_list_excludes_on_any_one_item():
    # Prior bevacizumab, no checkpoint or IDH inhibitor. One item applying
    # excludes; the items that do not apply never make the row pass.
    rec = post(**REC_GBM | dict(prior_treatments=(
        PriorTreatment(type="surgery"), PriorTreatment(type="radiation"),
        PriorTreatment(type="temozolomide"), PriorTreatment(type="bevacizumab"))))
    for text in ("Prior bevacizumab, checkpoint inhibitors, IDH inhibitors",
                 "Prior checkpoint inhibitors; prior bevacizumab",
                 "Prior checkpoint inhibitors and prior bevacizumab",
                 "Prior checkpoint inhibitor or prior bevacizumab"):
        f = ev("exclusion", text, rec)
        assert f.holds is True and "bevacizumab" in f.reason, (text, f)
    # Concept parts that disagree across a list separator: never met.
    for text in ("IDH-mutant, MGMT promoter unmethylated tumors", "IDH-mutant or MGMT promoter unmethylated"):
        assert status("exclusion", text, post(**NEW_GBM)) == "unknown", text
    # A qualifier with no separator still reads as one condition.
    assert status("exclusion", "IDH-mutant glioma", post(**NEW_GBM)) == "met"


def test_either_or_outside_read_spans_is_unknown():
    f = ev("inclusion", "MGMT promoter methylated or prior temozolomide", post(**REC_GBM))
    assert f.holds is None and "either/or" in f.reason


def test_pending_propagates_through_combination_and_guard():
    rows = [el.to_row("NCT00000000", el.Criterion("inclusion", t), ev("inclusion", t, post(**NEW_GBM | dict(
        mgmt="pending"))))
            for t in ("Histologically confirmed glioblastoma, IDH-wildtype, MGMT promoter unmethylated",
                      "MGMT testing required")]
    assert all(el.is_pending(r) for r in rows), [r.reason for r in rows]


def test_pediatric_criteria_and_adult_patient():
    assert ev("inclusion", "Pediatric patients with glioma", post(**NEW_GBM)).holds is False
    f = ev("inclusion", "Pediatric patients (8-17 years of age)", post(**NEW_GBM))
    assert f.holds is False and f.field == "age"
    assert ev("inclusion", "Pediatric patients with glioma", post(**NEW_GBM | dict(age=10))).holds is None
    # An adult is named too: the pediatric word decides nothing.
    assert ev("inclusion", "Children and adults with glioma", post(**NEW_GBM)).holds is not False


def test_text_age_range_is_read_against_age():
    assert status("inclusion", "Patients 18 to 70 years of age", post(**NEW_GBM)) == "met"
    assert status("inclusion", "Patients 18 to 50 years of age", post(**NEW_GBM)) == "not_met"


def test_sex_in_text_is_checked_against_sex():
    f = ev("inclusion", "Female patients with glioma", post(**NEW_GBM | dict(sex="male")))
    assert f.holds is False and f.field == "sex"
    assert ev("inclusion", "Female patients with glioma", post(**NEW_GBM)).holds is None  # not recorded


def test_glioma_subtype_is_doctor_review():
    assert status("inclusion", "Diffuse midline glioma", post(**NEW_GBM)) == "unknown"


# --- issue 48 live dry run: a misread must not hide the trial (not-met guard) -----
# Bullet texts below are verbatim from live ClinicalTrials.gov trials.

FEMALE_GBM = NEW_GBM | dict(sex="female")
MALE_GBM = NEW_GBM | dict(sex="male")


@pytest.mark.parametrize("kind, text, facts", [
    # A condition on those who had the treatment, or a permission.
    ("inclusion", "For patients who have received prior bevacizumab, at least 6 weeks must have elapsed prior "
                  "to enrollment.", NEW_GBM),
    ("inclusion", "Prior checkpoint inhibitors permitted 3 weeks prior to enrollment", NEW_GBM),
    ("inclusion", "Patients may have received prior chemotherapy regimens and prior radiation (there is no limit)",
     NEW_GBM),
    ("inclusion", "Prior brain surgery is allowed, although a lesion situated in the operative bed would not be "
                  "selected", NEW_GBM | dict(prior_treatments=())),
    # A requirement or a qualifier, read as an exclusion that applies.
    ("exclusion", "Prior Surgery - completed wound healing must occur prior to initiation of treatment", NEW_GBM),
    ("exclusion", "Prior radiation therapy that would overlap with current target volume", REC_GBM),
    ("exclusion", "In cohorts who have received prior brain-directed radiation, patients are not eligible for "
                  "this study if they have active radiation necrosis", REC_GBM),
    # "Not eligible" sentences under Inclusion Criteria.
    ("inclusion", "Patients with IDH2 mutations are not eligible.", NEW_GBM),
    ("inclusion", "Patients with oligodendroglioma, IDH-mutant and 1p/19q-codeleted are not eligible.", NEW_GBM),
    # One arm or cohort of several, or a slash list of alternatives.
    ("inclusion", "Arm B (newly diagnosed GBM)", REC_GBM),
    ("inclusion", "Cohort A: Newly Diagnosed DIPG", REC_GBM),
    ("inclusion", "Histologically diagnosed as primary/relapsed/metastasized brain glioma", NEW_GBM),
    # A heading for the sub-bullets under it (the registry said ALL sexes).
    ("inclusion", "Female patients:", MALE_GBM),
    ("inclusion", "For Female participants:", MALE_GBM),
])
def test_not_met_guard_leaves_misreadable_failures_to_the_doctor(kind, text, facts):
    f = ev(kind, text, post(**facts))
    assert status(kind, text, post(**facts)) == "unknown", f
    # Either the guard caught a rule's failure, or no rule read the bullet at all.
    assert el.NOT_MET_DOUBT_MARKER in f.reason or f.reason == el.NO_RULE_REASON


def test_not_met_guard_keeps_decisive_failures():
    # Treatment wording does not undo a failing tumor type in the same bullet.
    text = ("Confirmed with recurrent/progressive IDH-mutant 1p/19q co-deleted Oligodendroglioma WHO grade 2 or "
            "WHO grade 3 (Patients who received prior radiation must be more than 12 weeks from completion of "
            "radiation).")  # NCT06254326, labeled not_met for this patient
    assert status("inclusion", text, post(**NEW_GBM)) == "not_met"
    # A lead-in that says "must" still asks for something.
    text = "Patients must have an established diagnosis of recurrent glioblastoma and:"
    assert status("inclusion", text, post(**NEW_GBM)) == "not_met"
    # Plain requirements still fail.
    assert status("inclusion", "Histologically confirmed recurrent glioblastoma.", post(**NEW_GBM)) == "not_met"
    assert status("exclusion", "Prior radiation therapy to the brain.", post(**REC_GBM)) == "not_met"
    assert status("inclusion", "Women aged 18-75 years (inclusive).", post(**MALE_GBM)) == "not_met"
    assert status("inclusion", "Newly diagnosed primary glioblastoma by World Health Organization (WHO) 2021 "
                               "criteria", post(**REC_GBM)) == "not_met"


BEV_GBM = REC_GBM | dict(prior_treatments=REC_GBM["prior_treatments"] + (PriorTreatment(type="bevacizumab"),))


@pytest.mark.parametrize("kind, text, facts", [
    # Wording that looks like a permission or condition but states the exclusion.
    ("exclusion", "Prior bevacizumab is not permitted", BEV_GBM),
    ("exclusion", "Prior bevacizumab will not be allowed", BEV_GBM),
    ("exclusion", "Patients are excluded if they have received prior bevacizumab", BEV_GBM),
    ("exclusion", "Prior bevacizumab or other VEGF inhibitor since diagnosis", BEV_GBM),
    # A trailing "(if ...)" qualifies a requirement the bullet already made.
    ("inclusion", "Recurrent glioblastoma (if multiple recurrences, only first eligible)", NEW_GBM),
])
def test_not_met_guard_keeps_exclusions_stated_as_permissions_or_conditions(kind, text, facts):
    assert status(kind, text, post(**facts)) == "not_met", ev(kind, text, post(**facts))


GUARD_TEXTS = [
    "Female patients:", "Arm B (newly diagnosed GBM)", "Prior checkpoint inhibitors permitted",
    "Prior bevacizumab is not permitted", "Patients are excluded if they have received prior bevacizumab",
    "For patients who have received prior bevacizumab, at least 6 weeks must have elapsed prior to enrollment.",
    "Prior radiation therapy that would overlap with current target volume",
    "Histologically diagnosed as primary/relapsed/metastasized brain glioma",
    "Patients with IDH2 mutations are not eligible.", "Group 1: IDH-mutant astrocytoma",
]


@pytest.mark.parametrize("kind", ["inclusion", "exclusion"])
def test_not_met_guard_only_turns_not_met_into_unknown(kind, monkeypatch):
    """With the guard off and on, the only change allowed is not_met -> unknown."""
    cases = [(t, f) for t in GUARD_TEXTS for f in (FEMALE_GBM, MALE_GBM, REC_GBM, NEW_GBM, BEV_GBM)]
    on = [status(kind, t, post(**f)) for t, f in cases]
    monkeypatch.setattr(el, "_not_met_doubt", lambda *a: None)
    off = [status(kind, t, post(**f)) for t, f in cases]
    changed = [(c, b, a) for c, b, a in zip(cases, off, on) if b != a]
    assert changed, "the guard should change some of these rows"
    assert all(b == "not_met" and a == "unknown" for _, b, a in changed), changed
