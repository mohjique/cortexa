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

## Demo cases

`demo_cases.json` holds synthetic catalogs for the three recurrent-glioma demo patients. Copy each case's
`mutation_counts_96` and `true_sbs11_fraction` into `clinical/patients/<case>.json`.

| Case | Mutations | True SBS11 share | Call it gives |
|---|---|---|---|
| demo-016 | 600 | 0.60 | detected |
| demo-017 | 30 | 0.10 | inconclusive (SBS11 is present; 30 mutations can't show it) |
| demo-018 | 80 | 0.00 | not_detected |

The 96 counts follow `bin_order` (COSMIC row order). `true_sbs11_fraction` is an answer key: the app never reads it.
