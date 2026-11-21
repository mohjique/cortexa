# Interfaces between components

Draft. Agree on these at kickoff, then change them only after telling the team.

All examples use synthetic data. Trial IDs `NCT990000xx` are fake (see `agent/demo_cache/`).

## Case state

The agent owns this. The frontend reads it to know which screen to show.

```json
{
  "case_id": "demo-001",
  "stage": "intake | confirm_bucket | watch_or_treat | preop_screen | hormone_labs | surgery_or_meds | awaiting_pathology | enter_results | mutation_check | matching | review_shortlist | no_match | rescan | stopped",
  "suspected_bucket": "glioma",
  "confirmed_bucket": "glioma | meningioma | pituitary | no_tumor | null",
  "treatment_path": "surgery | medication_only | watch | null",
  "waiting_on": "doctor | agent | pathology",
  "updated_at": "2026-11-21T14:03:12Z"
}
```

The case moves forward only after the gate for its stage has a recorded doctor decision.

## Classifier → agent

```json
{
  "case_id": "demo-001",
  "suspected_bucket": "glioma | meningioma | pituitary | no_tumor",
  "probabilities": {"glioma": 0.91, "meningioma": 0.05, "pituitary": 0.02, "no_tumor": 0.02},
  "medgemma_bucket": "glioma",
  "confidence": 0.88,
  "flagged_uncertain": false,
  "heatmap_path": "artifacts/demo-001_gradcam.png"
}
```

## Doctor decision (approval gates) → agent

One record per gate. Each one is also written to the audit log.

```json
{
  "case_id": "demo-001",
  "gate": "confirm_bucket | watch_or_treat | hormone_labs | surgery_or_meds | enter_results | review_shortlist",
  "decision": {},
  "doctor_id": "dr-synthetic-01",
  "ts": "2026-11-21T14:05:40Z",
  "note": "optional free text"
}
```

`decision` by gate:

| gate | decision |
|---|---|
| `confirm_bucket` | `{"bucket": "glioma", "overrode_model": false}` |
| `watch_or_treat` (meningioma) | `{"choice": "watch \| treat", "rescan_in_months": 6}` (`rescan_in_months` only for watch) |
| `hormone_labs` (pituitary) | `{"labs_reviewed": true}` (doctor has reviewed hormone labs and endocrinology) |
| `surgery_or_meds` (pituitary) | `{"choice": "surgery \| medication_only"}` |
| `enter_results` | the Doctor-entered results object below |
| `review_shortlist` | `{"keep": ["NCT99000001"], "reject": [{"trial_id": "NCT99000004", "reason": "patient preference"}]}` |

## Doctor-entered results → agent

```json
{
  "case_id": "demo-001",
  "confirmed_bucket": "glioma",
  "diagnosis": "Glioblastoma, IDH-wildtype",
  "grade": "1-4 | pending | unknown | null",
  "idh": "mutant | wildtype | pending | unknown",
  "codeletion_1p19q": "codeleted | intact | pending | unknown",
  "mgmt": "methylated | unmethylated | pending | unknown",
  "tmb_clia": null,
  "recurrent": true,
  "recurrence_number": 1,
  "prior_treatments": [
    {"type": "surgery", "extent": "gross_total", "weeks_ago": 40},
    {"type": "radiation", "detail": "60 Gy / 30 fractions", "weeks_ago": 34},
    {"type": "temozolomide", "cycles": 6, "weeks_ago": 8}
  ],
  "comorbidities": ["hypertension"],
  "can_use_phone_or_tablet": true,
  "age": 54,
  "zip": "77840",
  "broad_sequencing_available": false,
  "sequencing": "exome | genome | panel | none | null",
  "pituitary": null
}
```

`sequencing` is optional. When set, it must agree with `broad_sequencing_available` (`exome` or `genome` means true). The mutation check needs it to tell exome/genome from a panel.

Field names follow `clinical/PATIENT_PROFILES_GUIDE.md`:

- `grade`: 1 to 4, `pending` or `unknown` for glioma and meningioma; `null` when it does not apply (pituitary).
- `tmb_clia`: tumor mutational burden (mut/Mb) from a CLIA-certified lab test, or `null`. Never filled from the mutation check.
- `recurrence_number`: 0 when `recurrent` is false, 1 or more when true.
- `prior_treatments[].type`: `surgery`, `radiation`, `temozolomide`, `bevacizumab`, `idh_inhibitor`, `checkpoint_inhibitor`, `pituitary_radiation`, `other`. Optional `detail` and `weeks_ago`. `extent` (`gross_total`, `subtotal`, `biopsy_only`) only for surgery; `cycles` only for temozolomide.
- `comorbidities`, `can_use_phone_or_tablet`, `tmb_clia` and `recurrence_number` are optional. `null` means not recorded, and the checker marks dependent criteria `unknown`. An empty `comorbidities` list means "none".

