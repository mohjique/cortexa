# agent/demo_cache/

Synthetic stand-in for live API responses, so the demo runs if an API is down. **Everything here is fake.** Trial IDs `NCT990000xx`, titles start with `[SYNTHETIC]`, and sites are named "Synthetic Neuro-Oncology Center A to F". Only the ZIP centroids are real (public, approximate).

Regenerate from repo root: `python agent/demo_cache/make_cache.py .`

The live-response cache goes in `cache/`, which stays gitignored. This folder is committed.

## Files

| File | Shape |
|---|---|
| `ctgov/studies_brain_tumor.json` | ClinicalTrials.gov API v2 `/studies` response (`studies[].protocolSection...`) |
| `nci/trials_brain_tumor.json` | NCI CTS API v2 `/trials` response, trimmed to the fields we use |
| `nominatim/zip_lookup.json` | ZIP → city, state, lat, lon |
| `osrm/drive_times.json` | Drive minutes and straight-line miles from demo origin ZIP 77840 |
| `manifest.json` | Which request each file stands in for |

No server-side geo filter was applied, and the agent applies none either: there is no distance cutoff. The agent computes distance from `geoPoint` to sort trials nearest first.

## Trials and what each one demos

Distances are from 77840 (College Station). Every eligible trial is shown regardless of distance.

| ID | Status | Site | Miles | Demos |
|---|---|---|---|---|
| NCT99000001 | Recruiting | Houston | 85 | IDH-mutant grade 2-3 astrocytoma. IDH and 1p/19q decide it. |
| NCT99000002 | Recruiting | Houston, Dallas | 85 | Recurrent IDH-wildtype GBM after temozolomide. Recurrent-glioma path. |
| NCT99000003 | Not yet recruiting | Austin | 87 | Must enroll **before surgery**. Pre-op screen flag. |
| NCT99000004 | Recruiting | Austin | 87 | MGMT-unmethylated new GBM. MGMT and grade decide it. |
| NCT99000005 | Recruiting | San Antonio | 155 | Grade 2-3 meningioma. A farther match (155 mi from 77840), still on the shortlist. |
| NCT99000006 | Not yet recruiting | Houston | 85 | ACTH pituitary adenoma, medication. Pituitary meds-only path. |
| NCT99000007 | Recruiting | New Orleans | 376 | 1p/19q-codeleted oligodendroglioma. A far match (376 mi), or a near miss. |
| NCT99000008 | Recruiting | Oklahoma City | 342 | Fully remote participation. On the shortlist for eligible patients who can use a phone or tablet. |
| NCT99000009 | Recruiting | Dallas | 154 | **Safety trap.** Checkpoint inhibitor needing high TMB from a CLIA assay. The mutation check count must never mark this `met`. With no CLIA TMB result it stays `unknown`. |
