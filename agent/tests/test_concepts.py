"""Concept detectors (issue 48 step 4): every concept, the false positives that
matter, and a run over all live criteria fixtures."""

from __future__ import annotations

import dataclasses
from typing import get_args

import pytest

from agent import concepts, live_eval
from agent.concepts import ConceptHit, ConceptName, detect, mentioned
from agent.eligibility import split_criteria


def hit(text: str, name: str) -> ConceptHit | None:
    return next((h for h in detect(text) if h.name == name), None)


def value(text: str, name: str) -> str | None:
    h = hit(text, name)
    assert h is not None, f"{name} not detected in {text!r}"
    return h.value


# --- interface ---------------------------------------------------------------------


def test_interface():
    assert set(get_args(ConceptName)) == {
        "recurrent", "newly_diagnosed", "idh", "codeletion_1p19q", "mgmt",
        "grade", "age_range", "pediatric", "sex", "h3k27m", "braf"}
    assert [f.name for f in dataclasses.fields(ConceptHit)] == ["name", "span", "value"]
    h = ConceptHit("idh", "IDH1", "mutant")
    with pytest.raises(dataclasses.FrozenInstanceError):
        h.value = None  # type: ignore[misc]


def test_one_hit_per_concept_first_span_and_order():
    text = "Recurrent IDH-wildtype glioblastoma; prior recurrence allowed; IDH status by IHC"
    hits = detect(text)
    assert [h.name for h in hits] == ["recurrent", "idh"]
    assert hits[0].span == "Recurrent" and hits[1].span == "IDH"
    assert detect(text) == hits  # deterministic
    assert mentioned(text) == {"recurrent", "idh"}
    assert detect("") == [] and mentioned("Adequate bone marrow function") == set()


def test_span_is_text_from_the_criterion():
    text = "Histologically confirmed WHO grade 3 to 4 astrocytoma, IDH1R132H-positive"
    for h in detect(text):
        assert h.span and h.span in text


# --- IDH ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,want", [
    ("Histologically confirmed IDH1-mutant glioma", "mutant"),
    ("IDH2 mutated glioma", "mutant"),
    ("IDH1/2-mutant glioma", "mutant"),
    ("documented IDH1 and/or IDH2 gene-mutation", "mutant"),
    ("glioma with IDH1 R132H mutation", "mutant"),
    ("astrocytoma, IDH1R132H-positive", "mutant"),
    ("mutant IDH1 glioma", "mutant"),
    ("IDH-mut astrocytoma", "mutant"),
    ("IDH wild-type glioblastoma", "wildtype"),
    ("Glioblastoma, IDH-wildtype", "wildtype"),
    ("IDH wildtype GBM", "wildtype"),
    ("GBM (IDH-wt)", "wildtype"),
    ("isocitrate dehydrogenase (IDH) wild type (WT)", "wildtype"),
    ("a glioma that is IDH1 R132H-wildtype", "wildtype"),
    ("absence of IDH mutation demonstrated by negative IDH1 R132H staining", "wildtype"),
    ("regardless of IDH status", None),
    ("IDH-mutant or wildtype glioma", None),
    ("both IDH-Mutant and IDH-Wild Type", None),
    ("IDH mutation status will not limit candidacy but needs to be known", None),
    ("prior therapies targeting IDH1 or IDH2", None),
])
def test_idh(text, want):
    assert value(text, "idh") == want


def test_idh_form_of_another_marker_is_not_borrowed():
    assert value("IDH-wildtype and H3 K27M-mutant glioma", "idh") == "wildtype"


# --- 1p/19q and MGMT -----------------------------------------------------------------


@pytest.mark.parametrize("text,want", [
    ("1p/19q co-deleted oligodendroglioma", "codeleted"),
    ("1p/19q codeletion", "codeleted"),
    ("IDH-mutant and 1p/19q-codeleted", "codeleted"),
    ("1p19q codeleted", "codeleted"),
    ("1p/19q non-codeleted", "intact"),
    ("1p/19q intact", "intact"),
    ("Absence of chromosomal 1p/19q co-deletion", "intact"),
    ("without 1p/19q codeletion", "intact"),
    ("confirmed 1p19q codeletion status", None),
    ("1p/19q codeleted or intact", None),
])
def test_codeletion(text, want):
    assert value(text, "codeletion_1p19q") == want