For pituitary cases, `pituitary` holds hormone and imaging results. For a medication-only case, the doctor enters these directly with no pathology. Glioma marker fields are then `unknown`.

```json
"pituitary": {
  "treatment_path": "surgery | medication_only",
  "secreting_hormone": "prolactin | growth_hormone | acth | tsh | none | pending | unknown",
  "size": "micro | macro | unknown",
  "cavernous_sinus_invasion": "yes | no | unknown",
  "surgical_candidate": "true | false | null",
  "hormone_levels": {"late_night_salivary_cortisol_ug_dl": 0.42, "acth_pg_ml": 68}
}
```

`surgical_candidate` and `hormone_levels` are optional. `hormone_levels` keys are `<hormone>_<unit>`.

`unknown` means not tested or not available. `pending` means ordered, result not back. The eligibility checker treats both as `unknown` but gives a different reason.

## Pre-op screen (glioma) → frontend

Runs after the bucket is confirmed as glioma and before surgery. It lists what the biopsy must answer and how many trials each answer decides.

```json
{
  "case_id": "demo-001",
  "trials_considered": 7,
  "tests_needed": [
    {
      "field": "idh",
      "label": "IDH mutation status",
      "current_value": "pending",
      "decides_trials": 3,
      "trial_ids": ["NCT99000001", "NCT99000004", "NCT99000007"]
    },
    {"field": "codeletion_1p19q", "label": "1p/19q codeletion", "current_value": "pending", "decides_trials": 1, "trial_ids": ["NCT99000007"]},
    {"field": "mgmt", "label": "MGMT promoter methylation", "current_value": "pending", "decides_trials": 1, "trial_ids": ["NCT99000004"]},
    {"field": "grade", "label": "WHO grade", "current_value": "pending", "decides_trials": 2, "trial_ids": ["NCT99000001", "NCT99000004"]}
  ],
  "pre_surgery_trials": [
    {
      "trial_id": "NCT99000003",
      "title": "[SYNTHETIC] Window-of-opportunity drug before resection in suspected glioblastoma",
      "recruitment_status": "NOT_YET_RECRUITING",
      "criterion_text": "Must enroll before planned surgical resection",
      "nearest_site": {"city": "Austin", "state": "TX", "distance_miles": 86.7, "drive_minutes": 110}
    }
  ],
  "label": "Suspected glioma. Biopsy results pending."
}
```

`decides_trials` counts trials where the result changes the criterion between `met` and `not_met`.

## Mutation check input (agent → mutation check)

Sent only for recurrent glioma with exome or genome sequencing.

```json
{
  "case_id": "demo-002",
  "sequencing": "exome | genome | panel",
  "reference": "GRCh38",
  "snvs": [
    {"chrom": "chr7", "pos": 55191822, "ref": "C", "alt": "T", "context": "ACG"}
  ]
}
```

`context` is the trinucleotide on the reference strand with `ref` in the middle. If `sequencing` is `panel`, the check does not run.

Instead of `snvs`, the input may carry `counts_96`: 96 non-negative integers in COSMIC SBS96 row order (5' base, then substitution, then 3' base). Send exactly one of the two, and leave the other key out rather than sending it as `null`. Real use sends `snvs`. The demo patients store `mutation_counts_96` and send it as `counts_96`, so no fake variant calls are invented.

## Mutation check → agent

```json
{
  "case_id": "demo-002",
  "ran": true,
  "total_mutations": 1340,
  "sbs11_call": "detected | not_detected | inconclusive",
  "false_alarm_rate": 0.01,
  "exposures": {"SBS1": 0.12, "SBS5": 0.18, "SBS11": 0.70},
  "note": "Context only. Does not decide eligibility.",
  "sbs11_fraction_ci95": [0.58, 0.79],
  "detection_limit_95": 0.12,
  "critical_fraction": 0.06,
  "summary": "SBS11 was detected at a 1% false-alarm rate: ..."
}
```

`total_mutations` counts SNVs in a research catalog. It is not TMB, never fills `tmb_clia`, and never satisfies a TMB criterion.

The last four fields are optional and default to `null`. They tell a doctor how far to trust the call, because `not_detected` alone can mean "no SBS11" or "too few mutations to see it":

| Field | Meaning |
|---|---|
| `sbs11_fraction_ci95` | 95% interval `[low, high]` on the fraction of mutations from SBS11 |
| `detection_limit_95` | SBS11 fraction that would be detected 95% of the time at this mutation count. `null` if not established: 95% is never reached even at 100% SBS11, or the mutation count is below the calibrated range |
| `critical_fraction` | SBS11 fraction detected 50% of the time. Above 0.5 the call is `inconclusive`, never `not_detected` |
| `summary` | One plain-English sentence from a fixed template. Every number comes from the fit, so it can be shown as is |

The schema checks that `low <= high` and `critical_fraction <= detection_limit_95`.

If it did not run: `{"case_id": "...", "ran": false, "skip_reason": "no_broad_sequencing | not_recurrent_glioma"}`.

