# mutation_check/

Owner: Nuclear engineer.

Runs only for recurrent gliomas with broad sequencing (exome or genome).

1. Convert somatic SNVs to 96-channel counts (6 substitution types x 4 x 4 flanking bases).
2. Background model: normal glioma processes (aging SBS1/SBS5) from the COSMIC SBS v3 matrix.
3. Detection: Poisson-likelihood fit with and without an SBS11 (temozolomide) component; threshold calibrated to the false-alarm rate in `policies/policy.yaml`. Output `detected | not_detected | inconclusive`.
4. Simulation (core deliverable): synthetic tumors mixing background with varying SBS11 amounts under Poisson noise, total counts from ~20 to ~2,000. Plot detection rate vs false-alarm rate per count.
5. Optional real-data check: open-access TCGA glioma mutation files.

Approach borrowed from LBNL's NMF methods for gamma-ray spectra (Bandstra et al.): learned background plus anomaly detection with a controlled false-alarm rate.

The result is context only. It never decides eligibility, and a high mutation count never routes a patient to immunotherapy trials.

Output format: see `docs/interfaces.md` (Mutation check → agent).

## Calling it (agent)

```python
from mutation_check import run_mutation_check, MutationCheckUnavailable

out = run_mutation_check(request, false_alarm_rate=policy.mutation_check.false_alarm_rate)
# -> a MutationCheckRan or MutationCheckSkipped dict; validates against agent/schemas.py
```

- `request` is the Mutation check input (`case_id`, `sequencing`, `reference`, `snvs`). It may carry
  `counts_96` instead of `snvs` (issue #6), which is how the demo patients' stored catalogs run.
- The agent decides "recurrent glioma" before calling. The check itself skips anything that isn't exome or
  genome (`skip_reason: no_broad_sequencing`).
- `MutationCheckUnavailable` means no trustworthy answer: the engine isn't installed and the catalog isn't a demo
  case, or `policy.yaml`'s false-alarm rate differs from the calibration's (0.01). Log it and continue matching
  without the check, as for `inconclusive`. Never substitute a guess.
- Results carry the four fields from issue #6 by default: `sbs11_fraction_ci95`, `detection_limit_95`,
  `critical_fraction` and `summary`. `summary` is shown exactly as written. A `null` limit means it isn't
  established at that mutation count, and the summary says so in words. `include_limits=False` drops all four.

## The engine

Detection runs in [sigdetect](https://github.com/almondshawarma/sigdetect) (private; ask the mutation-check
owner for access): Poisson fit of non-negative signature exposures, likelihood-ratio test with thresholds
simulated per mutation count, Currie detection limits. Without it installed, the three demo cases still run from
`demo_results.json` (precomputed by `python -m mutation_check.make_demo_results`), so nobody else needs sigdetect
or the COSMIC file to run the app. With it: `pip install -e <sigdetect checkout>`, and the calibration is found at
`../sigdetect/data/sbs11_calibration.npz` or `$SIGDETECT_CALIBRATION`.

Tests: `python -m pytest mutation_check` (the live-engine tests skip where sigdetect isn't installed).

## Demo cases

`demo_cases.json` holds synthetic catalogs for the three recurrent-glioma demo patients. Copy each case's
`mutation_counts_96` and `true_sbs11_fraction` into `clinical/patients/<case>.json`.

| Case | Mutations | True SBS11 share | Call it gives |
|---|---|---|---|
| demo-016 | 600 | 0.60 | detected |
| demo-017 | 30 | 0.10 | inconclusive (SBS11 is present; 30 mutations can't show it) |
| demo-018 | 80 | 0.00 | not_detected |

The 96 counts follow `bin_order` (COSMIC row order). `true_sbs11_fraction` is an answer key: the app never reads it.
