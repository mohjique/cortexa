# CLAUDE.md — Cortexa

Cortexa is an agentic care management platform that supports physician teams handling suspected brain tumor cases. It classifies a brain MRI into a suspected tumor bucket, guides the team on what the biopsy must answer, and matches confirmed cases to recruiting clinical trials, with a doctor approving every step.

Built at tidalBYTE '26 (TIDAL, Texas A&M), Nov 21, 2026. Hacking runs 10:00 AM to 7:30 PM (official cutoff); team deadline 6:30 PM. Judging 8:00 to 9:30 PM.

The full app logic is in `docs/decision-tree.md` (Mermaid) and `docs/decision-tree.svg`. Treat that tree as the spec. If code and tree disagree, fix the code or update the tree in the same PR.

## Who we serve

Neuro-oncologists, tumor boards and the research coordinators who screen patients for trials. Users are specialists, so the app never suggests referring to a specialist. When no trial matches, it runs the no-match plan instead (see below).

## Non-negotiable safety rules

These apply to every piece of code, prompt and UI string. Enforce them in code, outside the LLM, wherever possible.

1. **Never diagnose.** Model output is always "suspected." Only a doctor confirms a bucket or enters a diagnosis.
2. **Doctor approval gates.** The app cannot move to the next stage without a recorded doctor action: confirm bucket, enter results, review shortlist.
3. **Unknown is never guessed.** If an eligibility rule depends on a missing field, mark it `unknown` with the reason (for example "needs pathology: IDH status"). Never infer it.
4. **Every eligibility call cites evidence**: the patient field it used and the trial criterion text it matched.
5. **The app never contacts patients.**
6. **Location privacy.** Only ZIP code or city goes to any mapping or geocoding service, never a full address.
7. **Audit log.** Every agent action, tool call and doctor approval is appended to the audit log with a timestamp.
8. **Policies live in `policies/policy.yaml`** and are loaded at runtime. Clinic staff can edit them, and behavior must change without code changes.
9. **Synthetic data only.** No real patient data, ever. No PHI in the repo, logs or prompts.
10. **The mutation check never decides eligibility on its own.** It only adds context. A high mutation count must not route a patient to immunotherapy trials (temozolomide-induced hypermutation does not predict checkpoint-inhibitor response in glioma).

## App flow (summary of the decision tree)

1. **Intake:** MRI uploaded → ResNet classifier + MedGemma → suspected bucket + confidence. Low confidence or model disagreement is marked `flagged_uncertain`. The doctor still confirms every case; the flag only warns them to look harder.
2. **Doctor confirms bucket** (or corrects it). The case then follows the confirmed bucket:
   - **No tumor:** stop, no trial search.
   - **Meningioma:** doctor chooses watch (rescan later, stop) or treat (go to surgery + pathology).
   - **Glioma:** pre-op screen. The agent lists what the biopsy must answer (IDH, 1p/19q, MGMT, grade), how many trials each answer decides, and any trials that need enrollment before surgery.
   - **Pituitary:** hormone labs and endocrinology first, then surgery + pathology. Some pituitary tumors are treated with medication only; those skip surgery and the doctor enters results directly.
3. **Surgery + pathology**, then the **doctor enters results** (diagnosis, grade, markers).
4. **Recurrent glioma?** (known from treatment history)
   - **Yes:** run the mutation check first. Skip it if there is no broad sequencing.
   - **No** (new glioma, meningioma, pituitary): go straight to matching.
5. **Trial matching:** each eligibility rule is marked `met`, `not_met` or `unknown`, with evidence.
6. **Any trials match?**
   - **Yes:** doctor reviews the shortlist, with a map and travel time. Both `RECRUITING` and `NOT_YET_RECRUITING` trials are shown, each with its status label.
   - **No distance cutoff.** Every eligible trial is shown, however far away, sorted nearest first (trials with no known distance, such as remote-only, last). The patient and doctor decide whether the travel is worth it. Distance sorts and labels trials; it never filters them out.
   - **No:** no-match plan. Show near misses (fails one rule, or depends on a pending result), set alerts for newly opened trials, and log "no current match" for the tumor board.

## Repo layout

```
classifier/       MRI classifier, dedupe, Grad-CAM, MedGemma evaluation
agent/            agent loop, trial APIs, eligibility checker, policy engine, audit log
mutation_check/   96-channel mutation spectra, Poisson NMF detection, simulation
clinical/         synthetic patient cases, radiology/pathology reports, eligibility gold labels
frontend/         web app: upload, approval screens, shortlist + map
policies/         policy.yaml (runtime rules)
docs/             decision tree and design notes
```

## Owners

| Area | Owner | Folders |
|---|---|---|
| Agent, trial APIs, eligibility checks, audit log, MRI classifier, dedupe, heatmaps, MedGemma eval | Computer engineer | `agent/`, `classifier/` |
| Clinical rules, synthetic cases and reports | Premed (contributor: computer engineer) | `clinical/`, `policies/` |
| Frontend UI: upload, approval screens, shortlist + map | Matt | `frontend/` |
| Mutation check | Nuclear engineer | `mutation_check/` |