## Trial record (agent internal, normalized from both APIs)

```json
{
  "trial_id": "NCT99000001",
  "title": "[SYNTHETIC] ...",
  "source": "ctgov | nci | ctgov+nci",
  "recruitment_status": "RECRUITING | NOT_YET_RECRUITING",
  "phases": ["PHASE2"],
  "conditions": ["Astrocytoma, IDH-mutant"],
  "criteria_text": "Inclusion Criteria: ...",
  "min_age_years": 18,
  "max_age_years": null,
  "remote_participation": false,
  "sites": [
    {"facility": "Synthetic Neuro-Oncology Center A", "city": "Houston", "state": "TX", "zip": "77030", "lat": 29.7079, "lon": -95.4012}
  ],
  "fetched_at": "2026-11-21T13:00:00Z",
  "from_cache": true
}
```

## Agent eligibility output

One per criterion.

```json
{
  "trial_id": "NCT00000000",
  "criterion_type": "inclusion | exclusion",
  "criterion_text": "Histologically confirmed IDH-mutant glioma",
  "status": "met | not_met | unknown",
  "evidence_field": "idh",
  "reason": "needs pathology: IDH status"
}
```

`status` answers "does the patient pass this criterion?". For an exclusion, `met` means the exclusion does not apply. `criterion_type` is optional but matches the gold labels.

Safety rule 10: a row whose `evidence_field` is a mutation check field (`mutation_check`, `mutation_context`, `sbs11_call`, `exposures`, `total_mutations`, `counts_96`, `sbs11_fraction_ci95`, `detection_limit_95`, `critical_fraction`) must be `unknown`; the schema rejects `met` or `not_met`. A CLIA TMB criterion cites `tmb_clia`.

## Shortlist → frontend

Shown at the `review_shortlist` gate. A trial is on the shortlist when no criterion is `not_met` and none is waiting on a `pending` result.

```json
{
  "case_id": "demo-001",
  "origin": {"zip": "77840", "lat": 30.628, "lon": -96.3344},
  "radius_miles": 100,
  "statuses_searched": ["RECRUITING", "NOT_YET_RECRUITING"],
  "trials": [
    {
      "trial_id": "NCT99000001",
      "title": "[SYNTHETIC] ...",
      "recruitment_status": "RECRUITING",
      "status_label": "Recruiting",
      "remote_participation": false,
      "nearest_site": {"facility": "Synthetic Neuro-Oncology Center A", "city": "Houston", "state": "TX", "lat": 29.7079, "lon": -95.4012, "distance_miles": 84.6, "drive_minutes": 105},
      "summary": {"met": 5, "not_met": 0, "unknown": 1},
      "eligibility": ["<Agent eligibility output>"],
      "mutation_context": null,
      "doctor_decision": "pending | keep | reject"
    }
  ]
}
```

`status_label` comes from `trial_search.status_labels` in `policy.yaml` (today `"Recruiting"` or `"Not yet recruiting: opens soon, cannot enroll yet"`). Allowed `recruitment_status` values are `trial_search.statuses`, so staff can add a status without a code change. `mutation_context` is the Mutation check output when it ran, shown as context only. `distance_miles` is straight-line from the ZIP centroid. `drive_minutes` comes from OSRM.

## No-match plan → frontend

Produced when the shortlist is empty. Settings come from `trial_search.no_match_plan` in `policy.yaml`.

```json
{
  "case_id": "demo-003",
  "searched": {"radius_miles": 100, "statuses": ["RECRUITING", "NOT_YET_RECRUITING"], "match_count": 0},
  "widened": {
    "radius_miles": 500,
    "include_remote_trials": true,
    "trials": ["<Shortlist trial>"]
  },
  "near_misses": [
    {
      "trial_id": "NCT99000007",
      "title": "[SYNTHETIC] ...",
      "near_miss_type": "fails_one_rule | pending_result",
      "blocking": ["<Agent eligibility output>"],
      "nearest_site": {"city": "New Orleans", "state": "LA", "distance_miles": 376.1, "drive_minutes": 360}
    }
  ],
  "alert": {
    "alert_id": "alert-demo-003-1",
    "query": {"bucket": "meningioma", "markers": {"grade": 2}, "zip": "77840", "radius_miles": 500},
    "notify": "care_team_in_app",
    "created_at": "2026-11-21T15:10:00Z"
  },
  "tumor_board_log": {"ts": "2026-11-21T15:10:00Z", "case_id": "demo-003", "entry": "No current match", "searched_at": "2026-11-21T15:09:58Z"}
}
```

A near miss fails exactly one criterion, or has no failures but waits on a `pending` result. Alerts go to the care team in the app only. The app never contacts patients.

## Audit log entry

```json
{"ts": "2026-11-21T14:03:12Z", "case_id": "demo-001", "actor": "agent | doctor | system", "action": "trial_search", "detail": {}}
```

`system` is for events no agent or doctor caused, such as startup and policy reloads. `case_id` is `null` for those.
