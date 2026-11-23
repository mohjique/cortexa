# Live criteria fixtures

Eligibility criteria text from 25 real glioma trials, used to test the
eligibility checker on wording it was not written against (issue 48).

- **Source:** ClinicalTrials.gov API v2 (`https://clinicaltrials.gov/api/v2/studies`),
  public registry data, fetched 2026-11-23 (UTC; `fetched_at` in each file).
  Queries sent only condition and status terms (`query.cond` glioma,
  glioblastoma, oligodendroglioma, pediatric glioma; `query.term` IDH,
  H3 K27M, BRAF; `filter.overallStatus=RECRUITING,NOT_YET_RECRUITING`).
  No patient data, no ZIP code.
- **No PHI.** Registry text only. The two patient profiles in `labels.json`
  are synthetic.
- **Picked to cover:** IDH-mutant in varied wording (IDH1, IDH1/IDH2,
  R132H, "IDH mutated"), IDH-wildtype ("wild type (WT)", "absence of IDH
  mutation"), recurrent/progressive-only, newly diagnosed, pediatric and age
  ranges written in text ("8-17 years of age"), 1p/19q, MGMT, grade (Arabic and
  Roman numerals), H3 K27M and BRAF. None of the fetched trials restricts sex
  in its registry `sex` field; sex wording in the text is pregnancy and
  contraception language, which the concept tests use as false positives.

## Files

- `NCT*.json`: `trial_id`, `title`, `conditions`, `criteria_text`,
  `minimum_age`, `maximum_age`, `sex` (from `eligibilityModule`),
  `overall_status`, `fetched_at`.
- `labels.json`: hand labels for every bullet that
  `agent.eligibility.split_criteria` produces, keyed by trial ID and bullet
  index, with the bullet text stored for alignment. Two profiles, stored as
  `DoctorResults` dicts: `new_gbm` (54, new IDH-wildtype glioblastoma grade 4,
  MGMT unmethylated, 1p/19q intact, biopsy only) and `recurrent_astro`
  (41, first recurrence of IDH-mutant astrocytoma grade 3, 1p/19q intact,
  prior surgery, radiation and temozolomide). Labeling conventions are listed
  in the file. Labels are conservative: anything the profile's fields cannot
  decide is `unknown`.

## Use

    python -m agent.live_eval        # false-met count and agreement per profile
    python -m pytest agent/tests/test_live_criteria.py agent/tests/test_concepts.py

If the bullet splitter changes, the label alignment test fails; the labels
must then be re-keyed to the new bullets, checking each label again.
Refreshing the registry text needs the same re-check, so these files are not
refreshed automatically.
