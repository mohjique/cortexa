# clinical/

- 23 synthetic patients in `patients/demo-001.json` to `demo-023.json`, covering every branch of the decision tree. Spec: `PATIENT_PROFILES_GUIDE.md`; template: `patients/_template.json`.
- Gold eligibility labels: manual met / not met / unknown calls against the synthetic trials in `agent/demo_cache/`. Used to measure the agent.
- Medical wording for rules in `policies/policy.yaml`.
- Sanity-check the clinical workflow in `docs/decision-tree.md`.

Synthetic data only. No real patient information.
