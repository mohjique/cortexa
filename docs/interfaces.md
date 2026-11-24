# Interfaces between components

Draft. Agree on these at kickoff, then change them only after telling the team.

All examples use synthetic data. Trial IDs `NCT990000xx` are fake (see `agent/demo_cache/`).

## Workspaces (every case route)

The public demo gives each browser its own workspace (issue 48), so one visitor can't see or change another visitor's cases.

- The frontend makes a random ID on first load (`crypto.randomUUID`), keeps it in `localStorage` under `cortexa.workspace_id` (in memory for the tab if storage is blocked), and sends it on **every** call as the header `X-Cortexa-Workspace: <id>`. See `frontend/src/workspace.ts`.
- The ID must match `^[A-Za-z0-9_-]{8,64}$` (no dots or slashes, so no path traversal). It is lower-cased. Missing or invalid on a case route: **400**.
- `GET /cases/{case_id}/image` and `GET /cases/{case_id}/heatmap` also accept it as `?ws=<id>`, because an `<img src>` can't send a header. The header wins if both are sent. No other route reads `?ws=`.
- `GET /cases` lists only the caller's workspace. Every `/cases/{case_id}/...` route, and `POST /cases`, resolves the case only in the caller's workspace. A case that exists only in another workspace returns **404**, the same as a case that doesn't exist (never 403, which would leak that it exists).
- Case IDs repeat across workspaces: each browser can have its own `demo-012`.
- Each workspace has its own audit log. `GET /cases/{case_id}/audit` reads it, and every entry from a request (agent tool calls, stage changes, doctor decisions) lands only in that workspace's log. The first request from a new ID writes `workspace_created` (actor `system`).
- A new workspace starts the way the app always has: no cases, and the shared demo gallery (`GET /demo-scans`). The backend does not pre-create demo cases.
- `POST /workspace/reset` (header required, no body) deletes the caller's cases, audit log, stored scans and heatmaps, starts a fresh workspace, and returns `{"status": "reset", "removed_cases": 2}`. The fresh log's first entry is `workspace_reset` with `{"removed_cases": n}` (rule 7). Other workspaces are untouched. The UI has a **Reset demo** button in the header, behind a confirm dialog.
- Global routes need no workspace: `GET /health`, `GET /demo-scans`, `GET /demo-scans/{case_id}`.
- `GET /health` → `{status, classifier, trial_source}`. `trial_source` is `demo` (synthetic trials from `agent/demo_cache/`) or `live` (ClinicalTrials.gov); the UI banner says which.
- Housekeeping: when a new workspace starts, workspaces unused for more than `CORTEXA_WORKSPACE_TTL_HOURS` (default 24) are deleted, then the least recently used ones until at most `CORTEXA_MAX_WORKSPACES` (default 200) remain. A visitor whose workspace was evicted just gets a fresh, empty one.
- CORS allows the `X-Cortexa-Workspace` request header (with `Accept` and `Content-Type`).

**This is isolation, not authentication.** Anyone who holds a workspace ID can open that workspace, and the `?ws=` form can appear in server access logs. It is acceptable only because every case is synthetic (rule 9). The doctor ID is attribution on decisions, not scoping.

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

`preop_screen` is the one stage where `waiting_on` changes inside the stage. It starts as `agent` while the screen runs, then becomes `doctor` until the `preop_review` gate is recorded.

## Classifier → agent

```json
{
  "case_id": "demo-001",
  "suspected_bucket": "glioma | meningioma | pituitary | no_tumor",
  "probabilities": {"glioma": 0.91, "meningioma": 0.05, "pituitary": 0.02, "no_tumor": 0.02},
  "medgemma_bucket": null,
  "confidence": 0.88,
  "flagged_uncertain": false,
  "heatmap_path": "artifacts/demo-001_gradcam.png",
  "findings_summary": "Axial T1 post-contrast slice with an enhancing mass in the left frontal lobe, suspected high-grade glioma.",
  "reasoning": "The slice shows an enhancing intra-axial mass, consistent with the suspected glioma. The pre-op screen lists what the biopsy must answer.",
  "reasoning_withheld": false
}
```