@pytest.mark.parametrize("text,want", [
    ("MGMT promoter methylated", "methylated"),
    ("MGMT promoter hypermethylated", "methylated"),
    ("MGMT-promotor methylated patients", "methylated"),
    ("MGMT promoter unmethylated", "unmethylated"),
    ("Unmethylated MGMT promoter", "unmethylated"),
    ("MGMT promoter methylation-negative (MGMT-unmethylated) status", "unmethylated"),
    ("MGMT promoter methylation status will not limit candidacy", None),
    ("MGMT methylated or unmethylated", None),
    ("regardless of MGMT status", None),
])
def test_mgmt(text, want):
    assert value(text, "mgmt") == want


# --- grade -------------------------------------------------------------------------


@pytest.mark.parametrize("text,want", [
    ("WHO grade 2-4 glioma", "2-4"),
    ("CNS WHO grade 4", "4-4"),
    ("WHO Grade 3 or 4 glioma", "3-4"),
    ("grade 3 to 4 astrocytoma", "3-4"),
    ("grade II or III glioma", "2-3"),
    ("grade IV glioblastoma", "4-4"),
    ("WHO grade III-IV", "3-4"),
    ("IDH wild type WHO IV glioblastoma", "4-4"),
    ("high-grade glioma", None),
    ("low grade glioma", None),
    ("Grade 2 glioma or Grade 4 glioblastoma", None),  # two separate grades: not merged
])
def test_grade(text, want):
    assert value(text, "grade") == want


@pytest.mark.parametrize("text", [
    "All adverse events related to prior therapies must have resolved to Grade 1 or baseline",
    "Grade ≥2 changes in AST, ALT, or bilirubin attributed to prior checkpoint inhibitor",
    "Participants with residual Grade 1 toxicity due to prior chemotherapy are allowed",
    "Hypertension Grade 2 or higher",
])
def test_toxicity_grades_are_not_tumor_grade(text):
    assert hit(text, "grade") is None


# --- age ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,want", [
    ("Pediatric patients (8-17 years of age)", "8-17"),
    ("aged 18 to 70", "18-70"),
    ("Patients 18 to 70 years of age", "18-70"),
    ("≥ 18 years", "18-"),
    ("18 years or older", "18-"),
    ("Age: 18 years or older", "18-"),
    ("between 3 and 21 years", "3-21"),
    ("Males and females between the ages of 3 and 30 years old", "3-30"),
    ("Age ≥ 2 and ≤ 16 years", "2-16"),
    ("≥ 18 and ≤ 80 years of age", "18-80"),
    ("Age \\>=18 years, \\<=70 years.", "18-70"),
    ("≥18 years old ≤ 70 years old", "18-70"),
    ("Adults (≥18 years) with Grade 1-4 glioma", "18-"),
    ("Participants \\>= 18 years of age", "18-"),
    ("Patients age 18 or under", "-18"),
    ("Male or female \\< 18 years of age", "-17"),   # strict bound tightened
    ("Age \\> 18 years, male or female.", "19-"),
    ("Age ≥ 12 months", "1-"),
    ("Dose escalation: 2-21 years of age; Dose expansion: 2 years of age and above", None),
])
def test_age_range(text, want):
    assert value(text, "age_range") == want


@pytest.mark.parametrize("text", [
    "Prior invasive malignancy unless disease free for ≥ 3 years",
    "Expected survival ≥ 6 months",
    "At least 3 weeks or 5 half-lives after treatment",
    "Creatinine clearance (140 - Age) × (weight in kg) × (0.85 if female)/72",
    "Potential POCBP who may be menopausal and are \\< 55 years of age must have a serum FSH level",
    "Participants of childbearing age must use contraception",
])
def test_not_an_age_limit(text):
    assert hit(text, "age_range") is None


# --- recurrent / newly diagnosed ---------------------------------------------------


@pytest.mark.parametrize("text", [
    "recurrent glioblastoma", "Recurrent or progressive glioblastoma", "evidence of progression per RANO",
    "relapsed LGG", "refractory high-grade glioma", "disease that has progressed after first-line therapy",
    "with first recurrence after prior surgery",
])
def test_recurrent(text):
    assert hit(text, "recurrent") is not None


