"""Live ClinicalTrials.gov criteria text vs hand labels (issue 48).

The fixture and label checks pass today. The checker cases are the misses
reported on live text; they fail until `evaluate()` stops letting one rule
decide a whole bullet, so they are marked xfail (not strict).
"""

from __future__ import annotations

import pytest

from agent import live_eval
from agent.eligibility import Criterion, Facts, evaluate, split_criteria, to_row
from agent.policy import get_policy
from agent.schemas import DoctorResults

PROFILES = ("new_gbm", "recurrent_astro")
STATUSES = {"met", "not_met", "unknown"}
FIX_REASON = "fixed by evaluate() rewrite, issue 48 step 5"


@pytest.fixture(scope="module")
def fixtures():
    return live_eval.load_fixtures()


@pytest.fixture(scope="module")
def labels():
    return live_eval.load_labels()


def _status(text: str, kind: str, profile: str, labels) -> str:
    c = Criterion(kind, text)  # type: ignore[arg-type]
    return to_row("NCT00000000", c, evaluate(c, live_eval.profile_facts(labels, profile), get_policy())).status


def _bullet(labels, trial_id: str, index: int) -> dict:
    b = labels["trials"][trial_id][index]
    assert b["index"] == index
    return b


# --- fixtures and labels -------------------------------------------------------


def test_fixtures_are_live_glioma_trials(fixtures):
    assert 20 <= len(fixtures) <= 30
    keys = {"trial_id", "title", "conditions", "criteria_text", "minimum_age", "maximum_age",
            "sex", "overall_status", "fetched_at"}
    for tid, f in fixtures.items():
        assert keys <= set(f), tid
        assert f["trial_id"] == tid and tid.startswith("NCT")
        assert f["overall_status"] in {"RECRUITING", "NOT_YET_RECRUITING"}
        assert f["criteria_text"].strip()


def test_labels_cover_every_bullet_of_every_fixture(fixtures, labels):
    assert set(labels["trials"]) == set(fixtures)
    for tid, f in fixtures.items():
        bullets = split_criteria(f["criteria_text"])
        rows = labels["trials"][tid]
        assert [r["index"] for r in rows] == list(range(len(bullets))), tid
        for r, c in zip(rows, bullets):
            assert r["text"] == c.text and r["criterion_type"] == c.type, (tid, r["index"])
            assert set(r["labels"]) == set(PROFILES)
            assert set(r["labels"].values()) <= STATUSES


def test_label_profiles_are_doctor_results(labels):
    assert set(labels["profiles"]) == set(PROFILES)
    new = DoctorResults(**labels["profiles"]["new_gbm"])
    rec = DoctorResults(**labels["profiles"]["recurrent_astro"])
    assert (new.age, new.idh, new.grade, new.mgmt, new.codeletion_1p19q, new.recurrent) == (
        54, "wildtype", 4, "unmethylated", "intact", False)
    assert (rec.age, rec.idh, rec.grade, rec.codeletion_1p19q, rec.recurrent, rec.recurrence_number) == (
        41, "mutant", 3, "intact", True, 1)
    assert {t.type for t in rec.prior_treatments} == {"surgery", "radiation", "temozolomide"}


def test_both_label_kinds_present(labels):
    """Each profile has decided labels both ways, not only unknowns."""
    for p in PROFILES:
        got = {b["labels"][p] for bs in labels["trials"].values() for b in bs}
        assert got == STATUSES


# --- live_eval helpers ------------------------------------------------------------


def _row(tid: str, i: int, status: str) -> live_eval.LiveRow:
    return live_eval.LiveRow(tid, i, "inclusion", "x", status, "f", "r")


def test_false_met_counts_met_against_not_met_or_unknown():
    rows = [_row("A", 0, "met"), _row("A", 1, "met"), _row("A", 2, "met"), _row("A", 3, "unknown"),
            _row("A", 4, "met")]
    gold = {("A", 0): "met", ("A", 1): "not_met", ("A", 2): "unknown", ("A", 3): "not_met"}
    fm = live_eval.false_met(rows, gold)
    assert [(f.row.index, f.label) for f in fm] == [(1, "not_met"), (2, "unknown")]
    assert live_eval.false_met_rate(rows, gold) == pytest.approx(2 / 3)  # row 4 is unlabeled
    assert live_eval.agreement(rows, gold) == (1, 4)
    assert live_eval.false_met_rate([], gold) == 0.0