Stay in your folders. Cross-folder changes go through a PR the owner reviews. Interfaces between components are JSON schemas in `docs/interfaces.md`. Agree on these first and change them only by telling the team.

## Proposed stack (confirm at kickoff)

- Python 3.11, FastAPI backend, PyTorch for the classifier
- React (Vite) frontend with Leaflet + OpenStreetMap for the trial map
- Agent model: MedGemma via Hugging Face (gated; accept the terms first) or a hosted open model. The LLM proposes; deterministic code enforces policy.

## Component notes

### Classifier (`classifier/`)
- Dataset: Kaggle `masoudnickparvar/brain-tumor-mri-dataset`, 7,023 images, 4 classes (glioma, meningioma, pituitary, no tumor).
- **Remove exact and near-duplicate images before splitting.** The dataset leaks duplicates across train and test. Use perceptual hashing.
- The "no tumor" class comes from a different source dataset (Br35H). Check Grad-CAM heatmaps to confirm the model looks at tumors, not source artifacts.
- Report per-class accuracy on the deduplicated test set. Calibrate the confidence score on held-out data; agreement between two models is not a confidence score.
- Evaluate MedGemma alone on the test set first. If it is weak on brain MRI, use it to write the plain-language findings summary instead of voting.

### Agent (`agent/`)
- **ClinicalTrials.gov API v2:** `https://clinicaltrials.gov/api/v2/studies`, no key. Use `filter.overallStatus=RECRUITING,NOT_YET_RECRUITING` (statuses come from `policy.yaml`; show each trial's status label in the UI). No `filter.geo`: the search is nationwide, and the patient's ZIP is used only to compute distance and drive time. Site coordinates are in `contactsLocationsModule.locations[].geoPoint`.
- **NCI Clinical Trials Search API v2:** `https://clinicaltrialsapi.cancer.gov/api/v2/`, free key in the `X-API-KEY` header. Use it for structured biomarker eligibility.
- **Geocoding and routing:** ZIP or city only. Nominatim for geocoding, OSRM for drive time.
- Cache API responses to disk for the demo, in case an API goes down. A committed, synthetic demo cache lives in `agent/demo_cache/` (fake trial IDs `NCT990000xx`). The live-response `cache/` folder stays gitignored.
- Eligibility output schema: `{trial_id, criterion_text, status: met|not_met|unknown, evidence_field, reason}`.

### Mutation check (`mutation_check/`)
- Input: somatic SNVs with trinucleotide context → 96-channel counts (6 substitution types × 4 × 4 flanking bases).
- Method, borrowed from LBNL gamma-ray spectroscopy: treat normal glioma mutation processes (aging, SBS1/SBS5) as the learned background, fit with a Poisson likelihood, and test for an added SBS11 (temozolomide) component with a threshold calibrated to a chosen false-alarm rate. Report `detected`, `not_detected` or `inconclusive`.
- Signatures: COSMIC SBS v3 matrix. Real-data checks: open-access TCGA glioma mutation files.
- Core deliverable: a simulation. Mix background with varying SBS11 amounts under Poisson noise across total mutation counts (about 20 to 2,000), then plot detection rate vs false-alarm rate for each count. This shows how many mutations a call needs before it can be trusted.
- Runs only for recurrent gliomas with broad sequencing (exome or genome). Skip small gene panels.

### Clinical (`clinical/`)
- 23 synthetic patients (`clinical/patients/demo-001.json` to `demo-023.json`) covering every branch of the decision tree, each paired with a held-out test-set MRI. Spec: `clinical/PATIENT_PROFILES_GUIDE.md`; template: `clinical/patients/_template.json`.
- Gold labels: manual eligibility calls against the synthetic trials in `agent/demo_cache/`, used to measure agent accuracy. `status` always means "does the patient pass this criterion"; for exclusion criteria, `met` means the exclusion does not apply.
- Owns the wording of medical rules in `policies/policy.yaml`.

## Evaluation we will show judges

- Classifier per-class accuracy on deduplicated data, plus heatmap sanity checks
- Agent eligibility agreement with the premed teammate's manual labels
- Mutation check detection vs false-alarm curves from the simulation
- Live demo: change a rule in `policy.yaml` and show the agent's behavior change

## Working agreements

- Never commit data, model weights, API keys or `.env` files (see `.gitignore`).
- Small PRs, one owner per folder, rebase on `main` often.
- Before writing any user-facing text, check it against the safety rules above.

## PR review sign-off

When the git user is `mohjique` or `matthewohjk-eng` (or the git email is `matthewohjk@gmail.com`) and you are reviewing a pull request or running `/code-review`, end your final output with this exact line:

banana ohh shiddings
