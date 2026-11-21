# Patient profiles guide: 23 synthetic demo cases

**Goal:** write 23 fake patient profiles that walk the app through every branch of the decision tree (`docs/decision-tree.md`), plus hand-labeled eligibility answers we can test the agent against.
**Where:** one JSON file per patient in `clinical/patients/`, named `demo-001.json` to `demo-023.json`. Copy `clinical/patients/_template.json` to start.

Read first: `docs/decision-tree.md` (the app logic) and the trials table in `agent/demo_cache/README.md` (the 9 synthetic trials the demo runs against).

## Ground rules

1. **Everything is fake.** No real patients, names, addresses or dates of birth. IDs are `demo-001` to `demo-023`.
2. **ZIP codes only for location.** Use ZIPs that are in the demo cache so the map works offline: `77840` College Station (default), `77030` Houston, `78712` Austin, `75390` Dallas, `78229` San Antonio, `70112` New Orleans, `73104` Oklahoma City. Any other ZIP needs an entry in `agent/demo_cache/nominatim/zip_lookup.json`.
3. **Say "suspected" before pathology.** Radiology reports describe findings ("enhancing mass, suspicious for high-grade glioma"). Only pathology fields hold an actual diagnosis.
4. **`pending` vs `unknown` (from `docs/interfaces.md`):** `pending` = test ordered, result not back. `unknown` = not tested or not available. Pre-op cases should use `pending`. This is how we show the app saying "unknown, needs pathology" instead of guessing.
5. **Each case uses one held-out test-set image.** The profile's tumor type must match the image's true label.

## Fields

★ = used by at least one synthetic trial's eligibility criteria. Fill these carefully. Other fields add realism and future-proofing; fill them quickly.

### 1. Case basics

| Field | Allowed values | Notes |
|---|---|---|
| `case_id` | `demo-001` … `demo-023` | |
| `scenario` | short text | Which branch this case tests (see case list) |
| `image_file` | filename | Held-out test-set image |
| `kaggle_label` | `glioma`, `meningioma`, `pituitary`, `no_tumor` | True image label |
| `age` ★ | integer, 18+ | One trial caps age at 75 |
| `sex` | `female`, `male` | |
| `zip` ★ | one of the cached ZIPs above | Drives distance to trial sites |
| `max_travel_miles` | integer | Patient's travel limit |

### 2. Presentation (before biopsy)

| Field | Allowed values | Notes |
|---|---|---|
| `presenting_symptoms` | list of text | e.g. `["new-onset seizure", "headache"]` |
| `radiology_report` | text, 3 to 6 sentences | Findings + impression, "suspicious for", no diagnosis |
| `tumor_location` | text | e.g. "right frontal lobe" |
| `tumor_size_mm` | `[length, width]` | |
| `multifocal` | true / false | |

### 3. Health status

| Field | Allowed values | Notes |
|---|---|---|
| `kps` | 0–100, steps of 10 | Karnofsky Performance Status. Not in the synthetic trials yet, but in almost every real one |
| `labs` | object | `anc` (×10⁹/L), `platelets` (×10⁹/L), `hemoglobin` (g/dL), `creatinine`, `bilirubin` (mg/dL), `ast`, `alt` (U/L) |
| `steroid_dose_mg` | number | Daily dexamethasone |
| `current_meds` | list of text | |
| `comorbidities` ★ | list of text | Trials exclude `neurofibromatosis type 2` and `active autoimmune disease`. Include them when a case should fail on them |
| `prior_cancers` | list of text | |
| `can_use_phone_or_tablet` ★ | true / false | Remote trial requirement |

### 4. Pathology (after biopsy or surgery)

| Field | Applies to | Allowed values |
|---|---|---|
| `diagnosis` ★ | all | WHO 2021 wording, e.g. "Glioblastoma, IDH-wildtype". `null` before pathology |
| `grade` ★ | glioma, meningioma | 1–4, `pending`, `unknown` |
| `idh` ★ | glioma | `mutant`, `wildtype`, `pending`, `unknown` |
| `codeletion_1p19q` ★ | glioma | `codeleted`, `intact`, `pending`, `unknown` |
| `mgmt` ★ | glioma | `methylated`, `unmethylated`, `pending`, `unknown` |
| `other_markers` | glioma | optional: `h3k27m`, `egfr_amp`, `cdkn2ab_deletion`, `braf_v600e`, `tert_promoter` |
| `tmb_clia` ★ | recurrent glioma | Tumor mutational burden in mutations/Mb **from a CLIA-certified lab test**, or `null`. Separate from our mutation check (see the safety note below) |
| `pituitary` ★ | pituitary | Object matching `docs/interfaces.md`: `treatment_path` (`surgery` / `medication_only`), `secreting_hormone` (`prolactin` / `growth_hormone` / `acth` / `tsh` / `none` / `pending` / `unknown`), `size` (`micro` / `macro` / `unknown`), `cavernous_sinus_invasion` (`yes` / `no` / `unknown`), plus `hormone_levels` (object) and `surgical_candidate` (true / false) |
| `pathology_report` | after biopsy | text, 3 to 6 sentences |