def test_cli_runs(capsys):
    assert live_eval.main([]) == 0
    out = capsys.readouterr().out
    assert "profile: new_gbm" in out and "profile: recurrent_astro" in out


# --- (a) histology passes on diagnosis alone; IDH wording not read --------------------


@pytest.mark.xfail(strict=False, reason=FIX_REASON)
@pytest.mark.parametrize("text", [
    "Histologically confirmed IDH1-mutant glioma",
    "Histologically confirmed glioma with IDH1 R132H mutation",
    "Histologically confirmed mutant IDH glioma",
])
def test_a_idh_mutant_wording_is_read_for_wildtype_patient(text, labels):
    assert _status(text, "inclusion", "new_gbm", labels) != "met"


@pytest.mark.xfail(strict=False, reason=FIX_REASON)
@pytest.mark.parametrize("tid,index", [
    ("NCT06410248", 0),  # "isocitrate dehydrogenase (IDH) wild type (WT)": IDH-mutant patient passed
    ("NCT07888647", 0),  # "newly diagnosed ... astrocytoma, IDH1R132H-positive": recurrent patient passed
])
def test_a_live_histology_bullets(tid, index, labels):
    b = _bullet(labels, tid, index)
    assert b["labels"]["recurrent_astro"] == "not_met"
    assert _status(b["text"], b["criterion_type"], "recurrent_astro", labels) != "met"


# --- (b) recurrent-GBM bullet decided by the treatment rule or by histology ---------------


@pytest.mark.xfail(strict=False, reason=FIX_REASON)
@pytest.mark.parametrize("tid,index,profile", [
    ("NCT07538128", 4, "new_gbm"),          # "glioblastoma, with first recurrence after prior surgery": treatment rule
    ("NCT07538128", 4, "recurrent_astro"),
    ("NCT07145047", 1, "new_gbm"),          # "Histologically confirmed recurrent glioblastoma": histology rule
    ("NCT07416188", 3, "new_gbm"),          # "recurrent glioblastoma, IDH-wildtype": IDH rule
    ("NCT06254326", 2, "recurrent_astro"),  # "(Patients who received prior radiation ...)": treatment rule
])
def test_b_recurrent_bullets(tid, index, profile, labels):
    b = _bullet(labels, tid, index)
    assert b["labels"][profile] == "not_met"
    assert _status(b["text"], b["criterion_type"], profile, labels) != "met"


@pytest.mark.xfail(strict=False, reason=FIX_REASON)
@pytest.mark.parametrize("text", [
    "Histologically confirmed recurrent glioblastoma",
    "Progressive glioblastoma after prior surgery or biopsy",
])
def test_b_recurrent_wording_for_newly_diagnosed_patient(text, labels):
    assert _status(text, "inclusion", "new_gbm", labels) != "met"


# --- (c) age written in text without a leading "age" ----------------------------------


@pytest.mark.xfail(strict=False, reason=FIX_REASON)
@pytest.mark.parametrize("profile", PROFILES)
def test_c_pediatric_age_in_parentheses(profile, labels):
    b = _bullet(labels, "NCT05503797", 12)  # "Pediatric patients (8-17 years of age) with a Grade 3 or 4 glioma"
    assert b["labels"][profile] == "not_met"
    assert _status(b["text"], b["criterion_type"], profile, labels) != "met"


@pytest.mark.xfail(strict=False, reason=FIX_REASON)
def test_c_age_range_reads_years_of_age(labels):
    facts = Facts(stage="post_pathology", bucket="glioma", age=54)
    c = Criterion("inclusion", "Patients (8-17 years of age)")
    f = evaluate(c, facts, get_policy())
    assert f.field == "age" and f.holds is False


# --- safety: zero false mets across every labeled bullet ----------------------------------


@pytest.mark.xfail(strict=False, reason=FIX_REASON)
@pytest.mark.parametrize("profile", PROFILES)
def test_no_false_met_on_live_criteria(profile, fixtures, labels):
    rows = live_eval.run_profile(profile, fixtures, labels)
    fm = live_eval.false_met(rows, live_eval.labels_for(labels, profile))
    assert fm == [], [(f.row.trial_id, f.row.index, f.label) for f in fm]
