# agent/

- Agent loop that follows `docs/decision-tree.md` stage by stage, stopping at every doctor approval gate.
- Tools: ClinicalTrials.gov API v2 (no key), NCI CTS API v2 (`X-API-KEY`), Nominatim geocoding (ZIP/city only), OSRM drive time.
- Policy engine: loads `policies/policy.yaml` and enforces it in code, outside the LLM.
- Eligibility checker: `met | not_met | unknown` per criterion, with evidence.
- Append-only audit log.
- Cache API responses for the demo.

## Workspaces (issue 48)

`agent/workspace.py`. Each browser sends a random `X-Cortexa-Workspace` ID (`?ws=` on the two case image routes). Its cases, audit log, scans and heatmaps live in `<CORTEXA_DATA_DIR>/workspaces/<id>/`. `Workspace.active()` sets two context variables, so `store.get_store()` and `audit.audit()` reach the request's workspace and the workflow, pre-op, matching and eligibility code needs no change. Any background work added later must run in a copied context (`contextvars.copy_context()`) to keep its workspace. Another workspace's case is a 404. API details: `docs/interfaces.md`, "Workspaces".

| Variable | Default | What |
|---|---|---|
| `CORTEXA_WORKSPACE_TTL_HOURS` | `24` | A workspace unused this long is deleted when a new workspace starts. |
| `CORTEXA_MAX_WORKSPACES` | `200` | At most this many workspaces; the least recently used go first. |

Isolation, not authentication: anyone holding a workspace ID can open it. Acceptable only because the data is synthetic (rule 9).

## Trial search (F7)

- `agent/trials.py`: `search_trials(condition, case_id)` → merged `TrialRecord` list from ClinicalTrials.gov (primary) and NCI (fills gaps, `source: ctgov+nci`). Statuses from `trial_search.statuses` are filtered client-side too, so a policy edit changes results even on cached data. Closed sites are dropped. No distance cutoff: the search is nationwide, and distance only sorts and labels trials.
- `agent/geo.py`: `geocode_zip` (5-digit ZIP only), `haversine_miles`, `nearest_site` (straight line, then OSRM drive time for the closest site only; `None` if OSRM is down).
- `agent/http_cache.py`: source mode.

| Env var | Default | Meaning |
|---|---|---|
| `CORTEXA_TRIAL_SOURCE` | `demo` | `demo` reads `agent/demo_cache/` only, no network. `live` calls the APIs, saves to `cache/`, falls back to the last saved response, then the demo cache. |
| `CORTEXA_CACHE_DIR` | `cache/` | Live-response cache (gitignored). Request headers are never saved. |
| `NCI_API_KEY` | unset | Live NCI calls. Unset in live mode = NCI is skipped and logged. |

Every search, geocode, drive-time call, fallback and dropped record is in the audit log.

## Eligibility checker (F8)

- `agent/eligibility.py`: `check_trial(trial, facts)` → one `EligibilityResult` per criterion bullet. Deterministic: `split_criteria` splits the text into inclusion and exclusion bullets, and every rule that applies runs: the pattern rules in `RULES`, the tumor types named, and the concepts from `agent/concepts.py` checked against `Facts`. A bullet is `met` only when every part holds and every concept and content word in it was read; anything less is `unknown` for the doctor. A `not_met` hides the trial, so it is held to the same care: a bullet whose wording may make the failing part not a requirement (a condition, a permission, timing after a treatment, one arm or cohort of several, a heading, or "not eligible" under inclusion; `_NOT_MET_DOUBT`) is `unknown` too. No LLM call. Build `Facts` with `Facts.from_results(doctor_results)`, or by hand at the pre-op screen.
- A missing field is `unknown`. Pathology fields use `eligibility.missing_field_reason_template` from `policy.yaml`; other fields say "not recorded". A `pending` result is `unknown` with a reason starting `pending result:` (`is_pending`). `passes(rows)` is the shortlist rule: no `not_met`, nothing pending.
- A bullet no rule covers is `unknown`, `evidence_field: doctor_review`. Live trials will have many of these, and the doctor reviews them.
- "Enroll before surgery" bullets match `pre_surgery_detection.inclusion_phrases` in `policy.yaml` as whole words, and only in a bullet that also has an `inclusion_context_words` word (enrollment or study-treatment wording). The pre-op screen (`agent/preop.py`) uses the same matching.
- Rule 10: `Facts` has no mutation check field. TMB criteria read `tmb_clia` only.
- `python -m agent.eligibility_eval`: agreement with `gold_eligibility` in `clinical/patients/`. Mode `entered` uses the `enter_results` blocks as is. Mode `profile` fills `recurrence_number`, `comorbidities`, `can_use_phone_or_tablet` and `tmb_clia` from the profile.

## Matching: shortlist (F9) and no-match plan (F10)

- `agent/matching.py`: `match_case(rec, policy)` is the workflow's default matching hook. It geocodes the ZIP, searches nationwide, checks every trial, and shortlists every one that passes (`eligibility.passes`). There is no distance cutoff: the shortlist is sorted nearest first, and remote-only trials come last. Drive time is computed only for trials it shows. A non-remote trial with no site coordinates is skipped and audited, because the schema needs a site for it.
- Empty shortlist → `NoMatchPlan` from `trial_search.no_match_plan`. Near misses fail exactly one rule, or wait on a `pending` result. A trial that fails only on tumor type, new vs recurrent, the secreting hormone or "enroll before surgery" is not a near miss (`NOT_NEAR_MISS_FIELDS`). The plan also sets an in-app alert for the care team and writes a tumor board log entry.
- `workflow.stub_matching` is kept for state-machine tests.