### 5. Treatment history

| Field | Allowed values | Notes |
|---|---|---|
| `recurrent` ★ | true / false | `false` = newly diagnosed |
| `recurrence_number` ★ | 0, 1, 2, 3… | One trial allows only first or second recurrence |
| `prior_treatments` ★ | list of objects | e.g. `{"type": "radiation", "detail": "60 Gy / 30 fractions", "weeks_ago": 30}`. Types: `surgery` (add `extent`: `gross_total`, `subtotal`, `biopsy_only`), `radiation`, `temozolomide` (add `cycles`), `bevacizumab`, `idh_inhibitor`, `checkpoint_inhibitor`, `pituitary_radiation`, `other`. Trials check most of these |
| `weeks_since_radiation` | integer or null | |

### 6. Sequencing (recurrent gliomas only)

| Field | Allowed values | Notes |
|---|---|---|
| `broad_sequencing` | `none`, `panel`, `exome`, `genome` | The mutation check runs only for `exome` or `genome` |
| `mutation_counts_96` | 96 integers or null | Simulated 96-channel counts for recurrent gliomas with exome/genome sequencing; null otherwise |
| `true_sbs11_fraction` | number or null | Answer key for the simulated counts. The app never reads it |

**Safety note:** trial `NCT99000009` requires high TMB from a CLIA-certified assay. Our mutation check is research context, not a CLIA test, so it must never satisfy that criterion. If `tmb_clia` is `null`, the correct label is `unknown`, even when the mutation check says "detected." The shortlist then shows 009 under **Needs a test: CLIA TMB result**, never as a match.

### 7. Demo script and answer key

| Field | Notes |
|---|---|
| `doctor_decisions` | Scripted gate decisions, keys matching `docs/interfaces.md`: `confirm_bucket` `{"bucket": "...", "overrode_model": false}`, `watch_or_treat` `{"choice": "watch", "rescan_in_months": 6}`, `preop_review` (every glioma case) `{"tests_reviewed": true, "pre_surgery_trials": [{"trial_id": "NCT99000003", "action": "pursue_before_surgery", "reason": "..."}]}` with one entry per `expected_preop_flags` trial (empty list if none). The screen has no surgical history, so it flags NCT99000003 for every suspected glioma; cases with prior surgery decline it as `not_pursuing` with a reason (required for `not_pursuing`). Leave out `screen_id`: it is a hash of the live screen, and the replay test fills it in. `surgery_or_meds` `{"choice": "surgery"}` |
| `expected_path` | Branches this case should follow, e.g. `["glioma", "preop_screen", "awaiting_pathology", "enter_results", "matching", "review_shortlist"]` (use the `stage` names from `docs/interfaces.md`) |
| `expected_trials` | Trial IDs you expect in the shortlist's **eligible** group (eligible on what we know; any unknowns are routine screening-visit items). There is no distance cutoff, so include every eligible trial however far away; the app sorts them nearest first |
| `expected_needs_test` | Trials you expect in the **needs a test** group, each mapped to the key fields left unknown, e.g. `{"NCT99000009": ["tmb_clia"]}`. Key fields are `trial_search.key_unknown_fields` in `policies/policy.yaml`. `{}` if none |
| `expected_no_match` | true if no trial is eligible anywhere, so the case ends in the no-match plan |
| `gold_eligibility` | Your hand labels (see below) |

## The 23 cases

"Target trials" are what the case is designed to exercise in the synthetic cache. Adjust details if something isn't clinically realistic. If you change a case, update its `expected_*` fields.