@pytest.mark.parametrize("text", [
    "Treatment continues until disease progression or unacceptable toxicity",
    "time to progression will be measured",
    "carries a minimal risk of recurrence",
    "history of recurrent seizures",
    "progressive multifocal leukoencephalopathy",
    "pseudoprogression",
])
def test_recurrent_false_positives(text):
    assert hit(text, "recurrent") is None


@pytest.mark.parametrize("text", ["Newly diagnosed glioblastoma", "newly-diagnosed GBM",
                                  "H3 K27M-mutant diffuse glioma (new diagnosis)"])
def test_newly_diagnosed(text):
    assert hit(text, "newly_diagnosed") is not None


# --- pediatric ---------------------------------------------------------------------


@pytest.mark.parametrize("text", ["pediatric high-grade glioma", "paediatric patients", "Children with DIPG",
                                  "a child aged 3", "adolescents and young adults"])
def test_pediatric(text):
    assert hit(text, "pediatric") is not None


@pytest.mark.parametrize("text", [
    "women of child-bearing potential", "patients of childbearing potential", "Children's Oncology Group",
    "Diffuse pediatric-type high-grade glioma", "Male subjects able to father children must use contraception",
])
def test_pediatric_false_positives(text):
    assert hit(text, "pediatric") is None


# --- sex ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,want", [
    ("female patients only", "female"),
    ("Women with recurrent glioma", "female"),
    ("Male patients with glioblastoma", "male"),
    ("Men aged 50 or older", "male"),
    ("Male or female, aged 18 years and above", None),
    ("Male / female over 18 years of age", None),
    ("of any ethnic origin and gender", None),
    ("Aged ≥18 years, regardless of gender", None),
])
def test_sex(text, want):
    assert value(text, "sex") == want


@pytest.mark.parametrize("text", [
    "Women of childbearing potential must have a negative pregnancy test",
    "Men who are sexually active must agree to use a condom",
    "Female patients must be either documented not to be WOCBP or have a negative pregnancy test",
    "Pregnant or lactating women",
    "Male patients must be surgically sterile or willing to use contraception",
])
def test_sex_false_positives(text):
    assert hit(text, "sex") is None


# --- H3 K27M and BRAF --------------------------------------------------------------


@pytest.mark.parametrize("text", ["H3 K27M-mutant diffuse glioma", "H3K27M mutant DMG",
                                  "Diffuse midline glioma, H3 K27-altered", "H3K27-altered DMG", "K27M mutation"])
def test_h3k27m(text):
    assert hit(text, "h3k27m") is not None


def test_h3_without_k27_is_not_h3k27m():
    assert hit("Diffuse pediatric-type high-grade glioma, H3-wildtype", "h3k27m") is None
    assert hit("Diffuse hemispheric glioma, H3 G34-mutant", "h3k27m") is None


@pytest.mark.parametrize("text,span", [("BRAF V600E mutation", "BRAF V600E"), ("activating BRAF alteration", "BRAF"),
                                       ("KIAA1549-BRAF fusion", "KIAA1549-BRAF"), ("BRAFV600 mutation", "BRAFV600")])
def test_braf(text, span):
    assert hit(text, "braf").span == span


# --- live fixtures -----------------------------------------------------------------


def test_detect_runs_over_every_live_bullet():
    fixtures = live_eval.load_fixtures()
    assert fixtures
    seen: set[str] = set()
    for f in fixtures.values():
        for c in split_criteria(f["criteria_text"]):
            hits = detect(c.text)
            assert len({h.name for h in hits}) == len(hits)
            for h in hits:
                assert h.name in concepts.CONCEPTS and h.span in c.text
            seen |= {h.name for h in hits}
    # The fixtures were picked to exercise these concepts.
    assert {"recurrent", "newly_diagnosed", "idh", "codeletion_1p19q", "mgmt", "grade",
            "age_range", "pediatric", "h3k27m", "braf"} <= seen


def test_live_examples():
    labels = live_eval.load_labels()["trials"]
    assert value(labels["NCT05503797"][12]["text"], "age_range") == "8-17"
    assert value(labels["NCT06410248"][0]["text"], "idh") == "wildtype"
    assert value(labels["NCT07888647"][0]["text"], "idh") == "mutant"
    assert hit(labels["NCT07145047"][1]["text"], "recurrent") is not None
    assert value(labels["NCT06254326"][2]["text"], "codeletion_1p19q") == "codeleted"
    assert value(labels["NCT07476794"][2]["text"], "mgmt") == "unmethylated"
