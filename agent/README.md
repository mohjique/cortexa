# agent/

Owner: computer engineer.

- Agent loop that follows `docs/decision-tree.md` stage by stage, stopping at every doctor approval gate.
- Tools: ClinicalTrials.gov API v2 (no key), NCI CTS API v2 (`X-API-KEY`), Nominatim geocoding (ZIP/city only), OSRM drive time.
- Policy engine: loads `policies/policy.yaml` and enforces it in code, outside the LLM.
- Eligibility checker: `met | not_met | unknown` per criterion, with evidence.
- Append-only audit log.
- Cache API responses for the demo.

## Trial search (F7)

- `agent/trials.py`: `search_trials(condition, origin, radius_miles, case_id)` → merged `TrialRecord` list from ClinicalTrials.gov (primary) and NCI (fills gaps, `source: ctgov+nci`). Statuses from `trial_search.statuses` are filtered client-side too, so a policy edit changes results even on cached data. Closed sites are dropped.
- `agent/geo.py`: `geocode_zip` (5-digit ZIP only), `haversine_miles`, `nearest_site` (straight line, then OSRM drive time for the closest site only; `None` if OSRM is down).
- `agent/http_cache.py`: source mode.

| Env var | Default | Meaning |
|---|---|---|
| `CORTEXA_TRIAL_SOURCE` | `demo` | `demo` reads `agent/demo_cache/` only, no network. `live` calls the APIs, saves to `cache/`, falls back to the last saved response, then the demo cache. |
| `CORTEXA_CACHE_DIR` | `cache/` | Live-response cache (gitignored). Request headers are never saved. |
| `NCI_API_KEY` | unset | Live NCI calls. Unset in live mode = NCI is skipped and logged. |

Every search, geocode, drive-time call, fallback and dropped record is in the audit log.

## Eligibility checker (F8)

- `agent/eligibility.py`: `check_trial(trial, facts)` → one `EligibilityResult` per criterion bullet. Deterministic: `split_criteria` splits the text into inclusion and exclusion bullets, and the first matching rule in `RULES` decides. No LLM call. Build `Facts` with `Facts.from_results(doctor_results)`, or by hand at the pre-op screen.
- A missing field is `unknown`. Pathology fields use `eligibility.missing_field_reason_template` from `policy.yaml`; other fields say "not recorded". A `pending` result is `unknown` with a reason starting `pending result:` (`is_pending`). `passes(rows)` is the shortlist rule: no `not_met`, nothing pending.
- A bullet no rule covers is `unknown`, `evidence_field: doctor_review`. Live trials will have many of these, and the doctor reviews them.
- "Enroll before surgery" bullets match `pre_surgery_detection.inclusion_phrases` in `policy.yaml`.
- Rule 10: `Facts` has no mutation check field. TMB criteria read `tmb_clia` only.
- `python -m agent.eligibility_eval`: agreement with `gold_eligibility` in `clinical/patients/`. Mode `entered` uses the `enter_results` blocks as is. Mode `profile` fills `recurrence_number`, `comorbidities`, `can_use_phone_or_tablet` and `tmb_clia` from the profile.