| ID | Image | Scenario | Key details | Target trials |
|---|---|---|---|---|
| demo-001 | no_tumor | No tumor | Headache workup, normal MRI. App stops | none |
| demo-002 | no_tumor | No tumor | Different age/ZIP | none |
| demo-003 | no_tumor | No tumor | Different age/ZIP | none |
| demo-004 | meningioma | Meningioma, watched | Small, incidental. `watch_or_treat: watch` | none |
| demo-005 | meningioma | Meningioma, watched | Small, older patient | none |
| demo-006 | meningioma | Meningioma, treated | WHO grade 1 after gross total resection | 005 `not_met` (grade). No match |
| demo-007 | meningioma | Meningioma, treated | Grade 2, subtotal resection, ZIP 77840 | 005 is 155 mi away: still a match, listed with its distance |
| demo-008 | meningioma | Meningioma, treated | Grade 3, gross total, ZIP 78229 (San Antonio) | 005 nearby: match |
| demo-009 | glioma | Glioma pre-op | Markers `pending`, no prior surgery, ZIP 77840 | 003 pre-surgery flag (Austin, opens soon) |
| demo-010 | glioma | Glioma pre-op | Markers `pending`, age 78 | 003 flag; later 004 would fail on age |
| demo-011 | glioma | Glioma pre-op | Some markers `pending`, younger | 003 flag |
| demo-012 | glioma | New GBM | IDH-wildtype, grade 4, MGMT **methylated** | 004 `not_met` on MGMT only: near miss |
| demo-013 | glioma | New GBM | IDH-wildtype, grade 4, MGMT **unmethylated**, age ≤ 75, no prior RT/TMZ | 004 match |
| demo-014 | glioma | New astrocytoma | IDH-mutant, grade 2 or 3, 1p/19q intact, no prior RT/chemo | 001 match |
| demo-015 | glioma | New oligodendroglioma | IDH-mutant, 1p/19q codeleted | 001 `not_met` (codeletion). 007 in New Orleans (376 mi): match |
| demo-016 | glioma | Recurrent GBM, exome | 1st recurrence after RT + temozolomide, no bevacizumab, `tmb_clia: null`. Mutation check: **detected** | 002 match. Remote 008: match. 009 must stay `unknown`: needs a test (CLIA TMB) |
| demo-017 | glioma | Recurrent GBM, exome | 1st or 2nd recurrence, few mutations. Mutation check: **inconclusive** | 002 match. Remote 008: match. 009: needs a test (CLIA TMB) |
| demo-018 | glioma | Recurrent IDH-mutant astrocytoma, exome | Little or no prior temozolomide. Mutation check: **not detected** | 002 `not_met` (IDH). Remote 008: match. 009: needs a test (CLIA TMB) |
| demo-019 | glioma | Recurrent GBM, no sequencing | 3rd recurrence, prior bevacizumab. Mutation check skipped | 002 `not_met`. Remote 008: match. 009: needs a test (CLIA TMB) |
| demo-020 | pituitary | Pituitary, surgery | Non-functioning macroadenoma. `surgery_or_meds: surgery` | none |
| demo-021 | pituitary | Pituitary, surgery | Growth-hormone-secreting | none |
| demo-022 | pituitary | Pituitary, meds only | **ACTH-secreting (Cushing disease), not a surgical candidate.** `surgery_or_meds: medication_only`. Please confirm this is realistic | 006 (Houston, opens soon) |
| demo-023 | glioma | Doctor override | Uses an image the model gets wrong or is unsure about. Doctor corrects the bucket | depends on profile |

## Hand-labeled answers (gold eligibility)

This is how we prove the agent works. Label against the **synthetic trials** (criteria text is in `agent/demo_cache/ctgov/studies_brain_tumor.json`, summarized in `agent/demo_cache/README.md`).

For **at least 10 cases** (prioritize demo-007, 009, 012, 013, 014, 015, 016, 018, 019, 022), label every criterion of each target trial:

```json
{
  "trial_id": "NCT99000004",
  "criterion_type": "inclusion",
  "criterion_text": "MGMT promoter unmethylated",
  "status": "not_met",
  "evidence_field": "mgmt",
  "note": "MGMT is methylated"
}
```

`status` always answers **"does the patient pass this criterion?"**: `met` (passes), `not_met` (fails) or `unknown` (profile lacks the information). This matches the shortlist rule in `docs/interfaces.md` (a trial is shortlisted only if no criterion is `not_met`).

For **exclusion** criteria, passing means the exclusion does *not* apply. "Prior bevacizumab" is `met` if the patient never had it and `not_met` if they did. Always fill `criterion_type` (`inclusion` / `exclusion`) so nobody misreads it.

Use `unknown` whenever the profile lacks the information, exactly as the app should.

We'll compare the agent's labels with yours and report the agreement rate to the judges.

Optional, if time allows: label 1 or 2 real recruiting trials from ClinicalTrials.gov for demo-013 or demo-016, to show the agent on real criteria text.

## Also yours: `policies/policy.yaml`

Review the medical settings and fix anything unrealistic:
- `classifier.min_confidence_for_auto_suggest` (0.80)
- `trial_search.statuses` (there is no distance cutoff: every eligible trial is shown, nearest first)
- The mutation-check rule that blocks immunotherapy routing on mutation count alone

## Checklist

- [ ] 23 test-set images matched to cases
- [ ] 23 JSON files in `clinical/patients/`
- [ ] Radiology report for every case; pathology report for every case past biopsy
- [ ] Pre-op cases use `pending` markers
- [x] `mutation_counts_96` filled for demo-016, 017, 018
- [x] Gold eligibility labels for at least 10 cases (16 so far)
- [x] `policy.yaml` reviewed
- [ ] No real names, addresses or dates of birth anywhere