`findings_summary` is optional draft text from MedGemma, shown to the doctor labeled as an AI draft. The schema rejects any summary that diagnoses, names a tumor type without "suspected" or similar hedging, recommends treatment, or suggests a referral (`agent/findings.py`, rule 1). `reasoning` is optional draft text from MedGemma, written for `suspected_bucket` (the ResNet call) and its decision-tree pathway (`agent/reasoning.py`). It is attached only when it was written for that same bucket, and the schema rejects text that diagnoses, recommends, says what anyone should do, refers, or names a tumor type without hedging. `reasoning_withheld` is `true` when the reasoning written for this bucket was dropped because it contradicted the ResNet call (it said nothing is there, described a mass on a no-tumor call, or named another tumor type); `reasoning` is then `null` and the UI shows a fixed note, "Draft reasoning withheld", so the doctor knows. No MedGemma text is kept or shown, and the flag never changes `suspected_bucket`, `flagged_uncertain` or the route. A draft dropped for wording (rule 1) or written for a different bucket does not set it. `medgemma_bucket` is always `null`: MedGemma does not vote (its solo eval was below 0.5). The schema drops any value sent in it, so a second model can neither change the suspected bucket nor flag a case. The draft text comes from `classifier/medgemma/demo.json` (precomputed on Colab for the demo cases); without that file, cases have no MedGemma text and everything else works the same.

### Upload endpoint

`POST /cases?case_id=<id>&filename=<name>` with the raw image bytes as the request body, for example `fetch(url, {method: "POST", body: file})`. Both query parameters are optional; without `case_id` the API makes `case-<8 hex>`. The API classifies the image, creates the case and moves it to `confirm_bucket`. It returns the case state (201), or 422 for an unreadable image (no case is created) and 409 if the case exists. The image is kept so the doctor can read it: the API decodes it and stores its own PNG re-encoding (never the uploaded bytes) under the workspace's folder. An upload PIL can't decode (possible with the stub) is not kept; the case works without it.

`GET /cases/{case_id}/image` returns the case's MRI as that PNG (404 if it was not kept). It accepts the workspace as `?ws=` (see Workspaces).

`GET /demo-scans` lists the committed demo MRIs as `[{"case_id": "demo-001"}, ...]`: case IDs only, never a label or the model's call, so the gallery can't hand the doctor an answer. `GET /demo-scans/{case_id}` returns one, unchanged. The UI opens a demo scan as a case by uploading those bytes with `POST /cases`, so it goes through the same classifier and gates as any upload.

`GET /cases/{case_id}/heatmap` returns the Grad-CAM overlay PNG (404 if there is none, for example from the stub). It accepts the workspace as `?ws=` (see Workspaces).

`flagged_uncertain` in the stored output is the agent's value: the classifier's flag, or confidence below `classifier.min_confidence_for_auto_suggest`. There is no model-disagreement flag, because MedGemma does not vote. The agent never clears a flag the classifier set.

## Doctor decision (approval gates) → agent

One record per gate. Each one is also written to the audit log.

```json
{
  "case_id": "demo-001",
  "gate": "confirm_bucket | watch_or_treat | preop_review | hormone_labs | surgery_or_meds | enter_results | review_shortlist",
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
| `preop_review` (glioma) | `{"screen_id": "preop-1a2b3c4d5e6f", "tests_reviewed": true, "pre_surgery_trials": [{"trial_id": "NCT99000003", "action": "pursue_before_surgery \| not_pursuing", "reason": "optional free text"}]}`. Needs exactly one entry per trial in the pre-op screen's `pre_surgery_trials` (an empty list when there are none). `reason` is required for `not_pursuing`. `screen_id` is the screen the doctor reviewed; if the case's screen has changed since (rescreen), the decision is rejected with 422 and must be redone. |
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
  "sex": "female | male | null",
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
- `sex`: `female`, `male`, or `null` (not recorded). Used only against a trial's registry sex eligibility; when it is `null` and a trial enrolls one sex, that row is `unknown`, never guessed.
- `sex`, `comorbidities`, `can_use_phone_or_tablet`, `tmb_clia` and `recurrence_number` are optional. `null` means not recorded, and the checker marks dependent criteria `unknown`. An empty `comorbidities` list means "none".

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
  "screen_id": "preop-1a2b3c4d5e6f",
  "trials_considered": 7,
  "tests_needed": [
    {
      "field": "idh | codeletion_1p19q | mgmt | grade | h3k27m | braf",
      "label": "IDH mutation status",
      "current_value": "pending",
      "decides_trials": 4,
      "trial_ids": ["NCT99000001", "NCT99000002", "NCT99000004", "NCT99000007"],
      "mentions_trials": 0,
      "mention_trial_ids": [],
      "evidence": [
        {"trial_id": "NCT99000001", "requires": "mutant", "criterion_text": "Histologically confirmed astrocytoma, IDH-mutant, CNS WHO grade 2 or 3"},
        {"trial_id": "NCT99000002", "requires": "wildtype", "criterion_text": "Histologically confirmed glioblastoma, IDH-wildtype"}
      ]
    },
    {
      "field": "grade", "label": "WHO grade", "current_value": "pending",
      "decides_trials": 2, "trial_ids": ["NCT99000001", "NCT99000004"],
      "mentions_trials": 1, "mention_trial_ids": ["NCT99000008"],
      "evidence": [
        {"trial_id": "NCT99000001", "requires": "2-3", "criterion_text": "Histologically confirmed astrocytoma, IDH-mutant, CNS WHO grade 2 or 3"},
        {"trial_id": "NCT99000004", "requires": "4-4", "criterion_text": "Histologically confirmed glioblastoma, IDH-wildtype, CNS WHO grade 4"},
        {"trial_id": "NCT99000008", "requires": null, "criterion_text": "Recurrent glioma of any grade"}
      ]
    }
  ],
  "pre_surgery_trials": [
    {
      "trial_id": "NCT99000003",
      "title": "[SYNTHETIC] Window-of-opportunity drug before resection in suspected glioblastoma",
      "recruitment_status": "NOT_YET_RECRUITING",
      "criterion_text": "Must enroll before planned surgical resection",
      "matched_on": "criteria | title_hint",
      "nearest_site": {"city": "Austin", "state": "TX", "distance_miles": 86.7, "drive_minutes": 110},
      "doctor_action": "pending | pursue_before_surgery | not_pursuing",
      "doctor_reason": null
    }
  ],
  "label": "Suspected glioma. Biopsy results pending."
}
```

