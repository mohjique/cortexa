# Mutation check: simulation figures

The evaluation we show judges for the mutation check. Synthetic simulation only: background-only and
SBS11-planted catalogs drawn from COSMIC v3.5 SBS96 signature mixes, fixed seeds. Regenerate in the sigdetect repo
(`scripts/roc_sbs11.py`, `scripts/build_sbs11.py`).

## `sbs11_roc.png`: detection rate vs false-alarm rate, per mutation count

One curve per total mutation count (20 to 2,000), for SBS11 making up 10%, 25% and 50% of the mutations. Each
curve sweeps the decision threshold; the dot is where the policy's 1% false-alarm rate puts it.

Detection rate at the 1% false-alarm rate:

| SBS11 share | 20 | 50 | 100 | 200 | 500 | 1000 | 2000 |
|---|---|---|---|---|---|---|---|
| 10% | 0.02 | 0.04 | 0.07 | 0.11 | 0.30 | 0.58 | 0.86 |
| 25% | 0.08 | 0.19 | 0.41 | 0.65 | 0.97 | 1.00 | 1.00 |
| 50% | 0.30 | 0.63 | 0.93 | 1.00 | 1.00 | 1.00 | 1.00 |

**How to read it.** Below about 50 mutations, even a half-SBS11 tumor is usually missed, which is why the check
says `inconclusive` there instead of `not_detected`. With a few hundred mutations, a 25% share is caught almost
every time. The curves' sharp rise near a 50% false-alarm rate is real: about half of background-only catalogs fit
exactly zero SBS11, because the amount can't go below zero.

## `sbs11_power_map.png`: detection probability over mutation count × SBS11 share

The same simulation as a map. Solid line: the share detected 50% of the time (Currie's critical level). Dashed
line: the share detected 95% of the time (the detection limit). Both fall as 1/√N.
