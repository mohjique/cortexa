# mutation_check/

Owner: nuclear engineer.

Runs only for recurrent gliomas with broad sequencing (exome or genome).

1. Convert somatic SNVs to 96-channel counts (6 substitution types x 4 x 4 flanking bases).
2. Background model: normal glioma processes (aging SBS1/SBS5) from the COSMIC SBS v3 matrix.
3. Detection: Poisson-likelihood fit with and without an SBS11 (temozolomide) component; threshold calibrated to the false-alarm rate in `policies/policy.yaml`. Output `detected | not_detected | inconclusive`.
4. Simulation (core deliverable): synthetic tumors mixing background with varying SBS11 amounts under Poisson noise, total counts from ~20 to ~2,000. Plot detection rate vs false-alarm rate per count.
5. Optional real-data check: open-access TCGA glioma mutation files.

Approach borrowed from LBNL's NMF methods for gamma-ray spectra (Bandstra et al.): learned background plus anomaly detection with a controlled false-alarm rate.

The result is context only. It never decides eligibility, and a high mutation count never routes a patient to immunotherapy trials.

Output format: see `docs/interfaces.md` (Mutation check → agent).