`tests_needed` is built deterministically from the INCLUSION criteria of every trial that passes the glioma filter (`pre_surgery_detection.condition_keywords`), read with `agent.concepts.detect`, the same detectors the eligibility checker uses. Exclusion bullets are not read for it.

- `decides_trials` / `trial_ids`: trials whose criterion states a readable requirement for the test, so the result changes the criterion between `met` and `not_met`.
- `mentions_trials` / `mention_trial_ids`: trials that mention the test but state no readable requirement ("IDH status will be collected", "glioma of any grade", "H3 K27M-mutant"; H3 K27M and BRAF have no value reader yet). Never counted as deciding; the UI shows them as "check criteria". A trial is in one list or the other, never both.
- `evidence`: one entry per trial and requirement, with the inclusion criterion that cites it (rule 4). `requires` is the normalized requirement: IDH `mutant | wildtype`, 1p/19q `codeleted | intact`, MGMT `methylated | unmethylated`, grade `lo-hi` (`"4-4"` = grade 4), or `null` for a mention. A trial with cohorts can list more than one requirement.
- `current_value` is `pending` at pre-op (no pathology yet).
- Tests are ordered by `decides_trials`, then `mentions_trials`, descending. A test no glioma trial mentions is left out.
- `mentions_trials`, `mention_trial_ids` and `evidence` are optional (default `0`, `[]`, `[]`). When `evidence` is present, every listed trial must have an entry.
- Counts per test are audited as `tool_call:preop_tests_needed` (`{"glioma_trials": n, "tests": {field: {"decides", "mentions", "requires": {value: n}}}}`).

After the screen, the case waits at `preop_screen` (`waiting_on: doctor`) for the `preop_review` gate. Every `pre_surgery_trials` entry must be acknowledged before the case moves to `awaiting_pathology`. The gate copies each choice onto the trial as `doctor_action` and `doctor_reason` (`pending` until then).

A trial is flagged when an inclusion criterion contains a `pre_surgery_detection.inclusion_phrases` phrase together with an `inclusion_context_words` word (enrollment or study-treatment wording, so "Preoperatively, the lesion must be ≥1 cm" does not count), or an exclusion criterion contains an `exclusion_phrases` phrase. Phrases match as whole words. The trial's conditions or title must also name a `condition_keywords` term (all in `policy.yaml`). `criterion_text` is the line that matched (`matched_on: criteria`). A trial whose title has a `title_hints` phrase but no criterion match is still listed, with `matched_on: title_hint` and `criterion_text: "Title: ..."`. The UI shows it as "Possible: check criteria", and the doctor must answer it like any other. The screen has no surgical history, so it cannot rule a trial out for prior surgery; the doctor does that at the gate.

**Rescreen.** While the case waits on `preop_review`, the doctor can ask for a fresh screen with `POST /cases/{case_id}/preop/rescreen` and body `{"doctor_id": "dr-synthetic-01", "reason": "optional"}`. The old screen is dropped (its flags stay in the audit log as `preop_rescreen_requested`), `waiting_on` goes back to `agent`, and the screen runs again. Returns 409 at any other point.

**`screen_id`** is a content hash of the screen (flags, statuses, criteria, sites, tests). It changes whenever any of those change, so a review is always tied to the screen the doctor saw.

**Location.** The case record carries the patient's ZIP (`zip`, ZIP only, safety rule 6). The screen searches nationwide (no distance cutoff), geocodes the ZIP to set `nearest_site` on each flag, and sorts `pre_surgery_trials` nearest first; flags with no `nearest_site` go last. Distance is for sorting and display only, never a filter. Without a ZIP, or if geocoding fails (`preop_location_unavailable` in the audit log), the screen still runs with `nearest_site: null`.

