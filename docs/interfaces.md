# Interfaces between components

Draft. Agree on these at kickoff, then change them only after telling the team.

## Classifier → agent

```json
{
  "case_id": "demo-001",
  "suspected_bucket": "glioma | meningioma | pituitary | no_tumor",
  "probabilities": {"glioma": 0.91, "meningioma": 0.05, "pituitary": 0.02, "no_tumor": 0.02},
  "medgemma_bucket": "glioma",
  "confidence": 0.88,
  "needs_review": false,
  "heatmap_path": "artifacts/demo-001_gradcam.png"
}
```

## Doctor-entered results → agent

```json
{
  "case_id": "demo-001",
  "confirmed_bucket": "glioma",
  "diagnosis": "Glioblastoma, IDH-wildtype",
  "grade": 4,
  "idh": "mutant | wildtype | pending | unknown",
  "codeletion_1p19q": "codeleted | intact | pending | unknown",
  "mgmt": "methylated | unmethylated | pending | unknown",
  "recurrent": false,
  "prior_treatments": ["surgery", "radiation", "temozolomide"],
  "age": 54,
  "zip": "77840",
  "broad_sequencing_available": false
}
```

## Mutation check → agent

```json
{
  "case_id": "demo-002",
  "ran": true,
  "total_mutations": 1340,
  "sbs11_call": "detected | not_detected | inconclusive",
  "false_alarm_rate": 0.01,
  "exposures": {"SBS1": 0.12, "SBS5": 0.18, "SBS11": 0.70},
  "note": "Context only. Does not decide eligibility."
}
```

## Agent eligibility output

```json
{
  "trial_id": "NCT00000000",
  "criterion_text": "Histologically confirmed IDH-mutant glioma",
  "status": "met | not_met | unknown",
  "evidence_field": "idh",
  "reason": "needs pathology: IDH status"
}
```

## Audit log entry

```json
{"ts": "2026-11-21T14:03:12Z", "case_id": "demo-001", "actor": "agent | doctor", "action": "trial_search", "detail": {}}
```