**Agent failures after a doctor action.** If an agent stage fails after a decision or rescreen is saved, the API still returns 200 with the saved state (`waiting_on: agent`), and the failure is audited as `stage_failed:<stage>`. The decision is not undone. Retry with `POST /cases/{case_id}/run`, which returns 503 if the stage fails again.

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
  "sex_eligibility": "ALL | FEMALE | MALE | null",
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

Each trial is in one of three groups, the way coordinators sort a prescreen:

| `group` | Meaning | `needs_test_fields` |
|---|---|---|
| `eligible` | Eligible on what we know. At least one clinical criterion is `met`; any `unknown` rows are routine items (labs, washout, device use) checked at the screening visit. | `[]` |
| `needs_test` | At least one `unknown` on a key field listed in `trial_search.key_unknown_fields` in `policy.yaml` (today `idh`, `codeletion_1p19q`, `mgmt`, `tmb_clia`). | The key fields left unknown, in policy order, e.g. `["tmb_clia"]`. Each has an `unknown` row in `eligibility` whose `reason` says which test decides it. |
| `needs_review` | Nothing ruled it out, but no clinical inclusion criterion was confirmed automatically: the only `met` rows are demographics (age, sex) or exclusions that do not apply. Common for live trials whose free-text criteria the rule library does not cover. The doctor must read the criteria. | `[]` |

A `needs_test` or `needs_review` trial is not a match yet. Show each group under its own heading, for example "Needs a test: CLIA TMB result" or "Needs doctor review of the criteria". Near misses are unchanged and only appear in the no-match plan.

**Registry age limits.** When no criteria bullet decides age, the checker adds one inclusion row from the trial's structured age limits (ClinicalTrials.gov `minimumAge`/`maximumAge`, NCI `min/max_age_in_years`), with `criterion_text` like `"Age 18 years or older (trial registry age limits)"` and `evidence_field: "age"`.

**Registry sex eligibility.** `sex_eligibility` comes from ClinicalTrials.gov `eligibilityModule.sex` (`ALL`, `FEMALE`, `MALE`) or NCI `eligibility.structured.sex` (`BOTH` is stored as `ALL`); a missing or unrecognized value is `null`. When a trial enrolls one sex only, the checker adds one inclusion row with `criterion_text` like `"Sex: female only (trial registry sex eligibility)"` and `evidence_field: "sex"`: `met` or `not_met` from the doctor-entered `sex`, or `unknown` ("not recorded: sex; doctor must enter it") when it is `null`. `ALL` or `null` adds no row. A trial that fails only on sex is not a near miss.

There is no distance cutoff: every eligible trial is listed, however far away. `trials` lists the `eligible` group first, then `needs_test`, then `needs_review`. Within each group, trials are sorted by `nearest_site.distance_miles`, nearest first, and trials with no `nearest_site` come last. `nearest_site` is `null` for remote-only trials and for trials whose registry lists no site coordinates; those trials are kept, never dropped. The schema rejects a shortlist in any other order. `drive_minutes` comes from one batched OSRM `table` request for the whole shortlist.

```json
{
  "case_id": "demo-001",
  "origin": {"zip": "77840", "lat": 30.628, "lon": -96.3344},
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
      "doctor_decision": "pending | keep | reject",
      "group": "eligible | needs_test | needs_review",
      "needs_test_fields": []
    }
  ]
}
```

`status_label` comes from `trial_search.status_labels` in `policy.yaml` (today `"Recruiting"` or `"Not yet recruiting: enrollment has not opened"`). Allowed `recruitment_status` values are `trial_search.statuses`, so staff can add a status without a code change. `mutation_context` is the Mutation check output when it ran, shown as context only. `distance_miles` is straight-line from the ZIP centroid. `drive_minutes` comes from OSRM.

## No-match plan → frontend

Produced when the shortlist is empty. Since the search has no distance cutoff, there is no wider search to try: no eligible trial exists anywhere in the searched statuses. Settings come from `trial_search.no_match_plan` in `policy.yaml`.

```json
{
  "case_id": "demo-003",
  "searched": {"statuses": ["RECRUITING", "NOT_YET_RECRUITING"], "match_count": 0},
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
    "query": {"bucket": "meningioma", "markers": {"grade": 2}, "zip": "77840"},
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

`system` is for events no agent or doctor caused, such as startup, policy reloads, `workspace_created` and `workspace_reset`. `case_id` is `null` for those.

During an API request, entries go to the caller's workspace log, `<CORTEXA_DATA_DIR>/workspaces/<id>/audit.jsonl`. Entries from outside a request (startup, scripts) go to `<CORTEXA_DATA_DIR>/audit.jsonl`.
